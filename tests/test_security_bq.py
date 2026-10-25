"""Security core against live BigQuery (`pytest -m bq`; Stage 2 acceptance).

Executed queries bill ~40 MiB each (docs/facts.md §2), so there are few of them; dry runs are free."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from retail_agent.bq_client import create_runner
from retail_agent.config import REPO_ROOT, load_catalog, load_settings
from retail_agent.security.guards import QueryContext, QuerySuccess, column_values, run_scoped_query
from retail_agent.security.identity import Scope
from retail_agent.security.scoped_sql import build_scoped_sql, wrap_with_limit
from retail_agent.tracing import NullTracer

pytestmark = pytest.mark.bq

BM = Scope.of(["Calvin Klein", "Levi's"])
REVENUE = "SELECT ROUND(SUM(sale_price), 2) AS revenue FROM order_items WHERE status NOT IN ('Cancelled', 'Returned')"


class _Deadline:
    def remaining(self) -> float:
        return 600.0

    def expired(self) -> bool:
        return False


@pytest.fixture(scope="module")
def settings():
    return load_settings()          # loads .env


@pytest.fixture(scope="module")
def catalog(settings):
    return load_catalog(settings.paths.content_dir / "catalog.yaml")


@pytest.fixture(scope="module")
def runner(settings):
    return create_runner(settings)


def _ctx(scope, settings, catalog, runner):
    return QueryContext(scope=scope, exec_id="bq-test", thread_id="bqtest", turn_id="bqtest-0001",
                        deadline=_Deadline(), settings=settings, catalog=catalog, bq=runner,
                        tracer=NullTracer())


def test_brand_manager_never_sees_out_of_scope_brand(settings, catalog, runner):
    out = run_scoped_query(
        "SELECT p.brand, COUNT(*) AS items FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "GROUP BY p.brand", _ctx(BM, settings, catalog, runner))
    assert isinstance(out, QuerySuccess), out
    assert {r["brand"] for r in out.rows} == set(BM.brands)


def test_select_star_from_users_has_no_pii_column(settings, catalog, runner):
    # Trusted test code: the validator rejects SELECT * FROM users (quasi-identifier, QI-06),
    # so the scoped SQL is executed directly to prove the CTE itself carries no PII.
    scoped = build_scoped_sql("SELECT * FROM users LIMIT 3", BM, catalog)
    result = runner.run_query(scoped.sql, params=scoped.params, max_bytes_billed=settings.bigquery.bytes_cap,
                              timeout_s=60, labels={"app": "retail_agent", "exec_id": "bq-test"})
    columns = {c.lower() for c in result.columns}
    assert columns == {c.name for c in catalog.tables["users"].columns}
    assert not columns & catalog.blocked_columns
    assert len(result.rows) == 3


def test_same_aggregate_differs_between_ceo_and_manager(settings, catalog, runner):
    ceo = run_scoped_query(REVENUE, _ctx(Scope.everything(), settings, catalog, runner))
    bm = run_scoped_query(REVENUE, _ctx(BM, settings, catalog, runner))
    assert isinstance(ceo, QuerySuccess) and isinstance(bm, QuerySuccess)
    ceo_revenue, bm_revenue = ceo.rows[0]["revenue"], bm.rows[0]["revenue"]
    assert 0 < bm_revenue < ceo_revenue / 10      # one brand is < 2 % of the total (facts §1.4)


def test_column_values_live(settings, catalog, runner):
    out = column_values("products.brand", _ctx(BM, settings, catalog, runner), search="calv")
    assert isinstance(out, QuerySuccess), out
    assert [r["value"] for r in out.rows] == ["Calvin Klein"]


def _golden_sql() -> list[tuple[str, str]]:
    data = yaml.safe_load(Path(REPO_ROOT / "eval" / "golden_set.yaml").read_text(encoding="utf-8"))
    found = [(f"resolver:{name}", r["sql"]) for name, r in data["resolvers"].items() if r.get("sql")]
    found += [(c["id"], c["reference_sql"]) for c in data["cases"] if c.get("reference_sql")]
    return [(i, s) for i, s in found if not re.search(r"\{[a-z_]+\}", s)]   # placeholders need resolvers


def _accepted_cases() -> list[tuple[str, str]]:
    data = yaml.safe_load(Path(REPO_ROOT / "eval" / "security_cases.yaml").read_text(encoding="utf-8"))
    return [(c["id"], c["sql"]) for c in data["sql_cases"] if c["expect"]["ok"]]


@pytest.mark.parametrize("case_id, sql", _accepted_cases() + _golden_sql())
@pytest.mark.parametrize("scope", [BM, Scope.everything()], ids=["bm", "ceo"])
def test_rewritten_sql_is_valid_bigquery(case_id, sql, scope, settings, catalog, runner):
    """Dry runs are free: every accepted and golden query stays valid after CTE injection."""
    scoped = build_scoped_sql(sql, scope, catalog)
    final = wrap_with_limit(scoped.sql, settings.bigquery.max_rows_to_model)
    dry = runner.dry_run(final, scoped.params)
    assert dry.ok, f"{case_id}: {dry.error}"
    assert dry.bytes_processed < settings.bigquery.bytes_cap
