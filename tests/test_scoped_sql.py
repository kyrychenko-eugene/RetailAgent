"""Scoped CTE injection and row-limit wrapping (DD §8.1, §8.3)."""
from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from retail_agent.security.identity import Scope
from retail_agent.security.scoped_sql import (CTE_ORDER, ScopedSqlError, build_scoped_sql,
                                              wrap_with_limit)

BM = Scope.of(["Calvin Klein", "Levi's"])


def _ctes(sql: str) -> dict[str, exp.Select]:
    tree = sqlglot.parse_one(sql, read="bigquery")
    return {c.alias_or_name: c.this for c in tree.args["with_"].expressions}


def test_brand_scope_uses_query_parameter(catalog):
    scoped = build_scoped_sql("SELECT COUNT(*) AS n FROM products", BM, catalog)
    assert scoped.params == {"brands": ["Calvin Klein", "Levi's"]}
    ctes = _ctes(scoped.sql)
    assert list(ctes)[:4] == list(CTE_ORDER)
    assert "IN UNNEST(@brands)" in ctes["products"].sql(dialect="bigquery")
    assert "Calvin" not in scoped.sql and "Levi" not in scoped.sql      # never string-concatenated


def test_ceo_has_no_brand_filter_but_same_columns_and_filters(catalog):
    ceo = build_scoped_sql("SELECT 1 AS x", Scope.everything(), catalog)
    bm = build_scoped_sql("SELECT 1 AS x", BM, catalog)
    assert ceo.params == {} and "@brands" not in ceo.sql
    ceo_ctes, bm_ctes = _ctes(ceo.sql), _ctes(bm.sql)
    for name in CTE_ORDER:
        assert ceo_ctes[name].named_selects == bm_ctes[name].named_selects
        assert ceo_ctes[name].named_selects == [c.name for c in catalog.tables[name].columns]
    # Future-dated items are removed for every scope; orders/users follow order_items.
    for ctes in (ceo_ctes, bm_ctes):
        assert "created_at <= CURRENT_TIMESTAMP()" in ctes["order_items"].sql(dialect="bigquery")
        assert ctes["order_items"].args["joins"][0].this.name == "products"
        assert "FROM order_items" in ctes["orders"].sql(dialect="bigquery")
        assert "FROM order_items" in ctes["users"].sql(dialect="bigquery")


def test_no_pii_column_in_generated_ctes(catalog):
    sql = build_scoped_sql("SELECT 1 AS x", Scope.everything(), catalog).sql.lower()
    for column in catalog.blocked_columns:
        assert column not in sql


def test_model_with_clause_is_kept_after_generated_ctes(catalog):
    model = ("WITH sales AS (SELECT * FROM order_items), c AS (SELECT DISTINCT user_id FROM sales) "
             "SELECT COUNT(*) AS n FROM c")
    names = list(_ctes(build_scoped_sql(model, BM, catalog).sql))
    assert names == [*CTE_ORDER, "sales", "c"]


def test_union_and_nested_with(catalog):
    union = build_scoped_sql("SELECT id FROM products UNION ALL SELECT id FROM order_items", BM, catalog)
    assert list(_ctes(union.sql))[:4] == list(CTE_ORDER)
    nested = build_scoped_sql("SELECT * FROM (WITH a AS (SELECT 1 AS x) SELECT x FROM a)", BM, catalog)
    assert list(_ctes(nested.sql)) == list(CTE_ORDER)


def test_unparseable_or_multiple_statements(catalog):
    with pytest.raises(ScopedSqlError):
        build_scoped_sql("SELECT FROM WHERE (", BM, catalog)
    with pytest.raises(ScopedSqlError):
        build_scoped_sql("SELECT 1; SELECT 2", BM, catalog)


@pytest.mark.parametrize("sql, expected", [
    ("SELECT a FROM x ORDER BY a", "SELECT a FROM x ORDER BY a LIMIT 201"),
    ("SELECT a FROM x LIMIT 10", "SELECT a FROM x LIMIT 10"),
    ("SELECT a FROM x ORDER BY a LIMIT 5000 OFFSET 2", "SELECT a FROM x ORDER BY a LIMIT 201 OFFSET 2"),
    ("SELECT 1 AS a UNION ALL SELECT 2", "SELECT 1 AS a UNION ALL SELECT 2 LIMIT 201"),
])
def test_wrap_with_limit(sql, expected):
    assert wrap_with_limit(sql, 200) == expected


def test_wrap_with_limit_parameter_limit_is_wrapped():
    assert wrap_with_limit("SELECT a FROM x LIMIT @limit", 200) == "SELECT * FROM (\nSELECT a FROM x LIMIT @limit\n) LIMIT 201"
