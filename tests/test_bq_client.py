"""Offline tests of the BigQueryRunner extension: a fake google client stands in for BigQuery."""
from __future__ import annotations

import concurrent.futures
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from google.api_core import exceptions as gexc
from google.cloud import bigquery

from retail_agent import bq_client
from retail_agent.bq_client import (BigQueryBadRequestError, BigQueryRunner, BigQueryTimeoutError,
                                    BigQueryUnavailableError, BytesCapExceededError, create_runner,
                                    sanitize_labels, to_query_parameters)
from retail_agent.config import ConfigError, load_settings

DATASET = "bigquery-public-data.thelook_ecommerce"


class FakeJob:
    def __init__(self, rows=(), schema=(), error=None, timeout=False):
        self._rows, self._schema, self._error, self._timeout = list(rows), schema, error, timeout
        self.job_id, self.total_bytes_processed, self.total_bytes_billed = "job-1", 1000, 10485760
        self.cancelled = False

    def result(self, timeout=None):
        if self._timeout:
            raise concurrent.futures.TimeoutError()
        if self._error:
            raise self._error
        return _Rows(self._rows, self._schema)

    def cancel(self):
        self.cancelled = True


class _Rows:
    def __init__(self, rows, schema):
        self.schema = [SimpleNamespace(name=n) for n in schema]
        self._rows = rows

    def __iter__(self):
        return iter(SimpleNamespace(items=lambda r=r: list(r.items())) for r in self._rows)


class FakeClient:
    def __init__(self, job=None, query_error=None):
        self.job, self.query_error, self.calls = job, query_error, []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        if self.query_error:
            raise self.query_error
        return self.job


def runner(job=None, query_error=None) -> tuple[BigQueryRunner, FakeClient]:
    r = BigQueryRunner.__new__(BigQueryRunner)
    r.client, r.dataset_id = FakeClient(job, query_error), DATASET
    return r, r.client


def test_to_query_parameters_types():
    params = to_query_parameters({"s": "x", "i": 3, "f": 1.5, "b": True, "ls": ["a"], "li": [1, 2], "empty": []})
    by_name = {p.name: p for p in params}
    assert by_name["b"].type_ == "BOOL" and by_name["i"].type_ == "INT64" and by_name["f"].type_ == "FLOAT64"
    assert by_name["s"].type_ == "STRING"
    assert isinstance(by_name["ls"], bigquery.ArrayQueryParameter) and by_name["ls"].array_type == "STRING"
    assert by_name["li"].array_type == "INT64" and by_name["empty"].array_type == "STRING"
    with pytest.raises(TypeError):
        to_query_parameters({"x": object()})


def test_sanitize_labels():
    out = sanitize_labels({"Thread_ID": "AB12 cd", "app": "retail_agent", "empty": "", "1bad": "x", "exec_id": "ceo"})
    assert out == {"thread_id": "ab12_cd", "app": "retail_agent", "k1bad": "x", "exec_id": "ceo"}
    assert len(sanitize_labels({"k": "x" * 100})["k"]) == 63


def test_dry_run_ok_and_bad_sql():
    job = FakeJob()
    r, client = runner(job)
    res = r.dry_run("SELECT 1", {"brands": ["Nike"]})
    assert res.ok and res.bytes_processed == 1000 and res.error is None
    cfg = client.calls[0][1]
    assert cfg.dry_run is True and cfg.use_query_cache is False and cfg.query_parameters[0].name == "brands"

    bad, _ = runner(query_error=gexc.BadRequest(f"Unrecognized name: foo at [1:8]\n\nLocation: US\nJob ID: abc"))
    res = bad.dry_run("SELECT foo")
    assert not res.ok and res.bytes_processed is None and res.error == "Unrecognized name: foo at [1:8]"


def test_dry_run_transport_error_raises():
    r, _ = runner(query_error=gexc.ServiceUnavailable("down"))
    with pytest.raises(BigQueryUnavailableError):
        r.dry_run("SELECT 1")


def test_run_query_result_conversion_and_config():
    rows = [{"d": datetime(2026, 1, 2, 3, tzinfo=timezone.utc), "x": Decimal("1.5"), "day": date(2026, 1, 2), "n": 3}]
    job = FakeJob(rows, schema=("d", "x", "day", "n"))
    r, client = runner(job)
    res = r.run_query("SELECT 1", params={"brands": ["A"]}, max_bytes_billed=500, timeout_s=12,
                      labels={"thread_id": "T1", "app": "retail_agent"})
    assert res.columns == ("d", "x", "day", "n")
    assert res.rows == [{"d": "2026-01-02T03:00:00+00:00", "x": 1.5, "day": "2026-01-02", "n": 3}]
    assert (res.bytes_processed, res.bytes_billed, res.job_id) == (1000, 10485760, "job-1")
    cfg = client.calls[0][1]
    assert cfg.maximum_bytes_billed == 500 and int(cfg.job_timeout_ms) == 12000
    assert cfg.labels == {"thread_id": "t1", "app": "retail_agent"}


@pytest.mark.parametrize("exc, expected", [
    (gexc.BadRequest("Unrecognized name: foo"), BigQueryBadRequestError),
    (gexc.BadRequest("Query exceeded limit for bytes billed: 100. 200 or higher required."), BytesCapExceededError),
    (gexc.BadRequest("reason: bytesBilledLimitExceeded"), BytesCapExceededError),
    (gexc.DeadlineExceeded("slow"), BigQueryTimeoutError),
    (gexc.ServiceUnavailable("503"), BigQueryUnavailableError),
    (gexc.InternalServerError("500"), BigQueryUnavailableError),
    (gexc.Forbidden("no access"), BigQueryUnavailableError),
    (ConnectionError("reset"), BigQueryUnavailableError),
])
def test_run_query_error_mapping(exc, expected):
    r, _ = runner(FakeJob(error=exc))
    with pytest.raises(expected):
        r.run_query("SELECT 1", max_bytes_billed=1, timeout_s=1)


def test_run_query_timeout_cancels_job():
    job = FakeJob(timeout=True)
    r, _ = runner(job)
    with pytest.raises(BigQueryTimeoutError):
        r.run_query("SELECT 1", max_bytes_billed=1, timeout_s=1)
    assert job.cancelled


def test_error_messages_do_not_leak_the_dataset_path():
    r, _ = runner(FakeJob(error=gexc.BadRequest(f"Not found: Table {DATASET}.secret")))
    with pytest.raises(BigQueryBadRequestError) as exc:
        r.run_query("SELECT 1", max_bytes_billed=1, timeout_s=1)
    assert DATASET not in str(exc.value) and "<dataset>" in str(exc.value)


def test_create_runner_requires_project():
    with pytest.raises(ConfigError, match="GOOGLE_CLOUD_PROJECT"):
        create_runner(load_settings(env={}))


def test_original_methods_are_kept():
    assert callable(bq_client.BigQueryRunner.execute_query) and callable(bq_client.BigQueryRunner.get_table_schema)
