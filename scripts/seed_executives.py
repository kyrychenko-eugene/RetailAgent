"""Seed the demo executives (DD §7, docs/facts.md §5).

    python scripts/seed_executives.py [--dry-run]

Brands are computed live: the top 6 brands by counted revenue to date; brand_manager_1 gets
ranks 1, 3, 5 and brand_manager_2 gets ranks 2, 4, 6. Idempotent: executives are upserted and
each manager's brand rows are replaced in one transaction. Needs GOOGLE_CLOUD_PROJECT and ADC.
"""
from __future__ import annotations

import argparse
import sys
from typing import Sequence

from retail_agent.bq_client import BigQueryRunner, create_runner
from retail_agent.config import ConfigError, Settings, load_settings
from retail_agent.security.identity import IdentityStore

CEO = ("ceo", "Morgan Lee (CEO)")
MANAGERS = (
    ("brand_manager_1", "Sam Rivera (Brand Manager)"),
    ("brand_manager_2", "Alex Chen (Brand Manager)"),
)
TOP_N = 6


def top_brands_sql(dataset: str, n: int = TOP_N) -> str:
    """Trusted seed query over the base tables (never model SQL)."""
    return f"""
SELECT p.brand, ROUND(SUM(oi.sale_price), 2) AS revenue
FROM `{dataset}.order_items` AS oi
JOIN `{dataset}.products` AS p ON p.id = oi.product_id
WHERE oi.status NOT IN ('Cancelled', 'Returned')
  AND oi.created_at <= CURRENT_TIMESTAMP()
  AND p.brand IS NOT NULL
GROUP BY p.brand
ORDER BY revenue DESC, p.brand ASC
LIMIT {n}
"""


def split_brands(ranked: Sequence[str]) -> tuple[list[str], list[str]]:
    """Ranks 1, 3, 5 -> manager 1; ranks 2, 4, 6 -> manager 2 (non-overlapping)."""
    if len(ranked) < TOP_N:
        raise ValueError(f"Need {TOP_N} ranked brands, got {len(ranked)}")
    top = list(ranked[:TOP_N])
    return top[0::2], top[1::2]


def fetch_ranked_brands(runner: BigQueryRunner, settings: Settings) -> list[str]:
    result = runner.run_query(
        top_brands_sql(settings.bigquery.dataset),
        max_bytes_billed=settings.bigquery.bytes_cap,
        timeout_s=settings.bigquery.query_timeout_s,
        labels={"app": "retail_agent", "exec_id": "seed"},
    )
    return [row["brand"] for row in result.rows]


def seed(store: IdentityStore, brands_1: Sequence[str], brands_2: Sequence[str]) -> None:
    store.upsert_executive(*CEO, scope_all=True)
    for (exec_id, name), brands in zip(MANAGERS, (brands_1, brands_2)):
        store.upsert_executive(exec_id, name, scope_all=False, brands=brands)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="compute and print, do not write")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        runner = create_runner(settings)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    ranked = fetch_ranked_brands(runner, settings)
    brands_1, brands_2 = split_brands(ranked)
    print(f"{CEO[0]:<16} {CEO[1]:<28} all brands")
    for (exec_id, name), brands in zip(MANAGERS, (brands_1, brands_2)):
        print(f"{exec_id:<16} {name:<28} {', '.join(brands)}")
    if args.dry_run:
        print("Dry run: nothing written.")
        return 0
    store = IdentityStore(settings.paths.app_db)
    seed(store, brands_1, brands_2)
    print(f"Seeded {len(MANAGERS) + 1} executives into {settings.paths.app_db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
