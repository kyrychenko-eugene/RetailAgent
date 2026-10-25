"""Scoped CTE builder (contracts §2.2). Implemented in Stage 2."""
from __future__ import annotations

from dataclasses import dataclass

from retail_agent.bq_client import QueryParamValue
from retail_agent.config import Catalog
from retail_agent.security.identity import Scope


@dataclass(frozen=True)
class ScopedQuery:
    sql: str                                   # final SQL: generated CTEs + the model's query
    params: dict[str, QueryParamValue]         # {"brands": [...]} for a brand scope, {} for all


class ScopedSqlError(Exception): ...           # model SQL could not be parsed or rewritten


RESERVED_NAMES: frozenset[str] = frozenset({"products", "order_items", "orders", "users"})


def build_scoped_sql(model_sql: str, scope: Scope, catalog: Catalog) -> ScopedQuery:
    raise NotImplementedError


def wrap_with_limit(sql: str, max_rows: int) -> str:
    raise NotImplementedError
