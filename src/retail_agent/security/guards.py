"""Query guards: run_scoped_query, column_values, PII scans (contracts §2.4, DD §8.3, §10.1, §10.2).

Every model query takes one path: repeated-query check -> deadline check -> validator ->
scoped CTEs -> row cap -> dry-run (bytes vs cap) -> execution -> PII scan of the rows.
Model-caused problems come back as a typed QueryFailure, never as an exception.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence

import sqlglot
from sqlglot.errors import ParseError, TokenError

from retail_agent.bq_client import (
    BigQueryBadRequestError,
    BigQueryRunner,
    BigQueryTimeoutError,
    BigQueryUnavailableError,
    BytesCapExceededError,
    QueryParamValue,
)
from retail_agent.config import Catalog, Settings
from retail_agent.security.identity import Scope
from retail_agent.security.scoped_sql import DIALECT, ScopedSqlError, build_scoped_sql, wrap_with_limit
from retail_agent.security.validator import ReasonCode, ValidationResult, validate_sql
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
        query_id = f"Q{self.next_query_no}"
        self.next_query_no += 1
        return query_id


# -- messages ------------------------------------------------------------------------------

EMPTY_RESULT_HINT = (
    "The query returned no rows. Check string literals (exact case and spelling; use "
    "get_column_values to see real values) and the date range before concluding there is no data."
)
REPEATED_SUFFIX = "This exact query was already run; change the approach."
_MSG_DEADLINE = "No time is left for another query. Answer with the results you already have."
_MSG_BYTES_MODEL = "The query would scan more data than allowed. Do not retry it."
_MSG_BYTES_USER = ("That request is too large to run right now (it would scan more data than the "
                   "allowed limit). Try narrowing it, for example to a shorter period or fewer categories.")
_MSG_UNAVAILABLE_MODEL = "The data source is unavailable. Do not retry."
_MSG_UNAVAILABLE_USER = "The data source is unavailable right now. Please try again in a few minutes."
_MSG_TIMEOUT = "The query timed out. Simplify it (fewer joins, more aggregation) or answer with what you have."
_MSG_PII_RESULT = ("The result contained values that cannot be shown (personal contact data). "
                   "Rewrite the query to return aggregates without free-text personal fields.")
_MSG_PII_REPEATED_MODEL = "Repeated requests for unavailable personal data. Stop and tell the user."
_MSG_PII_REPEATED_USER = ("I can't provide personal customer data such as names, email addresses or "
                          "street addresses. I can help with aggregated analysis instead.")
_MSG_ROW_LIMIT_MODEL = "The query returned more than {n} rows. Rewrite it with aggregation."
_MSG_ROW_LIMIT_USER = ("That request returns more than {n} rows, which is above the limit I can analyse. "
                       "I can aggregate it instead (for example by month or category) if you'd like.")

# Validator reason code -> security_event kind (contracts §1.3).
_SECURITY_KIND: dict[ReasonCode, str] = {
    ReasonCode.NOT_SELECT: "forbidden_statement",
    ReasonCode.FORBIDDEN_FUNCTION: "forbidden_function",
    ReasonCode.INFORMATION_SCHEMA: "information_schema",
    ReasonCode.QUALIFIED_TABLE: "qualified_table",
    ReasonCode.RESERVED_CTE_NAME: "reserved_cte_name",
    ReasonCode.PII_COLUMN: "pii_column",
    ReasonCode.OUT_OF_SCOPE_BRAND: "out_of_scope_brand",
    ReasonCode.QUASI_IDENTIFIER: "quasi_identifier",
}

# Failure kind -> trace error_type (contracts §4.3).
_ERROR_TYPE: dict[FailureKind, str] = {
    FailureKind.VALIDATION: "validation",
    FailureKind.OUT_OF_SCOPE_BRAND: "validation",
    FailureKind.PII_REPEATED: "validation",
    FailureKind.PII_IN_RESULT: "validation",
    FailureKind.ROW_LIMIT: "validation",
    FailureKind.BAD_REQUEST: "bq_bad_request",
    FailureKind.BYTES_CAP: "bytes_cap",
    FailureKind.TIMEOUT: "bq_timeout",
    FailureKind.UNAVAILABLE: "bq_unavailable",
    FailureKind.DEADLINE: "deadline",
}
# Transient failures are not stored for the repeated-query check (R11): a retry may succeed.
_NOT_STORED = frozenset({FailureKind.TIMEOUT, FailureKind.UNAVAILABLE, FailureKind.DEADLINE})


# -- query path ----------------------------------------------------------------------------


_URL = re.compile(r"https?://\S+")


def _clean_error(error: object) -> str:
    """BigQuery error text for the model and traces, without job URLs (they carry the project ID)."""
    return _URL.sub("<url>", str(error))


def run_scoped_query(sql: str, ctx: QueryContext, *,
                     user_requested_granularity: bool = False) -> QueryOutcome:
    """Repeated check -> deadline check -> validate -> build_scoped_sql -> wrap_with_limit ->
    dry_run (bytes vs cap) -> run_query -> PII scan of rows -> outcome. Writes the tool_call
    trace record and any security_event. Never raises for model-caused problems."""
    return _run(sql, ctx, tool="run_sql", extra_params={},
                user_requested_granularity=user_requested_granularity)


def column_values(column: str, ctx: QueryContext, *, search: str | None = None,
                  limit: int | None = None) -> QueryOutcome:
    """Distinct values of a catalog-categorical column, through the same scoped CTEs and guards."""
    max_limit = ctx.settings.bigquery.column_values_limit
    table_name, _, col_name = column.strip().partition(".")
    col = ctx.catalog.column(table_name, col_name) if col_name else None
    problem = None
    if col is None or not col.categorical:
        categorical = sorted(f"{t.name}.{c.name}" for t in ctx.catalog.tables.values()
                             for c in t.columns if c.categorical)
        problem = (f"`{column}` is not a categorical column. Use one of: {', '.join(categorical)}.")
    elif col.high_cardinality and not (search and search.strip()):
        problem = f"`{column}` has many values; pass `search` (a case-insensitive substring)."
    if problem is not None:
        query_id = ctx.new_query_id()
        failure = QueryFailure(query_id=query_id, kind=FailureKind.VALIDATION, model_message=problem,
                               user_message=None, terminal=False, security_event=False, sql=column)
        _trace_call(ctx, "get_column_values", failure, latency_ms=0)
        return failure

    n = max(1, min(int(limit) if limit else max_limit, max_limit))
    table = ctx.catalog.tables[table_name.lower()].name
    name = col.name
    where = f"{name} IS NOT NULL"
    params: dict[str, QueryParamValue] = {}
    if search and search.strip():
        where += f" AND LOWER(CAST({name} AS STRING)) LIKE CONCAT('%', LOWER(@search), '%')"
        params["search"] = search.strip()
    # The limit is a validated int, inlined so wrap_with_limit keeps it as a literal top-level LIMIT.
    sql = (f"SELECT {name} AS value, COUNT(*) AS n FROM {table} WHERE {where} "
           f"GROUP BY value ORDER BY n DESC, value LIMIT {n}")
    outcome = _run(sql, ctx, tool="get_column_values", extra_params=params,
                   user_requested_granularity=False)
    if (search and search.strip() and table_name.strip().lower() == "products" and name.lower() == "brand"
            and not ctx.scope.all_brands and isinstance(outcome, QuerySuccess) and not outcome.rows):
        # The scoped CTE holds only this user's brands, so a brand search with no match is a brand
        # outside the scope (or a typo). Stop with the "no access" answer instead of leaving it to the model.
        brand = search.strip()
        ctx.tracer.security_event("out_of_scope_brand", ReasonCode.OUT_OF_SCOPE_BRAND.value,
                                  query_id=outcome.query_id, brands=[brand])
        return _fail(outcome.query_id, sql, FailureKind.OUT_OF_SCOPE_BRAND,
                     f"Brand `{brand}` is outside this user's data. Do not retry; tell the user they have no access to it.",
                     user_message=f"You don't have access to brand {brand}, or it does not exist in your data.",
                     terminal=True, security_event=True, reason_code=ReasonCode.OUT_OF_SCOPE_BRAND)
    return outcome


def _run(sql: str, ctx: QueryContext, *, tool: str, extra_params: dict[str, QueryParamValue],
         user_requested_granularity: bool) -> QueryOutcome:
    started = time.monotonic()
    query_id = ctx.new_query_id()
    key = normalize_sql(sql) + (json.dumps(extra_params, sort_keys=True) if extra_params else "")

    previous = ctx.executed.get(key)
    if previous is not None:
        outcome = _repeat(previous, query_id)
        _trace_call(ctx, tool, outcome, latency_ms=_ms(started))
        return outcome

    outcome, executed_sql, bytes_scanned = _execute(sql, ctx, query_id, extra_params,
                                                    user_requested_granularity)
    if not (isinstance(outcome, QueryFailure) and outcome.kind in _NOT_STORED):
        ctx.executed[key] = outcome
    _trace_call(ctx, tool, outcome, latency_ms=_ms(started), sql_executed=executed_sql,
                bytes_scanned=bytes_scanned)
    return outcome


def _repeat(previous: QueryOutcome, query_id: str) -> QueryOutcome:
    if isinstance(previous, QuerySuccess):
        return replace(previous, query_id=query_id, cached=True)
    return replace(previous, query_id=query_id, kind=FailureKind.REPEATED, terminal=False,
                   security_event=False, user_message=None,
                   model_message=f"{previous.model_message} {REPEATED_SUFFIX}")


def _fail(query_id: str, sql: str, kind: FailureKind, model_message: str, *,
          user_message: str | None = None, terminal: bool = False, security_event: bool = False,
          reason_code: ReasonCode | None = None) -> QueryFailure:
    return QueryFailure(query_id=query_id, kind=kind, model_message=model_message,
                        user_message=user_message, terminal=terminal, security_event=security_event,
                        sql=sql, reason_code=reason_code)


def _execute(sql: str, ctx: QueryContext, query_id: str, extra_params: dict[str, QueryParamValue],
             user_requested_granularity: bool) -> tuple[QueryOutcome, str | None, int | None]:
    """The guarded path. Returns (outcome, final scoped SQL if built, bytes scanned)."""
    bq_settings = ctx.settings.bigquery
    reserve = ctx.settings.turn.synthesis_reserve_s
    if ctx.deadline.remaining() - reserve <= 0:
        return _fail(query_id, sql, FailureKind.DEADLINE, _MSG_DEADLINE, terminal=True), None, None

    validation = validate_sql(sql, ctx.scope, ctx.catalog)
    if not validation.ok:
        return _validation_failure(validation, sql, ctx, query_id), None, None

    try:
        scoped = build_scoped_sql(sql, ctx.scope, ctx.catalog)
        final_sql = wrap_with_limit(scoped.sql, bq_settings.max_rows_to_model)
    except ScopedSqlError as exc:
        return _fail(query_id, sql, FailureKind.VALIDATION, str(exc)), None, None
    params = {**scoped.params, **extra_params}

    try:
        dry = ctx.bq.dry_run(final_sql, params)
    except BigQueryUnavailableError:
        return _unavailable(query_id, sql), final_sql, None
    if not dry.ok:
        return _fail(query_id, sql, FailureKind.BAD_REQUEST, f"BigQuery error: {_clean_error(dry.error)}"), final_sql, None
    if dry.bytes_processed is not None and dry.bytes_processed > bq_settings.bytes_cap:
        return _bytes_cap(query_id, sql), final_sql, dry.bytes_processed

    timeout_s = min(bq_settings.query_timeout_s, ctx.deadline.remaining() - reserve)
    if timeout_s <= 0:
        return _fail(query_id, sql, FailureKind.DEADLINE, _MSG_DEADLINE, terminal=True), final_sql, None
    try:
        result = ctx.bq.run_query(
            final_sql, params=params, max_bytes_billed=bq_settings.bytes_cap, timeout_s=timeout_s,
            labels={"thread_id": ctx.thread_id, "turn_id": ctx.turn_id, "exec_id": ctx.exec_id,
                    "app": "retail_agent"},
        )
    except BytesCapExceededError:
        return _bytes_cap(query_id, sql), final_sql, dry.bytes_processed
    except BigQueryTimeoutError:
        return _fail(query_id, sql, FailureKind.TIMEOUT, _MSG_TIMEOUT), final_sql, dry.bytes_processed
    except BigQueryBadRequestError as exc:
        return _fail(query_id, sql, FailureKind.BAD_REQUEST, f"BigQuery error: {_clean_error(exc)}"), final_sql, None
    except BigQueryUnavailableError:
        return _unavailable(query_id, sql), final_sql, None

    n_max = bq_settings.max_rows_to_model
    if len(result.rows) > n_max:
        outcome = _fail(
            query_id, sql, FailureKind.ROW_LIMIT, _MSG_ROW_LIMIT_MODEL.format(n=n_max),
            user_message=_MSG_ROW_LIMIT_USER.format(n=n_max) if user_requested_granularity else None,
        )
        return outcome, final_sql, result.bytes_processed

    hits = scan_rows_for_pii(result.rows)
    if hits:
        ctx.tracer.security_event(
            "pii_in_result", None, query_id=query_id,
            kinds=sorted({h.kind for h in hits}), columns=sorted({h.column for h in hits if h.column}))
        outcome = _fail(query_id, sql, FailureKind.PII_IN_RESULT, _MSG_PII_RESULT, security_event=True)
        return outcome, final_sql, result.bytes_processed

    rows = [_truncate_cells(r, bq_settings.max_cell_chars) for r in result.rows]
    success = QuerySuccess(
        query_id=query_id, columns=result.columns, rows=rows, row_count=len(rows),
        bytes_processed=result.bytes_processed, sql=sql,
        hint=EMPTY_RESULT_HINT if not rows else None,
    )
    return success, final_sql, result.bytes_processed


def _validation_failure(v: ValidationResult, sql: str, ctx: QueryContext, query_id: str) -> QueryFailure:
    code = v.reason_code
    if v.security_event and code in _SECURITY_KIND:
        ctx.tracer.security_event(_SECURITY_KIND[code], code.value, query_id=query_id, **v.details)
    if code == ReasonCode.OUT_OF_SCOPE_BRAND:
        return _fail(query_id, sql, FailureKind.OUT_OF_SCOPE_BRAND, v.model_message,
                     user_message=v.user_message, terminal=True, security_event=True, reason_code=code)
    if code == ReasonCode.PII_COLUMN:
        ctx.pii_attempts += 1
        if ctx.pii_attempts >= ctx.settings.turn.pii_attempts_before_stop:
            ctx.tracer.security_event("repeated_pii_attempt", code.value, query_id=query_id,
                                      attempts=ctx.pii_attempts)
            return _fail(query_id, sql, FailureKind.PII_REPEATED, _MSG_PII_REPEATED_MODEL,
                         user_message=_MSG_PII_REPEATED_USER, terminal=True, security_event=True,
                         reason_code=code)
    return _fail(query_id, sql, FailureKind.VALIDATION, v.model_message,
                 security_event=v.security_event, reason_code=code)


def _bytes_cap(query_id: str, sql: str) -> QueryFailure:
    return _fail(query_id, sql, FailureKind.BYTES_CAP, _MSG_BYTES_MODEL,
                 user_message=_MSG_BYTES_USER, terminal=True)


def _unavailable(query_id: str, sql: str) -> QueryFailure:
    return _fail(query_id, sql, FailureKind.UNAVAILABLE, _MSG_UNAVAILABLE_MODEL,
                 user_message=_MSG_UNAVAILABLE_USER, terminal=True)


def _truncate_cells(row: Mapping[str, Any], max_chars: int) -> dict[str, Any]:
    return {k: (v[:max_chars] + "…" if isinstance(v, str) and len(v) > max_chars else v)
            for k, v in row.items()}


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _trace_call(ctx: QueryContext, tool: str, outcome: QueryOutcome, *, latency_ms: int,
                sql_executed: str | None = None, bytes_scanned: int | None = None) -> None:
    if isinstance(outcome, QuerySuccess):
        status = "cached" if outcome.cached else ("empty" if outcome.row_count == 0 else "ok")
        error_type, rows = None, outcome.row_count
        if outcome.cached:
            bytes_scanned = 0
    else:
        status, error_type, rows = outcome.kind.value, _ERROR_TYPE.get(outcome.kind), None
    fields: dict[str, Any] = {
        "tool": tool, "query_id": outcome.query_id, "sql": outcome.sql, "outcome": status,
        "error_type": error_type, "rows": rows, "bytes_scanned": bytes_scanned,
        "latency_ms": latency_ms,
    }
    if sql_executed is not None:
        fields["sql_executed"] = sql_executed
    if not getattr(ctx.tracer, "_context", {}).get("node"):
        fields["node"] = "analysis"
    ctx.tracer.write("tool_call", **fields)


# -- repeated-query key (R11) --------------------------------------------------------------


def normalize_sql(sql: str) -> str:
    """sqlglot canonical form (bigquery dialect, normalize=True, comments dropped, pretty=False).
    Falls back to whitespace-collapsed lower-case text if parsing fails."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
        if len(statements) == 1:
            return statements[0].sql(dialect=DIALECT, normalize=True, normalize_functions="upper",
                                     comments=False, pretty=False)
    except (ParseError, TokenError):
        pass
    return " ".join(sql.split()).lower().rstrip(";").strip()


# -- PII scans (DD §8.3; calibrated in docs/facts.md §7) -----------------------------------


@dataclass(frozen=True)
class PiiHit:
    kind: Literal["EMAIL", "PHONE", "ADDRESS", "CARD"]
    column: str | None


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Stricter than input masking: product names hold digit runs such as `14-16-18-20-22`.
_PHONES = (
    re.compile(r"\+\d{1,3}[\s-]?\(?\d{1,4}\)?(?:[\s-]?\d{2,4}){2,4}"),
    re.compile(r"\(\d{3}\)\s?\d{3}[\s-]\d{4}"),
    re.compile(r"\b\d{3}[-.]\d{3}[-.]\d{4}\b"),
)
_ADDRESS = re.compile(
    r"(?i)\b\d{1,6}\s+(?:[A-Za-z]+\s+){1,3}(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|"
    r"drive|dr|way|court|ct|place|pl|park|parkway|trail|motorway|causeway|square|valley|run|landing|"
    r"lodge|corner|port|falls|vista|prairie|cliffs|mountains|orchard|keys|heights|terrace|crescent|"
    r"circle|highway|hwy|plaza|union|dam|flat|fall|parks|streets|squares)s?\.?"
    r"(?:,|\s+(?:apt|suite|unit)\.?\s*\d+|$)",
    re.MULTILINE,
)


def _scan(text: str) -> list[tuple[str, int]]:
    """(kind, number of matches) for each kind found in text."""
    found = [("EMAIL", len(_EMAIL.findall(text)))]
    phone_spans: set[tuple[int, int]] = set()
    for pattern in _PHONES:
        phone_spans |= {m.span() for m in pattern.finditer(text)}
    # Overlapping matches of different phone patterns count once.
    merged = []
    for start, end in sorted(phone_spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    found.append(("PHONE", len(merged)))
    found.append(("ADDRESS", len(_ADDRESS.findall(text))))
    return [(kind, n) for kind, n in found if n]


def scan_rows_for_pii(rows: Sequence[Mapping[str, Any]]) -> list[PiiHit]:
    """One hit per (cell, kind). String cells only: numbers are never contact data."""
    hits = []
    for row in rows:
        for column, value in row.items():
            if isinstance(value, str):
                hits += [PiiHit(kind=kind, column=column) for kind, _ in _scan(value)]  # type: ignore[arg-type]
    return hits


def scan_text_for_pii(text: str) -> list[PiiHit]:
    """One hit per match, for the final answer check."""
    return [PiiHit(kind=kind, column=None) for kind, n in _scan(text) for _ in range(n)]  # type: ignore[arg-type]
