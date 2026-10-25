"""Scoped CTE builder (contracts §2.2, DD §8.1).

The model writes SQL against the logical names products, order_items, orders and users.
This module prepends one CTE per name, generated from `catalog.yaml`: a brand-scoped,
PII-free subset of the public table. Injection goes through the sqlglot AST, so a model
query with its own WITH clause keeps working. The public dataset path appears only inside
the generated CTEs.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from retail_agent.bq_client import QueryParamValue
from retail_agent.config import Catalog, Table
from retail_agent.security.identity import Scope

DIALECT = "bigquery"
BRANDS_PARAM = "brands"
# Dependency order: order_items joins products; orders and users are derived from order_items.
CTE_ORDER: tuple[str, ...] = ("products", "order_items", "orders", "users")
_JOIN_ALIAS = "j"
_SELF_ALIAS = "t"


@dataclass(frozen=True)
class ScopedQuery:
    sql: str                                   # final SQL: generated CTEs + the model's query
    params: dict[str, QueryParamValue]         # {"brands": [...]} for a brand scope, {} for all


class ScopedSqlError(Exception): ...           # model SQL could not be parsed or rewritten


RESERVED_NAMES: frozenset[str] = frozenset({"products", "order_items", "orders", "users"})


def _qualify_unqualified(predicate: str, alias: str) -> exp.Expression:
    """Parse a catalog filter and attach `alias` to its bare columns, so the predicate stays
    unambiguous next to a join. Columns inside subqueries keep their own resolution."""
    node = sqlglot.parse_one(predicate, read=DIALECT)
    for col in list(node.find_all(exp.Column)):
        if not col.table and col.find_ancestor(exp.Subquery, exp.Select) is None:
            col.set("table", exp.to_identifier(alias))
    return node


def _cte_select(table: Table, catalog: Catalog, scope: Scope) -> exp.Select:
    source = exp.to_table(f"`{catalog.dataset}.{table.source}`", dialect=DIALECT)
    source.set("alias", exp.TableAlias(this=exp.to_identifier(_SELF_ALIAS)))
    select = exp.select(*[exp.column(c.name, table=_SELF_ALIAS) for c in table.columns]).from_(source)
    conditions: list[exp.Expression] = []
    if table.join:
        if table.join not in catalog.tables:
            raise ScopedSqlError(f"Catalog table '{table.name}' joins unknown table '{table.join}'")
        key = f"{table.join[:-1] if table.join.endswith('s') else table.join}_id"
        if catalog.column(table.name, key) is None:
            raise ScopedSqlError(f"Catalog table '{table.name}' has no join key '{key}'")
        select = select.join(
            exp.to_table(table.join).as_(_JOIN_ALIAS),
            on=exp.column("id", table=_JOIN_ALIAS).eq(exp.column(key, table=_SELF_ALIAS)),
            join_type="inner",
        )
    if table.scope_filter and not scope.all_brands:
        conditions.append(sqlglot.parse_one(
            f"{_SELF_ALIAS}.{table.scope_filter} IN UNNEST(@{BRANDS_PARAM})", read=DIALECT))
    if table.filter:
        conditions.append(_qualify_unqualified(table.filter, _SELF_ALIAS))
    if conditions:
        select = select.where(*conditions)
    return select


def generated_ctes(scope: Scope, catalog: Catalog) -> list[exp.CTE]:
    missing = [name for name in CTE_ORDER if name not in catalog.tables]
    if missing:
        raise ScopedSqlError(f"Catalog lacks tables {missing}")
    return [
        exp.CTE(this=_cte_select(catalog.tables[name], catalog, scope),
                alias=exp.TableAlias(this=exp.to_identifier(name)))
        for name in CTE_ORDER
    ]


def build_scoped_sql(model_sql: str, scope: Scope, catalog: Catalog) -> ScopedQuery:
    """Parse with sqlglot (dialect bigquery), prepend the four CTEs generated from the catalog
    (in dependency order) to the AST's WITH clause, with the model's own CTEs after them.
    Precondition: validate_sql(model_sql, ...) returned ok."""
    try:
        statements = [s for s in sqlglot.parse(model_sql, read=DIALECT) if s is not None]
    except (ParseError, TokenError) as exc:
        raise ScopedSqlError(f"SQL could not be parsed: {exc}") from None
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ScopedSqlError("Expected exactly one SELECT statement")
    query = statements[0]

    ctes = generated_ctes(scope, catalog)
    existing = query.args.get("with_") or query.args.get("with")
    recursive = False
    if existing is not None:
        own = list(existing.expressions)
        clash = [c.alias_or_name for c in own if c.alias_or_name.lower() in RESERVED_NAMES]
        if clash:
            raise ScopedSqlError(f"CTE names {clash} are reserved")
        ctes += own
        recursive = bool(existing.args.get("recursive"))
    with_key = "with_" if "with_" in query.arg_types else "with"
    query.set(with_key, exp.With(expressions=ctes, recursive=recursive))

    params: dict[str, QueryParamValue] = {} if scope.all_brands else {BRANDS_PARAM: list(scope.brands)}
    return ScopedQuery(sql=query.sql(dialect=DIALECT), params=params)


def wrap_with_limit(sql: str, max_rows: int) -> str:
    """Cap the rows returned to max_rows + 1 (DD §8.3), applied after build_scoped_sql.

    A literal top-level LIMIT is tightened in place, so the model's ORDER BY is kept for
    sure; otherwise the query is wrapped as `SELECT * FROM (<sql>) LIMIT max_rows + 1`."""
    cap = max_rows + 1
    try:
        query = sqlglot.parse_one(sql, read=DIALECT)
    except (ParseError, TokenError) as exc:
        raise ScopedSqlError(f"SQL could not be parsed: {exc}") from None
    limit = query.args.get("limit")
    if isinstance(query, (exp.Select, exp.SetOperation)):
        if limit is None:
            return query.limit(cap, copy=False).sql(dialect=DIALECT)
        value = limit.expression
        if isinstance(value, exp.Literal) and not value.is_string:
            if int(value.this) > cap:
                limit.set("expression", exp.Literal.number(cap))
            return query.sql(dialect=DIALECT)
    return f"SELECT * FROM (\n{sql}\n) LIMIT {cap}"
