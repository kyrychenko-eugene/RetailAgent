"""Query guards: run_scoped_query, column_values, PII scans (contracts §2.4). Implemented in Stage 2."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence

from retail_agent.bq_client import BigQueryRunner
from retail_agent.config import Catalog, Settings
from retail_agent.security.identity import Scope
from retail_agent.security.validator import ReasonCode
from retail_agent.tracing import Tracer

if TYPE_CHECKING:
    from retail_agent.llm.wrapper import Deadline


class FailureKind(StrEnum):
    VALIDATION = "validation"
    OUT_OF_SCOPE_BRAND = "out_of_scope_brand"
    PII_REPEATED = "pii_repeated"
    BAD_REQUEST = "bad_request"
    BYTES_CAP = "bytes_cap"
    ROW_LIMIT = "row_limit"
    PII_IN_RESULT = "pii_in_result"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    DEADLINE = "deadline"
    REPEATED = "repeated"


@dataclass(frozen=True)
class QuerySuccess:
    query_id: str
    columns: tuple[str, ...]
    rows: list[dict[str, Any]]
    row_count: int
    bytes_processed: int
    sql: str
    hint: str | None = None
    cached: bool = False


@dataclass(frozen=True)
class QueryFailure:
    query_id: str
    kind: FailureKind
    model_message: str
    user_message: str | None
    terminal: bool
    security_event: bool
    sql: str
    reason_code: ReasonCode | None = None


QueryOutcome = QuerySuccess | QueryFailure


@dataclass
class QueryContext:
    """Per-turn state shared by run_sql and get_column_values."""
    scope: Scope
    exec_id: str
    thread_id: str
    turn_id: str
    deadline: "Deadline"
    settings: Settings
    catalog: Catalog
    bq: BigQueryRunner
    tracer: Tracer
    executed: dict[str, QueryOutcome] = field(default_factory=dict)
    pii_attempts: int = 0
    next_query_no: int = 1

    def new_query_id(self) -> str:
        raise NotImplementedError


def run_scoped_query(sql: str, ctx: QueryContext, *,
                     user_requested_granularity: bool = False) -> QueryOutcome:
    raise NotImplementedError


def column_values(column: str, ctx: QueryContext, *, search: str | None = None,
                  limit: int | None = None) -> QueryOutcome:
    raise NotImplementedError


def normalize_sql(sql: str) -> str:
    raise NotImplementedError


@dataclass(frozen=True)
class PiiHit:
    kind: Literal["EMAIL", "PHONE", "ADDRESS", "CARD"]
    column: str | None


def scan_rows_for_pii(rows: Sequence[Mapping[str, Any]]) -> list[PiiHit]:
    raise NotImplementedError


def scan_text_for_pii(text: str) -> list[PiiHit]:
    raise NotImplementedError
