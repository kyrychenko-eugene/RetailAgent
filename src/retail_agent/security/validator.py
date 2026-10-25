"""SQL validator (contracts §2.3, DD §8.2).

Runs on the model's SQL before the scoped CTEs are added. The CTEs are the core guarantee;
this is the second line of defense and the source of security events and user-facing
"no access" messages. Rules run in a fixed order and the first failure wins. The binding
cases are `eval/security_cases.yaml`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope as SqlScope
from sqlglot.optimizer.scope import build_scope, traverse_scope

from retail_agent.config import Catalog
from retail_agent.security.identity import Scope
from retail_agent.security.scoped_sql import DIALECT, RESERVED_NAMES


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
    details: dict[str, Any] = field(default_factory=dict)


_MSG_TABLES = "Only the tables products, order_items, orders and users can be queried, by those names."
_MSG_SINGLE_SELECT = "Only a single read-only SELECT statement is allowed."
_MODEL_MESSAGES: dict[ReasonCode, str] = {
    ReasonCode.PII_COLUMN: "Column `{col}` is not available. Use only the columns listed in the schema.",
    ReasonCode.UNKNOWN_COLUMN: "Column `{col}` is not available. Use only the columns listed in the schema.",
    ReasonCode.UNKNOWN_TABLE: _MSG_TABLES,
    ReasonCode.QUALIFIED_TABLE: _MSG_TABLES,
    ReasonCode.INFORMATION_SCHEMA: _MSG_TABLES,
    ReasonCode.RESERVED_CTE_NAME: "CTE name `{name}` is reserved; choose another name.",
    ReasonCode.NOT_SELECT: _MSG_SINGLE_SELECT,
    ReasonCode.MULTIPLE_STATEMENTS: _MSG_SINGLE_SELECT,
    ReasonCode.FORBIDDEN_FUNCTION: _MSG_SINGLE_SELECT,
    ReasonCode.OUT_OF_SCOPE_BRAND: (
        "Brand `{brand}` is outside this user's data. Do not retry with other brand filters; "
        "tell the user they have no access to it."
    ),
    ReasonCode.QUASI_IDENTIFIER: (
        "Customer IDs cannot be combined with demographic columns (age, gender, city, state, "
        "country, traffic_source). Return aggregates by demographic group instead, or customer "
        "IDs without demographics."
    ),
    ReasonCode.PARSE_ERROR: "SQL could not be parsed: {error}",
}

# Statements and clauses that are never allowed anywhere in the tree.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name) for name in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable",
        "Command", "Export", "Declare", "Set", "Use", "Grant", "Revoke", "Transaction",
        "Commit", "Rollback", "Copy", "LoadData", "Into",
    ) if hasattr(exp, name)
)
# Function names (upper-case) that reach outside the four logical tables.
_FORBIDDEN_FUNCTIONS = frozenset({"EXTERNAL_QUERY", "VECTOR_SEARCH", "APPENDS", "CHANGES",
                                  "EXTERNAL_OBJECT_TRANSFORM", "SESSION_USER"})
_FORBIDDEN_FUNCTION_PREFIXES = ("ML.", "AI.", "KEYS.", "NET.")

# Aggregates that return one of their input values; an identifier inside them still reaches
# the output (quasi-identifier rule). Every other aggregate collapses its input.
_VALUE_AGGREGATES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name) for name in (
        "AnyValue", "ArrayAgg", "ArrayConcatAgg", "ArrayUnionAgg", "ArrayUniqueAgg", "ArgMax",
        "ArgMin", "First", "FirstValue", "GroupConcat", "Last", "LastValue", "Lag", "Lead", "Max",
        "Min", "Mode", "NthValue", "ApproxTopK", "ApproxQuantiles", "PercentileDisc",
        "AnonymousAggFunc",
    ) if hasattr(exp, name)
)
_BRAND_WRAPPERS = (exp.Lower, exp.Upper, exp.Trim, exp.Paren, exp.Cast)


class _Reject(Exception):
    def __init__(self, code: ReasonCode, user_message: str | None = None, **details: Any) -> None:
        super().__init__(code.value)
        self.code = code
        self.user_message = user_message
        self.details = details


def _ok() -> ValidationResult:
    return ValidationResult(ok=True, reason_code=None, model_message="", user_message=None,
                            security_event=False, details={})


def _result(rej: _Reject) -> ValidationResult:
    fmt = {"col": rej.details.get("column", ""), "name": rej.details.get("name", ""),
           "brand": ", ".join(rej.details.get("brands", [])), "error": rej.details.get("error", "")}
    return ValidationResult(
        ok=False,
        reason_code=rej.code,
        model_message=_MODEL_MESSAGES[rej.code].format(**fmt),
        user_message=rej.user_message,
        security_event=rej.code in SECURITY_EVENT_CODES,
        details=dict(rej.details),
    )


def validate_sql(sql: str, scope: Scope, catalog: Catalog) -> ValidationResult:
    try:
        tree = _check_statement(sql)
        _check_functions(tree)
        _check_tables(tree, catalog)
        _check_reserved_ctes(tree)
        qualified = _check_columns(tree, catalog)
        if not scope.all_brands:
            _check_brand_literals(tree, scope)
        _check_quasi_identifiers(qualified, catalog)
    except _Reject as rej:
        return _result(rej)
    return _ok()


# -- statement shape -------------------------------------------------------------------


def _check_statement(sql: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except (ParseError, TokenError) as exc:
        raise _Reject(ReasonCode.PARSE_ERROR, error=_short_error(exc)) from None
    if not statements:
        raise _Reject(ReasonCode.PARSE_ERROR, error="empty statement")
    if len(statements) > 1:
        raise _Reject(ReasonCode.MULTIPLE_STATEMENTS, count=len(statements))
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise _Reject(ReasonCode.NOT_SELECT, statement=type(tree).__name__)
    bad = next(tree.find_all(*_FORBIDDEN_NODES), None) if _FORBIDDEN_NODES else None
    if bad is not None:
        raise _Reject(ReasonCode.NOT_SELECT, statement=type(bad).__name__)
    return tree


def _short_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if errors:
        first = errors[0]
        return f"{first.get('description', '')} (line {first.get('line')}, col {first.get('col')})".strip()
    return " ".join(str(exc).split())[:300]


# -- functions and tables --------------------------------------------------------------


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).upper()
    return (node.sql_name() if isinstance(node, exp.Func) else type(node).__name__).upper()


def _check_functions(tree: exp.Expression) -> None:
    for table in tree.find_all(exp.Table):
        parts = [table.catalog, table.db, table.name]
        if any("information_schema" in (p or "").lower() for p in parts):
            raise _Reject(ReasonCode.INFORMATION_SCHEMA)
        if not isinstance(table.this, exp.Identifier):
            # Table-valued function in FROM: EXTERNAL_QUERY(...), ML.PREDICT(...), ...
            raise _Reject(ReasonCode.FORBIDDEN_FUNCTION, function=_function_name(table.this))
    for func in tree.find_all(exp.Func):
        name = _function_name(func)
        parent = func.parent
        if isinstance(parent, exp.Dot) and isinstance(parent.this, (exp.Var, exp.Identifier, exp.Column)):
            name = f"{parent.this.name.upper()}.{name}"
        if name in _FORBIDDEN_FUNCTIONS or name.startswith(_FORBIDDEN_FUNCTION_PREFIXES):
            raise _Reject(ReasonCode.FORBIDDEN_FUNCTION, function=name)


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}


def _check_tables(tree: exp.Expression, catalog: Catalog) -> None:
    allowed = set(RESERVED_NAMES) | set(catalog.tables) | _cte_names(tree)
    for table in tree.find_all(exp.Table):
        if table.catalog or table.db or "." in table.name:
            raise _Reject(ReasonCode.QUALIFIED_TABLE, table=table.sql(dialect=DIALECT))
        if table.name.lower() not in allowed:
            raise _Reject(ReasonCode.UNKNOWN_TABLE, table=table.name)


def _check_reserved_ctes(tree: exp.Expression) -> None:
    for cte in tree.find_all(exp.CTE):
        if cte.alias_or_name.lower() in RESERVED_NAMES:
            raise _Reject(ReasonCode.RESERVED_CTE_NAME, name=cte.alias_or_name)


# -- columns ---------------------------------------------------------------------------


def _schema(catalog: Catalog) -> dict[str, dict[str, str]]:
    return {name: {c.name.lower(): c.type for c in t.columns} for name, t in catalog.tables.items()}


def _check_columns(tree: exp.Expression, catalog: Catalog) -> exp.Expression | None:
    """Resolve every column to its source; reject unresolved ones (PII names first).
    Returns the qualified tree used by the quasi-identifier rule."""
    try:
        qualified = qualify(tree.copy(), schema=_schema(catalog), dialect=DIALECT,
                            validate_qualify_columns=False, identify=False, quote_identifiers=False)
        unresolved = _unresolved_columns(qualified, catalog)
    except OptimizeError as exc:
        # sqlglot gives up on a column qualified with an alias whose source lacks it
        # (`u.first_name`, `x.street_address`). Fall back to the raw names.
        blocked = sorted({c.name.lower() for c in tree.find_all(exp.Column)} & catalog.blocked_columns)
        if blocked:
            raise _Reject(ReasonCode.PII_COLUMN, column=blocked[0]) from None
        match = re.search(r"Unknown column: (\S+)", str(exc))
        raise _Reject(ReasonCode.UNKNOWN_COLUMN, column=match.group(1) if match else "?",
                      error=_short_error(exc)) from None
    blocked = [c for c in unresolved if c in catalog.blocked_columns]
    if blocked:
        raise _Reject(ReasonCode.PII_COLUMN, column=blocked[0])
    if unresolved:
        raise _Reject(ReasonCode.UNKNOWN_COLUMN, column=unresolved[0])
    return qualified


def _select_aliases(scope: SqlScope) -> set[str]:
    expr = scope.expression
    if isinstance(expr, exp.Select):
        return {s.lower() for s in expr.named_selects}
    return set()


def _unnest_names(scope: SqlScope) -> set[str]:
    names: set[str] = set()
    for unnest in scope.expression.find_all(exp.Unnest):
        alias = unnest.args.get("alias")
        if alias is not None:
            if alias.name:
                names.add(alias.name.lower())
            names.update(c.name.lower() for c in alias.columns)
        offset = unnest.args.get("offset")
        if isinstance(offset, exp.Expression) and offset.name:
            names.add(offset.name.lower())
    return names


def _in_projection(node: exp.Expression, scope: SqlScope) -> bool:
    expr = scope.expression
    if not isinstance(expr, exp.Select):
        return False
    projections = {id(p) for p in expr.expressions}
    while node is not None and node is not expr:
        if id(node) in projections:
            return True
        node = node.parent
    return False


def _scope_outputs(scope: SqlScope) -> set[str] | None:
    """Output column names of a derived table / CTE scope; None if unknown (unexpanded *)."""
    expr = scope.expression
    selects = list(expr.selects) if isinstance(expr, (exp.Select, exp.SetOperation)) else []
    if not selects or any(isinstance(s, exp.Star) or (isinstance(s, exp.Column) and s.is_star)
                          for s in selects):
        return None
    return {s.lower() for s in expr.named_selects}


def _unresolved_columns(qualified: exp.Expression, catalog: Catalog) -> list[str]:
    allowed = catalog.allowed_columns()
    unresolved: list[str] = []
    for scope in traverse_scope(qualified):
        aliases = _select_aliases(scope)
        local_names = _unnest_names(scope) | {s.lower() for s in scope.sources}
        for col in scope.columns:
            name = col.name.lower()
            if not name or col.is_star:
                continue
            if not col.table:
                # Output aliases are visible in GROUP BY / ORDER BY / HAVING, not in the
                # SELECT list itself (where qualify() aliases a bare column to its own name).
                alias_ref = name in aliases and not _in_projection(col, scope)
                if not alias_ref and name not in local_names:
                    unresolved.append(name)
                continue
            source = scope.sources.get(col.table)
            if isinstance(source, exp.Table):
                table = source.name.lower()
                if table in allowed and name not in allowed[table]:
                    unresolved.append(name)
            elif isinstance(source, SqlScope):
                outputs = _scope_outputs(source)
                if outputs is not None and name not in outputs:
                    unresolved.append(name)
    return unresolved


# -- brand literals (DD §8.2, Q37) -----------------------------------------------------


def _is_brand_expr(node: exp.Expression | None) -> bool:
    while isinstance(node, _BRAND_WRAPPERS):
        node = node.this
    return isinstance(node, exp.Column) and node.name.lower() == "brand"


def _string_literals(nodes: Iterable[exp.Expression]) -> list[str] | None:
    values = []
    for node in nodes:
        if not (isinstance(node, exp.Literal) and node.is_string):
            return None
        values.append(node.this)
    return values


def _like_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.compile("".join(out), re.IGNORECASE | re.DOTALL)


def _check_brand_literals(tree: exp.Expression, scope: Scope) -> None:
    out_of_scope: list[str] = []
    for node in tree.find_all(exp.EQ, exp.In, exp.Like):
        if isinstance(node.parent, exp.Not):
            continue          # negative predicates never select another brand's rows
        if isinstance(node, exp.EQ):
            left, right = node.this, node.expression
            if _is_brand_expr(left):
                values = _string_literals([right])
            elif _is_brand_expr(right):
                values = _string_literals([left])
            else:
                continue
            out_of_scope += [v for v in values or [] if not scope.allows(v)]
        elif isinstance(node, exp.In):
            if not _is_brand_expr(node.this):
                continue
            items = list(node.expressions)
            unnest = node.args.get("unnest")
            if unnest is not None:
                items = [e for arr in unnest.expressions if isinstance(arr, exp.Array) for e in arr.expressions]
            out_of_scope += [v for v in _string_literals(items) or [] if not scope.allows(v)]
        else:
            if not _is_brand_expr(node.this):
                continue
            patterns = _string_literals([node.expression])
            for pattern in patterns or []:
                regex = _like_regex(pattern)
                if not any(regex.fullmatch(b) for b in scope.brands):
                    out_of_scope.append(pattern.strip("%") or pattern)
    if out_of_scope:
        brands = list(dict.fromkeys(out_of_scope))
        raise _Reject(ReasonCode.OUT_OF_SCOPE_BRAND,
                      user_message=f"You don't have access to brand {', '.join(brands)}.",
                      brands=brands)


# -- quasi-identifiers (DD §8.2, §8.4, R12) ----------------------------------------------


def _select_scopes(scope: SqlScope) -> list[SqlScope]:
    if isinstance(scope.expression, exp.SetOperation):
        return [s for child in scope.set_operation_scopes for s in _select_scopes(child)]
    return [scope]


def _projection(scope: SqlScope, name: str) -> exp.Expression | None:
    expr = scope.expression
    if not isinstance(expr, exp.Select):
        return None
    return next((p for p in expr.expressions if p.alias_or_name.lower() == name), None)


Origin = tuple[str, str]          # (logical table, column), lower-case


class _Lineage:
    """Which base columns reach an expression without being aggregated away."""

    _MAX_DEPTH = 50

    def __init__(self, catalog: Catalog) -> None:
        self.table_columns = {n: tuple(c.name.lower() for c in t.columns) for n, t in catalog.tables.items()}

    def origins(self, node: exp.Expression, scope: SqlScope, depth: int = 0) -> set[Origin]:
        found: set[Origin] = set()
        if depth > self._MAX_DEPTH:
            return found

        def walk(n: exp.Expression) -> None:
            if isinstance(n, exp.AggFunc) and not isinstance(n, _VALUE_AGGREGATES):
                return
            if isinstance(n, exp.Window):
                walk(n.this)          # PARTITION BY / ORDER BY do not reach the value
                return
            if isinstance(n, exp.Exists):
                return                # boolean
            if isinstance(n, exp.Subquery):
                # Scalar subquery: its output reaches this value (correlated lookups).
                child = next((c for c in scope.subquery_scopes if c.expression is n.this), None)
                if child is not None:
                    found.update(self.scope_origins(child, depth + 1))
                return
            if isinstance(n, exp.Column):
                found.update(self.column_origins(n, scope, depth))
                return
            if isinstance(n, exp.TableColumn):
                # qualify() turns a whole-row reference (TO_JSON_STRING(u), SELECT u) into this.
                found.update(self.source_origins(scope.sources.get(n.name.lower()), depth))
                return
            for child_node in n.iter_expressions():
                walk(child_node)

        walk(node)
        return found

    def scope_origins(self, scope: SqlScope, depth: int) -> set[Origin]:
        """Everything a derived table, CTE or subquery outputs."""
        result: set[Origin] = set()
        for select_scope in _select_scopes(scope):
            for projection in select_scope.expression.expressions:
                result |= self.origins(projection, select_scope, depth + 1)
        return result

    def source_origins(self, source: Any, depth: int) -> set[Origin]:
        if isinstance(source, exp.Table):
            table = source.name.lower()
            return {(table, c) for c in self.table_columns.get(table, ())}
        return self.scope_origins(source, depth) if isinstance(source, SqlScope) else set()

    def column_origins(self, col: exp.Column, scope: SqlScope, depth: int = 0) -> set[Origin]:
        name = col.name.lower()
        if not col.table:
            if name in scope.sources:
                # Whole-row reference such as TO_JSON_STRING(u) or SELECT u: every column of u.
                return self.source_origins(scope.sources[name], depth)
            # An output alias referenced in GROUP BY / ORDER BY.
            proj = _projection(scope, name)
            if proj is not None and not any(c is col for c in proj.find_all(exp.Column)):
                return self.origins(proj, scope, depth + 1)
            return set()
        source = scope.sources.get(col.table)
        if isinstance(source, exp.Table):
            return {(source.name.lower(), name)}
        if isinstance(source, SqlScope):
            result: set[Origin] = set()
            for child in _select_scopes(source):
                proj = _projection(child, name)
                if proj is not None:
                    result |= self.origins(proj, child, depth + 1)
            return result
        return set()


def _is_constant(node: exp.Expression) -> bool:
    return node.find(exp.Column, exp.Query, exp.Subquery) is None


def _is_literal_filter(col: exp.Column) -> bool:
    """`col = <constant>`, `col IN (<constants>)`, `col IN UNNEST([...])` or BETWEEN constants."""
    node: exp.Expression = col
    parent = node.parent
    while isinstance(parent, (exp.Paren, exp.Cast)):
        node, parent = parent, parent.parent
    if isinstance(parent, exp.EQ):
        other = parent.expression if parent.this is node else parent.this
        return _is_constant(other)
    if isinstance(parent, exp.In) and parent.this is node:
        if parent.args.get("query") is not None:
            return False
        unnest = parent.args.get("unnest")
        items = list(parent.expressions) + ([unnest] if unnest is not None else [])
        return bool(items) and all(_is_constant(i) for i in items)
    if isinstance(parent, exp.Between) and parent.this is node:
        return _is_constant(parent.args["low"]) and _is_constant(parent.args["high"])
    return False


def _check_quasi_identifiers(qualified: exp.Expression | None, catalog: Catalog) -> None:
    """Customer identifiers (output, GROUP BY or literal filter) never next to demographic output."""
    if qualified is None:
        return
    root = build_scope(qualified)
    if root is None:
        return
    lineage = _Lineage(catalog)
    ids = {(n, c.name.lower()) for n, t in catalog.tables.items() for c in t.columns if c.customer_identifier}
    demo = {(n, c.name.lower()) for n, t in catalog.tables.items() for c in t.columns if c.demographic}

    outputs: set[Origin] = set()
    grouped: set[Origin] = set()
    for select_scope in _select_scopes(root):
        select = select_scope.expression
        for projection in select.expressions:
            outputs |= lineage.origins(projection, select_scope)
        group = select.args.get("group")
        if group is not None:     # includes ROLLUP / CUBE / GROUPING SETS
            grouped |= lineage.origins(group, select_scope)

    filtered: set[Origin] = set()
    for scope in traverse_scope(qualified):
        for col in scope.columns:
            if _is_literal_filter(col):
                filtered |= lineage.column_origins(col, scope)

    demographic_out = sorted(c for _, c in outputs & demo)
    identifier = (outputs | grouped | filtered) & ids
    if demographic_out and identifier:
        raise _Reject(ReasonCode.QUASI_IDENTIFIER,
                      identifiers=sorted(f"{t}.{c}" for t, c in identifier),
                      demographics=demographic_out)
