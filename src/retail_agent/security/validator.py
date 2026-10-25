"""SQL validator (contracts §2.3). Implemented in Stage 2."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from retail_agent.config import Catalog
from retail_agent.security.identity import Scope


class ReasonCode(StrEnum):
    PARSE_ERROR = "parse_error"
    MULTIPLE_STATEMENTS = "multiple_statements"
    NOT_SELECT = "not_select"
    FORBIDDEN_FUNCTION = "forbidden_function"
    INFORMATION_SCHEMA = "information_schema"
    QUALIFIED_TABLE = "qualified_table"
    UNKNOWN_TABLE = "unknown_table"
    RESERVED_CTE_NAME = "reserved_cte_name"
    PII_COLUMN = "pii_column"
    UNKNOWN_COLUMN = "unknown_column"
    OUT_OF_SCOPE_BRAND = "out_of_scope_brand"
    QUASI_IDENTIFIER = "quasi_identifier"


SECURITY_EVENT_CODES: frozenset[ReasonCode] = frozenset(
    set(ReasonCode) - {ReasonCode.PARSE_ERROR, ReasonCode.MULTIPLE_STATEMENTS,
                       ReasonCode.UNKNOWN_TABLE, ReasonCode.UNKNOWN_COLUMN}
)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason_code: ReasonCode | None
    model_message: str
    user_message: str | None
    security_event: bool
    details: dict[str, Any]


def validate_sql(sql: str, scope: Scope, catalog: Catalog) -> ValidationResult:
    raise NotImplementedError
