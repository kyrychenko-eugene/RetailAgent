"""BigQuery access: the provided `BigQueryRunner` plus the DD §8.6 extension
(query parameters, dry run, bytes cap, per-query timeout, job labels, typed errors).

`execute_query()` and `get_table_schema()` are the original methods and are unchanged.
"""
from __future__ import annotations

import concurrent.futures
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd
from google.api_core import exceptions as gexc
from google.cloud import bigquery

from retail_agent.config import ConfigError, Settings

QueryParamValue = str | int | float | bool | list[str] | list[int]

_LABEL_MAX = 63
_JOB_SUFFIX = re.compile(r"\s*(Location:|Job ID:).*", re.DOTALL)


@dataclass(frozen=True)
class DryRunResult:
    ok: bool
    bytes_processed: int | None
    error: str | None              # BigQuery message (syntax / unknown column ...), None if ok


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: list[dict[str, Any]]     # JSON-safe values: datetime/date -> ISO str, Decimal -> float
    bytes_processed: int
    bytes_billed: int
    job_id: str
    elapsed_ms: int


class BigQueryError(Exception): ...
class BigQueryBadRequestError(BigQueryError): ...      # syntax / semantic error; message is model-safe
class BytesCapExceededError(BigQueryError): ...        # dry-run bytes > cap, or bytesBilledLimitExceeded
class BigQueryTimeoutError(BigQueryError): ...
class BigQueryUnavailableError(BigQueryError): ...     # 5xx / transport after client retries, auth failure


def to_query_parameters(
    params: Mapping[str, QueryParamValue],
) -> list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter]:
    """Typed BigQuery query parameters. bool is checked before int."""
    out: list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter] = []
    for name, value in params.items():
        if isinstance(value, list):
            elem = "INT64" if value and all(isinstance(v, int) and not isinstance(v, bool) for v in value) else "STRING"
            out.append(bigquery.ArrayQueryParameter(name, elem, value))
        elif isinstance(value, bool):
            out.append(bigquery.ScalarQueryParameter(name, "BOOL", value))
        elif isinstance(value, int):
            out.append(bigquery.ScalarQueryParameter(name, "INT64", value))
        elif isinstance(value, float):
            out.append(bigquery.ScalarQueryParameter(name, "FLOAT64", value))
        elif isinstance(value, str):
            out.append(bigquery.ScalarQueryParameter(name, "STRING", value))
        else:
            raise TypeError(f"Unsupported query parameter type for '{name}': {type(value).__name__}")
    return out


def sanitize_labels(labels: Mapping[str, str]) -> dict[str, str]:
    """Keys/values lower-case, [a-z0-9_-] only, max 63 chars; drop empty values."""
    def clean(text: str) -> str:
        return re.sub(r"[^a-z0-9_-]", "_", str(text).lower())[:_LABEL_MAX]

    out: dict[str, str] = {}
    for key, value in labels.items():
        if value is None or not str(value):
            continue
        k, v = clean(key), clean(value)
        if not k:
            continue
        if not k[0].isalpha():          # BigQuery label keys must start with a letter
            k = ("k" + k)[:_LABEL_MAX]
        out[k] = v
    return out


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class BigQueryRunner:
    """A lean BigQuery client for executing SQL queries and returning DataFrame results."""
    
    def __init__(self, project_id: Optional[str] = None, dataset_id: Optional[str] = "bigquery-public-data.thelook_ecommerce",
                 location: Optional[str] = "US") -> None:
        """Initialize BigQuery client.
        
        Args:
            project_id: Google Cloud project ID. If None, uses default credentials.
            dataset_id: BigQuery dataset ID. If None, uses default dataset.
            location: Job location (the public dataset lives in US).
        """
        logging.info("Initializing BigQuery client")
        try:
            self.client = bigquery.Client(project=project_id, location=location)
            self.dataset_id = dataset_id
            logging.info(f"BigQuery client initialized for dataset: {self.dataset_id}")
        except Exception as e:
            logging.error(f"Failed to initialize BigQuery client: {str(e)}")
            raise
    
    def execute_query(self, sql_query: str) -> pd.DataFrame:
        """Execute a SQL query and return results as a DataFrame.
        
        Args:
            sql_query: The SQL query to execute.
            
        Returns:
            DataFrame containing the query results.
            
        Raises:
            Exception: If query execution fails.
        """
        try:
            logging.info(f"Executing BigQuery query")
            query_job = self.client.query(sql_query)
            df = query_job.result().to_dataframe()
            logging.info(f"Query completed successfully, returned {len(df)} rows")
            return df
        except Exception as e:
            logging.error(f"BigQuery execution failed: {str(e)}")
            raise 

    def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        """Get schema information for a specific table.
        
        Args:
            table_name: Name of the table (orders, order_items, products, users).
            
        Returns:
            List of dictionaries containing column information.
        """
        try:
            table_ref = f"{self.dataset_id}.{table_name}"
            table = self.client.get_table(table_ref)
            schema_info = []
            for field in table.schema:
                schema_info.append({
                    "name": field.name,
                    "type": field.field_type,
                    "mode": field.mode,
                    "description": field.description or ""
                })
            logging.info(f"Retrieved schema for table {table_name}")
            return schema_info
        except Exception as e:
            logging.error(f"Failed to get schema for table {table_name}: {str(e)}")
            raise

    # -- DD §8.6 extension ---------------------------------------------------

    def _message(self, exc: BaseException) -> str:
        """Model-safe error text: no dataset path, no job/location suffix."""
        text = getattr(exc, "message", None) or str(exc)
        text = _JOB_SUFFIX.sub("", text)
        if self.dataset_id:
            text = text.replace(self.dataset_id, "<dataset>")
        text = re.sub(r"^\d{3} ", "", text.strip())
        return text

    def _job_config(self, params, **kwargs) -> bigquery.QueryJobConfig:
        config = bigquery.QueryJobConfig(**kwargs)
        if params:
            config.query_parameters = to_query_parameters(params)
        return config

    def dry_run(self, sql: str, params: Mapping[str, QueryParamValue] | None = None) -> DryRunResult:
        """Never raises for bad SQL (returns ok=False). Raises BigQueryUnavailableError on transport."""
        config = self._job_config(params, dry_run=True, use_query_cache=False)
        try:
            job = self.client.query(sql, job_config=config)
        except gexc.BadRequest as exc:
            return DryRunResult(ok=False, bytes_processed=None, error=self._message(exc))
        except Exception as exc:
            raise self._map_error(exc, default=BigQueryUnavailableError) from exc
        return DryRunResult(ok=True, bytes_processed=int(job.total_bytes_processed or 0), error=None)

    def run_query(self, sql: str, *, params: Mapping[str, QueryParamValue] | None = None,
                  max_bytes_billed: int, timeout_s: float,
                  labels: Mapping[str, str] | None = None) -> QueryResult:
        """Execute with maximum_bytes_billed, job timeout and labels; maps errors to the types above."""
        config = self._job_config(
            params,
            maximum_bytes_billed=max_bytes_billed,
            job_timeout_ms=max(1, int(timeout_s * 1000)),
            labels=sanitize_labels(labels or {}),
        )
        started = time.monotonic()
        job = None
        try:
            job = self.client.query(sql, job_config=config)
            result = job.result(timeout=timeout_s)
            rows = [_json_safe(dict(row.items())) for row in result]
            columns = tuple(field.name for field in result.schema)
        except concurrent.futures.TimeoutError as exc:
            if job is not None:
                try:
                    job.cancel()
                except Exception:
                    logging.warning("Could not cancel timed-out BigQuery job")
            raise BigQueryTimeoutError(f"Query exceeded {timeout_s:.0f} s") from exc
        except Exception as exc:
            raise self._map_error(exc, default=BigQueryUnavailableError) from exc
        return QueryResult(
            columns=columns,
            rows=rows,
            bytes_processed=int(job.total_bytes_processed or 0),
            bytes_billed=int(job.total_bytes_billed or 0),
            job_id=job.job_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    def _map_error(self, exc: BaseException, *, default: type[BigQueryError]) -> BigQueryError:
        message = self._message(exc)
        if isinstance(exc, BigQueryError):
            return exc
        if "bytesBilledLimitExceeded" in str(exc) or "bytes billed" in str(exc).lower():
            return BytesCapExceededError(message)
        if isinstance(exc, (gexc.DeadlineExceeded, TimeoutError)):
            return BigQueryTimeoutError(message)
        if isinstance(exc, (gexc.BadRequest, gexc.NotFound)):
            return BigQueryBadRequestError(message)
        return default(message)


def create_runner(settings: Settings) -> BigQueryRunner:
    """Build the runner from settings. A missing GOOGLE_CLOUD_PROJECT is a ConfigError here,
    not at load time, so offline tests never need it."""
    if not settings.bigquery.project:
        raise ConfigError(
            "BigQuery needs a project to run query jobs: set GOOGLE_CLOUD_PROJECT in .env "
            "(see .env.example) and run `gcloud auth application-default login`."
        )
    return BigQueryRunner(
        project_id=settings.bigquery.project,
        dataset_id=settings.bigquery.dataset,
        location=settings.bigquery.location,
    )
