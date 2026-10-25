"""BigQuery integration (`pytest -m bq`). Needs ADC credentials and GOOGLE_CLOUD_PROJECT. Each call
bills at least 10 MiB per referenced table, so keep these few."""
from __future__ import annotations

import uuid

import pytest

from retail_agent.bq_client import create_runner
from retail_agent.config import load_catalog, load_settings

pytestmark = pytest.mark.bq

# get_table_schema() returns legacy type names; the catalog uses GoogleSQL names.
LEGACY_TYPES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}


@pytest.fixture(scope="module")
def settings():
    return load_settings()          # loads .env


@pytest.fixture(scope="module")
def runner(settings):
    return create_runner(settings)


def test_dry_run_reports_bytes_and_errors(runner, settings):
    ds = settings.bigquery.dataset
    ok = runner.dry_run(f"SELECT COUNT(*) AS n FROM `{ds}.orders` WHERE order_id IN UNNEST(@ids)",
                        {"ids": [1, 2, 3]})
    assert ok.ok and ok.bytes_processed and 0 < ok.bytes_processed < settings.bigquery.bytes_cap
    bad = runner.dry_run(f"SELECT no_such_column FROM `{ds}.orders`")
    assert not bad.ok and bad.bytes_processed is None and "no_such_column" in bad.error
    assert ds not in bad.error


def test_run_query_with_bytes_cap_and_labels(runner, settings):
    ds = settings.bigquery.dataset
    res = runner.run_query(f"SELECT COUNT(*) AS n FROM `{ds}.products` WHERE brand IN UNNEST(@b)",
                           params={"b": ["Calvin Klein"]}, max_bytes_billed=settings.bigquery.bytes_cap,
                           timeout_s=settings.bigquery.query_timeout_s, labels={"app": "retail_agent_test"})
    assert res.columns == ("n",) and res.rows[0]["n"] > 0 and res.job_id      # bytes_billed is 0 for a cached result
    from retail_agent.bq_client import BytesCapExceededError
    with pytest.raises(BytesCapExceededError):
        runner.run_query(f"SELECT SUM(sale_price), '{uuid.uuid4().hex}' AS nonce FROM `{ds}.order_items`",   # defeats the query cache
                         max_bytes_billed=1, timeout_s=30)


def test_catalog_matches_live_schema(runner, settings):
    """Drift test: every catalog column exists in its base table with the same type."""
    catalog = load_catalog(settings.paths.content_dir / "catalog.yaml")
    assert catalog.dataset == settings.bigquery.dataset
    for table in catalog.tables.values():
        live = {f["name"].lower(): LEGACY_TYPES.get(f["type"], f["type"]) for f in runner.get_table_schema(table.source)}
        for col in table.columns:
            assert col.name.lower() in live, f"{table.name}.{col.name} missing in {table.source}"
            assert live[col.name.lower()] == col.type, f"{table.name}.{col.name}: {live[col.name.lower()]} != {col.type}"
        for blocked in catalog.blocked_columns & set(live):
            assert table.name and blocked not in {c.name.lower() for c in table.columns}
