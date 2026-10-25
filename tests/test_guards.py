"""Execution guards with a fake BigQuery runner (DD §8.3, §10.1, §10.2, R10, R11)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from retail_agent.bq_client import (BigQueryBadRequestError, BigQueryTimeoutError,
                                    BigQueryUnavailableError, BytesCapExceededError, DryRunResult,
                                    QueryResult)
from retail_agent.security.guards import (EMPTY_RESULT_HINT, REPEATED_SUFFIX, FailureKind,
                                          QueryContext, QueryFailure, QuerySuccess, column_values,
                                          normalize_sql, run_scoped_query)
from retail_agent.security.identity import Scope
from retail_agent.security.validator import ReasonCode
from retail_agent.tracing import validate_record

BM = Scope.of(["Calvin Klein", "Levi's"])
SQL = "SELECT status, COUNT(*) AS n FROM order_items GROUP BY status"


class FakeDeadline:
    def __init__(self, remaining: float) -> None:
        self._remaining = remaining

    def remaining(self) -> float:
        return self._remaining

    def expired(self) -> bool:
        return self._remaining <= 0


@dataclass
class FakeRunner:
    rows: list[dict] = field(default_factory=lambda: [{"status": "Complete", "n": 3}])
    dry: DryRunResult = field(default_factory=lambda: DryRunResult(ok=True, bytes_processed=10_000_000, error=None))
    run_error: Exception | None = None
    dry_error: Exception | None = None
    dry_calls: list = field(default_factory=list)
    run_calls: list = field(default_factory=list)

    def dry_run(self, sql, params=None):
        self.dry_calls.append((sql, params))
        if self.dry_error:
            raise self.dry_error
        return self.dry

    def run_query(self, sql, *, params=None, max_bytes_billed, timeout_s, labels=None):
        self.run_calls.append(dict(sql=sql, params=params, max_bytes_billed=max_bytes_billed,
                                   timeout_s=timeout_s, labels=labels))
        if self.run_error:
            raise self.run_error
        columns = tuple(self.rows[0]) if self.rows else ()
        return QueryResult(columns=columns, rows=list(self.rows), bytes_processed=10_000_000,
                           bytes_billed=41_943_040, job_id="job", elapsed_ms=5)


@pytest.fixture
def make_ctx(settings, catalog, fake_tracer):
    def make(runner=None, scope=BM, remaining=60.0):
        return QueryContext(scope=scope, exec_id="bm", thread_id="t" * 32, turn_id="tttttttt-0001",
                            deadline=FakeDeadline(remaining), settings=settings, catalog=catalog,
                            bq=runner or FakeRunner(), tracer=fake_tracer)
    return make


def _tool_calls(tracer):
    return [r for r in tracer.records if r["event"] == "tool_call"]


def _security(tracer):
    return [r for r in tracer.records if r["event"] == "security_event"]


def test_success_path(make_ctx, settings, fake_tracer):
    runner = FakeRunner()
    ctx = make_ctx(runner)
    out = run_scoped_query(SQL, ctx)
    assert isinstance(out, QuerySuccess)
    assert out.query_id == "Q1" and out.row_count == 1 and out.hint is None and out.sql == SQL
    call = runner.run_calls[0]
    assert call["params"] == {"brands": ["Calvin Klein", "Levi's"]}
    assert call["max_bytes_billed"] == settings.bigquery.bytes_cap
    assert call["timeout_s"] == min(settings.bigquery.query_timeout_s, 60 - settings.turn.synthesis_reserve_s)
    assert call["labels"] == {"thread_id": "t" * 32, "turn_id": "tttttttt-0001", "exec_id": "bm",
                              "app": "retail_agent"}
    assert call["sql"].startswith("WITH products AS") and call["sql"].endswith(f"LIMIT {settings.bigquery.max_rows_to_model + 1}")
    assert runner.dry_calls[0][0] == call["sql"]
    [record] = _tool_calls(fake_tracer)
    assert record["outcome"] == "ok" and record["sql"] == SQL and record["sql_executed"] == call["sql"]
    assert record["rows"] == 1 and record["bytes_scanned"] == 10_000_000 and record["tool"] == "run_sql"
    assert validate_record(record) == []


def test_empty_result_has_hint(make_ctx, fake_tracer):
    out = run_scoped_query(SQL, make_ctx(FakeRunner(rows=[])))
    assert isinstance(out, QuerySuccess) and out.row_count == 0 and out.hint == EMPTY_RESULT_HINT
    assert _tool_calls(fake_tracer)[0]["outcome"] == "empty"


def test_long_cells_are_truncated(make_ctx, settings):
    long = "x" * (settings.bigquery.max_cell_chars + 50)
    out = run_scoped_query(SQL, make_ctx(FakeRunner(rows=[{"status": long, "n": 1}])))
    assert len(out.rows[0]["status"]) == settings.bigquery.max_cell_chars + 1


@pytest.mark.parametrize("user_requested", [True, False])
def test_row_limit(make_ctx, settings, user_requested):
    n = settings.bigquery.max_rows_to_model
    rows = [{"status": str(i), "n": i} for i in range(n + 1)]
    out = run_scoped_query(SQL, make_ctx(FakeRunner(rows=rows)), user_requested_granularity=user_requested)
    assert isinstance(out, QueryFailure) and out.kind == FailureKind.ROW_LIMIT and not out.terminal
    if user_requested:
        assert f"more than {n} rows" in out.user_message and "aggregate" in out.user_message
    else:
        assert out.user_message is None and "Rewrite it with aggregation" in out.model_message


def test_bytes_cap_from_dry_run_does_not_execute(make_ctx, settings):
    runner = FakeRunner(dry=DryRunResult(ok=True, bytes_processed=settings.bigquery.bytes_cap + 1, error=None))
    out = run_scoped_query(SQL, make_ctx(runner))
    assert out.kind == FailureKind.BYTES_CAP and out.terminal and out.user_message
    assert runner.run_calls == []


def test_bytes_cap_from_execution(make_ctx):
    out = run_scoped_query(SQL, make_ctx(FakeRunner(run_error=BytesCapExceededError("cap"))))
    assert out.kind == FailureKind.BYTES_CAP and out.terminal


def test_dry_run_error_goes_back_to_model(make_ctx, fake_tracer):
    runner = FakeRunner(dry=DryRunResult(ok=False, bytes_processed=None, error="Unrecognized name: foo"))
    out = run_scoped_query(SQL, make_ctx(runner))
    assert out.kind == FailureKind.BAD_REQUEST and not out.terminal and "Unrecognized name" in out.model_message
    assert _tool_calls(fake_tracer)[0]["error_type"] == "bq_bad_request"


@pytest.mark.parametrize("error, kind, terminal", [
    (BigQueryTimeoutError("slow"), FailureKind.TIMEOUT, False),
    (BigQueryUnavailableError("down"), FailureKind.UNAVAILABLE, True),
    (BigQueryBadRequestError("bad"), FailureKind.BAD_REQUEST, False),
])
def test_execution_errors(make_ctx, error, kind, terminal):
    out = run_scoped_query(SQL, make_ctx(FakeRunner(run_error=error)))
    assert out.kind == kind and out.terminal is terminal
    if terminal:
        assert out.user_message


def test_dry_run_unavailable_is_terminal(make_ctx):
    out = run_scoped_query(SQL, make_ctx(FakeRunner(dry_error=BigQueryUnavailableError("down"))))
    assert out.kind == FailureKind.UNAVAILABLE and out.terminal


def test_deadline_below_reserve_does_not_start(make_ctx, settings):
    runner = FakeRunner()
    out = run_scoped_query(SQL, make_ctx(runner, remaining=settings.turn.synthesis_reserve_s - 1))
    assert out.kind == FailureKind.DEADLINE and out.terminal
    assert runner.dry_calls == [] and runner.run_calls == []


def test_pii_in_result_is_withheld(make_ctx, fake_tracer):
    out = run_scoped_query(SQL, make_ctx(FakeRunner(rows=[{"status": "mail jane@example.com", "n": 1}])))
    assert out.kind == FailureKind.PII_IN_RESULT and out.security_event
    assert "jane" not in out.model_message
    [event] = _security(fake_tracer)
    assert event["kind"] == "pii_in_result" and event["detail"]["kinds"] == ["EMAIL"]
    assert "jane" not in str(fake_tracer.records)


def test_out_of_scope_brand_is_terminal_with_user_message(make_ctx, fake_tracer):
    runner = FakeRunner()
    out = run_scoped_query("SELECT COUNT(*) AS n FROM products WHERE brand = 'Nike'", make_ctx(runner))
    assert out.kind == FailureKind.OUT_OF_SCOPE_BRAND and out.terminal and out.security_event
    assert out.user_message == "You don't have access to brand Nike."
    assert out.reason_code == ReasonCode.OUT_OF_SCOPE_BRAND
    assert runner.dry_calls == []
    [event] = _security(fake_tracer)
    assert event["kind"] == "out_of_scope_brand" and event["reason_code"] == "out_of_scope_brand"
    assert validate_record(event) == []


def test_ceo_skips_brand_check(make_ctx):
    out = run_scoped_query("SELECT COUNT(*) AS n FROM products WHERE brand = 'Nike'",
                           make_ctx(scope=Scope.everything()))
    assert isinstance(out, QuerySuccess)


def test_repeated_pii_attempts_stop_the_turn(make_ctx, settings, fake_tracer):
    ctx = make_ctx()
    first = run_scoped_query("SELECT email FROM users", ctx)
    assert first.kind == FailureKind.VALIDATION and not first.terminal and first.security_event
    assert "email" in first.model_message and "PII" not in first.model_message
    second = run_scoped_query("SELECT first_name FROM users", ctx)
    assert settings.turn.pii_attempts_before_stop == 2
    assert second.kind == FailureKind.PII_REPEATED and second.terminal and second.user_message
    kinds = [e["kind"] for e in _security(fake_tracer)]
    assert kinds == ["pii_column", "pii_column", "repeated_pii_attempt"]


def test_validation_failure_is_neutral_and_not_executed(make_ctx, fake_tracer):
    runner = FakeRunner()
    out = run_scoped_query("SELECT * FROM events", make_ctx(runner))
    assert out.kind == FailureKind.VALIDATION and not out.terminal and not out.security_event
    assert out.reason_code == ReasonCode.UNKNOWN_TABLE and runner.dry_calls == []
    assert _security(fake_tracer) == []
    assert _tool_calls(fake_tracer)[0]["error_type"] == "validation"


def test_repeated_success_is_returned_from_store(make_ctx, fake_tracer):
    runner = FakeRunner()
    ctx = make_ctx(runner)
    first = run_scoped_query(SQL, ctx)
    again = run_scoped_query("select status,  count(*) as n\nfrom order_items -- same\ngroup by status", ctx)
    assert isinstance(again, QuerySuccess) and again.cached and again.query_id == "Q2"
    assert again.rows == first.rows and len(runner.run_calls) == 1
    assert [r["outcome"] for r in _tool_calls(fake_tracer)] == ["ok", "cached"]


def test_repeated_failure_says_change_the_approach(make_ctx):
    runner = FakeRunner(dry=DryRunResult(ok=False, bytes_processed=None, error="bad"))
    ctx = make_ctx(runner)
    run_scoped_query(SQL, ctx)
    again = run_scoped_query(SQL, ctx)
    assert again.kind == FailureKind.REPEATED and again.model_message.endswith(REPEATED_SUFFIX)
    assert len(runner.dry_calls) == 1


def test_transient_failure_is_not_stored(make_ctx):
    runner = FakeRunner(run_error=BigQueryTimeoutError("slow"))
    ctx = make_ctx(runner)
    run_scoped_query(SQL, ctx)
    runner.run_error = None
    assert isinstance(run_scoped_query(SQL, ctx), QuerySuccess)


def test_column_values_builds_scoped_query_with_search_parameter(make_ctx, settings, fake_tracer):
    runner = FakeRunner(rows=[{"value": "Calvin Klein", "n": 10}])
    out = column_values("products.brand", make_ctx(runner), search="calv", limit=500)
    assert isinstance(out, QuerySuccess)
    call = runner.run_calls[0]
    assert call["params"] == {"brands": ["Calvin Klein", "Levi's"], "search": "calv"}
    assert "LOWER(@search)" in call["sql"] and "calv" not in call["sql"]
    assert call["sql"].endswith(f"LIMIT {settings.bigquery.column_values_limit}")   # capped
    assert _tool_calls(fake_tracer)[0]["tool"] == "get_column_values"


def test_column_values_repeated_check_includes_search(make_ctx):
    runner = FakeRunner(rows=[{"value": "x", "n": 1}])
    ctx = make_ctx(runner)
    column_values("products.name", ctx, search="jeans")
    column_values("products.name", ctx, search="shorts")
    column_values("products.name", ctx, search="jeans")
    assert len(runner.run_calls) == 2


@pytest.mark.parametrize("column, search, fragment", [
    ("products.name", None, "pass `search`"),
    ("users.email", None, "not a categorical column"),
    ("order_items.sale_price", None, "not a categorical column"),
    ("status", None, "not a categorical column"),
])
def test_column_values_rejections(make_ctx, column, search, fragment):
    runner = FakeRunner()
    out = column_values(column, make_ctx(runner), search=search)
    assert isinstance(out, QueryFailure) and out.kind == FailureKind.VALIDATION and fragment in out.model_message
    assert runner.dry_calls == []


def test_column_values_low_cardinality_without_search(make_ctx):
    runner = FakeRunner(rows=[{"value": "Complete", "n": 5}])
    out = column_values("order_items.status", make_ctx(runner))
    assert isinstance(out, QuerySuccess) and "search" not in runner.run_calls[0]["params"]


def test_normalize_sql():
    assert normalize_sql("select A from X -- c\nwhere b=1;") == normalize_sql("SELECT a FROM x WHERE b = 1")
    assert normalize_sql("SELECT FROM WHERE (") == "select from where ("
