# Interface Contracts

These contracts bind every module in the repository layout (plan §4). DD (`docs/design-decisions.md`) says *what* and *why*; this file says *how modules talk to each other*. A stage may add private helpers freely. Any change to a signature, model or error type documented here is made in this file in the same commit (plan §3, rule 2).

Conventions:
- Python 3.11+, `from __future__ import annotations`. Value objects are frozen `@dataclass`es. Models that cross the LLM boundary (structured output) or the persistence boundary are `pydantic.BaseModel` (v2).
- The package is `retail_agent` (`src/` layout). Paths below are relative to `src/retail_agent/`.
- Time: `time.monotonic()` for deadlines and latency. Trace timestamps are UTC ISO-8601 strings with a `Z` suffix. Dates in prompts are ISO dates.
- IDs: `thread_id` = `uuid4().hex`; `turn_id` = `f"{thread_id[:8]}-{n:04d}"`, where `n` is the turn number within the thread starting at 1; report IDs = `uuid4().hex[:12]`; query IDs within a turn = `"Q1"`, `"Q2"`, ….
- Every module receives its collaborators explicitly (constructor or argument). The only module-level singletons are pure constants.

---

## 1. Infrastructure

### 1.1 `config.py`

```python
class ConfigError(Exception):
    """Readable message naming the key, file and expected value."""

@dataclass(frozen=True)
class ModelSpec:
    provider: str            # "google" | "anthropic"
    model: str               # e.g. "gemini-3.8-flash"

@dataclass(frozen=True)
class Settings:
    profile: str                                   # "standard" | "free"
    roles: dict[str, tuple[ModelSpec, ...]]        # "cheap"/"expensive" -> chain, primary first;
                                                   # extra_fallbacks already appended if key present
    provider_key_env: dict[str, str]               # provider -> env var name (never the key itself)
    llm: LLMSettings
    turn: TurnSettings
    bigquery: BigQuerySettings
    paths: PathSettings                            # all Paths absolute, resolved against repo root
    tracing_max_field_chars: int
    prices: dict[str, tuple[float, float]]         # model -> (input, output) USD per 1M tokens
    eval: EvalSettings

@dataclass(frozen=True)
class LLMSettings: temperature: float; call_timeout_s: float; max_retries: int
                   backoff_base_s: float; backoff_max_s: float; min_time_for_retry_s: float
@dataclass(frozen=True)
class TurnSettings: deadline_s: dict[str, float]   # intent -> seconds, has "default"
                    synthesis_reserve_s: float; tool_budget: dict[str, int]
                    history_turns: int; max_consecutive_clarifications: int; pii_attempts_before_stop: int
    def deadline_for(self, intent: str) -> float: ...       # falls back to "default"
    def budget_for(self, intent: str) -> int: ...           # analysis / report_generation, else 0
@dataclass(frozen=True)
class BigQuerySettings: project: str | None; dataset: str; location: str; bytes_cap: int
                        query_timeout_s: float; max_rows_to_model: int; column_values_limit: int
                        max_cell_chars: int
@dataclass(frozen=True)
class PathSettings: data_dir: Path; app_db: Path; checkpoint_db: Path; traces: Path; content_dir: Path
@dataclass(frozen=True)
class EvalSettings: judge_role: str; judge_model: ModelSpec | None

def load_settings(path: Path | None = None, env: Mapping[str, str] | None = None) -> Settings:
    """Load config.yaml (default: repo root), apply the profile and env overrides, validate.
    Loads `.env` (python-dotenv) only when env is None. Raises ConfigError."""

def require_api_key(settings: Settings, provider: str, env: Mapping[str, str] | None = None) -> str:
    """Return the key or raise ConfigError("GOOGLE_API_KEY is not set ..."). Never logs it."""
```

**Content loaders**, also in `config.py`, because they are configuration that is not code:

```python
@dataclass(frozen=True)
class Column:
    name: str; type: str; description: str
    categorical: bool = False; high_cardinality: bool = False
    customer_identifier: bool = False; demographic: bool = False

@dataclass(frozen=True)
class Table:
    name: str                     # logical name == CTE name
    source: str                   # base table name in the dataset
    description: str
    columns: tuple[Column, ...]   # CTE column order
    scope_filter: str | None      # "brand" for products
    join: str | None              # "products" for order_items
    filter: str | None            # SQL predicate text (from catalog.yaml): order_items, orders, users

@dataclass(frozen=True)
class Catalog:
    dataset: str
    tables: dict[str, Table]               # keys: products, order_items, orders, users (lower-case)
    blocked_columns: frozenset[str]        # lower-case; NEVER rendered into prompts
    derived_metrics: tuple[str, ...]
    def column(self, table: str, column: str) -> Column | None: ...   # case-insensitive
    def allowed_columns(self) -> dict[str, frozenset[str]]: ...       # table -> lower-case names
    def schema_prompt(self) -> str: ...      # tables + columns + types + descriptions (analysis, schema answer)
    def compact_prompt(self) -> str: ...     # table: col, col, ... + derived metrics (classify)

def load_catalog(path: Path) -> Catalog: ...             # raises ConfigError
def load_text(path: Path) -> str: ...                    # glossary.md, persona.md, prompts
```

### 1.2 Configuration keys and environment overrides

The keys are exactly those in `config.yaml`. Overrides are applied after the profile is chosen:

| Env var | Effect |
|---|---|
| `LLM_PROFILE` | profile name (`standard` / `free`); unknown name → `ConfigError` |
| `GOOGLE_CLOUD_PROJECT` | `bigquery.project` (`bigquery.project_env` names this variable) |
| `RA_MODEL_CHEAP`, `RA_MODEL_EXPENSIVE` | replace the primary model of the role (provider `google`); the rest of the chain stays |
| `RA_BYTES_CAP` | `bigquery.bytes_cap` (int) |
| `RA_TRACES_PATH` | `paths.traces` |
| `RA_DATA_DIR` | `paths.data_dir`; `app_db` and `checkpoint_db` keep their file names under it |
| `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY` | provider keys; read only through `require_api_key` |
| `LANGSMITH_API_KEY` | optional, documented only (DD §15) |

A variable that is empty or still holds a `.env.example` placeholder (`your-gemini-api-key`, `your-gcp-project-id`) counts as unset. `UiEvent` and its event classes live in `agent/state.py`.

A missing `GOOGLE_CLOUD_PROJECT` is not an error at load time. It becomes a `ConfigError` the first time a `BigQueryRunner` is created, so offline tests never need it.

### 1.3 `tracing.py`: JSONL trace

One JSON object per line in `paths.traces`, appended and flushed per record. Schema version 1:

| Field | Type | Present in |
|---|---|---|
| `schema_version` | int (1) | all |
| `ts` | str, UTC ISO-8601 | all |
| `event` | str, see the table of events below | all |
| `thread_id`, `turn_id`, `exec_id` | str \| null | all; null only before login |
| `persona_hash` | str \| null | all within a turn |
| `node` | str \| null | `node`, `llm_call`, `tool_call` |
| `intent`, `classify_reason` | str \| null | `node` (classify), `turn_summary` |
| `model`, `role` | str | `llm_call` |
| `prompt` | list[{role, content}] \| str | `llm_call` (full messages, Req 7) |
| `response` | str \| dict | `llm_call` (text, tool calls or parsed output) |
| `truncated` | bool | records whose `prompt`/`response`/`sql` were cut to `tracing.max_field_chars` |
| `tokens_in`, `tokens_out` | int | `llm_call`, `turn_summary` |
| `latency_ms` | int | `node`, `llm_call`, `tool_call`, `turn_summary` |
| `attempt`, `retry_count` | int | `llm_call` (attempt starts at 1) |
| `fallback_used` | bool | `llm_call`, `turn_summary` |
| `llm_calls`, `tool_calls` | int | `turn_summary` (`tool_calls` also on `node` for analysis) |
| `tool` | str | `tool_call` (`run_sql` / `get_column_values`) |
| `query_id` | str | `tool_call` |
| `sql` | str | `tool_call`: the model's SQL; `sql_executed`: the final scoped SQL |
| `bytes_scanned`, `rows` | int \| null | `tool_call` |
| `outcome` | str | `tool_call` (`ok` / `empty` / failure kind), `turn_summary` (see below) |
| `error_type` | str \| null | any record that carries an error (values in §4.3) |
| `kind`, `reason_code`, `detail` | str / str / dict | `security_event` |
| `counts` | dict[str,int] | `pii_in_input` (e.g. `{"EMAIL": 1}`, never values) |
| `rating`, `comment` | str | `feedback` |
| `deadline_hit`, `budget_exhausted` | bool | `turn_summary` |
| `est_cost_usd` | float | `turn_summary` |

Event types:

| `event` | Written by | When |
|---|---|---|
| `session` | cli | login / new / resume / quit (`detail.action`) |
| `node` | agent nodes | one per node execution (enter→exit latency) |
| `llm_call` | llm wrapper | one per attempt, including failed attempts and retries |
| `tool_call` | analysis tools | one per tool invocation, executed or short-circuited |
| `security_event` | guards, nodes, cli | see `kind` values below |
| `pii_in_input` | input masking node | when the masked count is > 0 |
| `deletion` | reports.deletion | after a confirmed delete: `detail = {report_ids, count}` (metadata only) |
| `feedback` | cli | `/feedback` |
| `turn_summary` | turn wrapper | exactly one per user turn, always last |

`security_event.kind` values: `pii_column`, `out_of_scope_brand`, `quasi_identifier`, `qualified_table`, `forbidden_statement`, `forbidden_function`, `information_schema`, `reserved_cte_name`, `repeated_pii_attempt`, `pii_in_result`, `pii_in_answer`, `thread_access_denied`, `report_access_denied`, `malicious_intent`.

`turn_summary.outcome` values: `answered`, `partial`, `cannot_answer`, `clarification`, `refused`, `schema_answer`, `library`, `confirm_pending`, `deleted`, `delete_cancelled`, `no_access`, `error`.

```python
class Tracer:
    def __init__(self, path: Path, *, max_field_chars: int = 20000,
                 secret_values: Iterable[str] = ()) -> None: ...
    def bind(self, **context: str | None) -> "Tracer":
        """Child tracer sharing the file, with thread_id/turn_id/exec_id/persona_hash/node merged."""
    def write(self, event: str, **fields: Any) -> dict:
        """Add schema_version, ts and the bound context; truncate long fields (set truncated);
        replace any occurrence of a secret value with '<REDACTED>'; append; return the record."""
    def security_event(self, kind: str, reason_code: str | None = None, **detail: Any) -> dict: ...
    def pii_in_input(self, counts: Mapping[str, int]) -> dict: ...
    def feedback(self, rating: Literal["good", "bad"], comment: str | None) -> dict: ...
    def turn_summary(self, **fields: Any) -> dict: ...

class NullTracer(Tracer): ...   # tests: keeps records in .records (list[dict]), writes nothing

def read_traces(path: Path) -> Iterator[dict]: ...   # used by scripts; skips malformed lines
def validate_record(record: Mapping[str, Any]) -> list[str]: ...   # contract-schema check; [] = valid
```

`write()`, `security_event()` and `turn_summary()` raise `ValueError` for an unknown event, `kind` or `outcome`, and `bind()` for an unknown context key. Truncation applies to `prompt`, `response` and `sql`; a non-string value that is too long is stored as its truncated JSON text.

`secret_values` = the values of every provider key env var that is set. Values shorter than 8 characters are ignored.

### 1.4 `bq_client.py`: `BigQueryRunner` (DD §8.6)

The provided class moves into the package. Its constructor, `execute_query()` and `get_table_schema()` keep their signatures and behaviour. The constructor gains `location`; everything else is additive.

```python
QueryParamValue = str | int | float | bool | list[str] | list[int]

@dataclass(frozen=True)
class DryRunResult:
    ok: bool
    bytes_processed: int | None
    error: str | None              # BigQuery message (syntax / unknown column ...), None if ok

@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: list[dict[str, Any]]     # JSON-safe values: datetime/date -> ISO str, Decimal -> float
    bytes_processed: int
    bytes_billed: int
    job_id: str
    elapsed_ms: int

class BigQueryError(Exception): ...
class BigQueryBadRequestError(BigQueryError): ...      # syntax / semantic error; message is model-safe
class BytesCapExceededError(BigQueryError): ...        # dry-run bytes > cap, or bytesBilledLimitExceeded
class BigQueryTimeoutError(BigQueryError): ...
class BigQueryUnavailableError(BigQueryError): ...     # 5xx / transport after client retries, auth failure

class BigQueryRunner:
    def __init__(self, project_id: str | None = None,
                 dataset_id: str | None = "bigquery-public-data.thelook_ecommerce",
                 location: str | None = "US") -> None: ...
    def execute_query(self, sql_query: str) -> pd.DataFrame: ...                  # original
    def get_table_schema(self, table_name: str) -> list[dict[str, Any]]: ...     # original
    def dry_run(self, sql: str, params: Mapping[str, QueryParamValue] | None = None) -> DryRunResult:
        """Never raises for bad SQL (returns ok=False). Raises BigQueryUnavailableError on transport."""
    def run_query(self, sql: str, *, params: Mapping[str, QueryParamValue] | None = None,
                  max_bytes_billed: int, timeout_s: float,
                  labels: Mapping[str, str] | None = None) -> QueryResult:
        """Execute with maximum_bytes_billed, job timeout and labels; maps errors to the types above."""

def create_runner(settings: Settings) -> BigQueryRunner:
    """Runner from settings. ConfigError if bigquery.project is None (GOOGLE_CLOUD_PROJECT unset)."""

def to_query_parameters(params: Mapping[str, QueryParamValue]) -> list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter]: ...
def sanitize_labels(labels: Mapping[str, str]) -> dict[str, str]:
    """Keys/values lower-case, [a-z0-9_-] only, max 63 chars; drop empty values."""
```

Error messages of the typed errors are model-safe: the dataset path is replaced by `<dataset>` and BigQuery's `Location:` / `Job ID:` suffix is removed. `to_query_parameters` raises `TypeError` for an unsupported value; an empty list is sent as `ARRAY<STRING>`.

Job labels are `thread_id`, `turn_id`, `exec_id` and `app=retail_agent`.

---

## 2. Security core

### 2.1 `security/identity.py`

The SQLite file is `paths.app_db`. It holds the tables of DD §7, created if missing:
`executives(exec_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, scope_all INTEGER NOT NULL)`,
`executive_brands(exec_id TEXT NOT NULL REFERENCES executives, brand TEXT NOT NULL, PRIMARY KEY(exec_id, brand))`.

```python
@dataclass(frozen=True)
class Scope:
    all_brands: bool
    brands: tuple[str, ...]          # sorted; empty iff all_brands
    @classmethod
    def everything(cls) -> "Scope": ...
    @classmethod
    def of(cls, brands: Iterable[str]) -> "Scope": ...    # raises ValueError if empty
    def allows(self, brand: str) -> bool: ...              # case-insensitive exact match; True for all

@dataclass(frozen=True)
class Executive:
    exec_id: str; display_name: str; scope: Scope

class UnknownExecutiveError(Exception): ...

class IdentityStore:
    def __init__(self, db_path: Path) -> None: ...         # creates tables
    def upsert_executive(self, exec_id: str, display_name: str, *, scope_all: bool,
                         brands: Sequence[str] = ()) -> None:
        """One transaction; replaces the brand rows. scope_all=True requires brands=()."""
    def get_executive(self, exec_id: str) -> Executive: ...    # raises UnknownExecutiveError
    def get_scope(self, exec_id: str) -> Scope: ...            # the single source of permissions
    def list_executives(self) -> list[Executive]: ...
```

`scripts/seed_executives.py [--dry-run]` implements `docs/facts.md` §5 through `BigQueryRunner` and `IdentityStore`, and prints the chosen brands.

### 2.2 `security/scoped_sql.py`

```python
@dataclass(frozen=True)
class ScopedQuery:
    sql: str                                   # final SQL: generated CTEs + the model's query
    params: dict[str, QueryParamValue]         # {"brands": [...]} for a brand scope, {} for all

class ScopedSqlError(Exception): ...           # model SQL could not be parsed or rewritten

RESERVED_NAMES: frozenset[str]                 # {"products", "order_items", "orders", "users"}
DIALECT = "bigquery"
CTE_ORDER: tuple[str, ...]                     # ("products", "order_items", "orders", "users")

def generated_ctes(scope: Scope, catalog: Catalog) -> list[exp.CTE]:
    """The four scoped CTEs as sqlglot nodes (used by build_scoped_sql)."""

def build_scoped_sql(model_sql: str, scope: Scope, catalog: Catalog) -> ScopedQuery:
    """Parse with sqlglot (dialect bigquery), prepend the four CTEs generated from the catalog
    (DD §8.1, in dependency order products -> order_items -> orders -> users) to the AST's WITH
    clause, with the model's own CTEs after them. Brand filter `brand IN UNNEST(@brands)` only
    for a brand scope; the order_items CTE always applies the catalog `filter`
    (`oi.created_at <= CURRENT_TIMESTAMP()`, future-dated items removed) for every scope. The public dataset path appears only inside the generated CTEs.
    Precondition: validate_sql(model_sql, ...) returned ok."""

def wrap_with_limit(sql: str, max_rows: int) -> str:
    """Cap the result at max_rows + 1 rows (DD §8.3), applied after build_scoped_sql. A query
    without LIMIT gets `LIMIT max_rows + 1`; a literal LIMIT above that is tightened in place
    (OFFSET kept), so the model's ORDER BY is preserved; any other shape is wrapped as
    `SELECT * FROM (<sql>) LIMIT max_rows + 1`."""
```

### 2.3 `security/validator.py`

```python
class ReasonCode(StrEnum):
    PARSE_ERROR = "parse_error"
    MULTIPLE_STATEMENTS = "multiple_statements"
    NOT_SELECT = "not_select"                     # DML, DDL, scripting, EXPORT, CALL ...
    FORBIDDEN_FUNCTION = "forbidden_function"     # EXTERNAL_QUERY, ML.*, table functions over paths
    INFORMATION_SCHEMA = "information_schema"
    QUALIFIED_TABLE = "qualified_table"           # project/dataset-qualified table path
    UNKNOWN_TABLE = "unknown_table"               # not one of the 4 logical names or own CTEs
    RESERVED_CTE_NAME = "reserved_cte_name"       # model CTE named like a logical table (any case, any depth)
    PII_COLUMN = "pii_column"                     # unresolved column whose name is in blocked_columns
    UNKNOWN_COLUMN = "unknown_column"             # not in the allowlist of its table
    OUT_OF_SCOPE_BRAND = "out_of_scope_brand"
    QUASI_IDENTIFIER = "quasi_identifier"

SECURITY_EVENT_CODES: frozenset[ReasonCode]  # every code except PARSE_ERROR, MULTIPLE_STATEMENTS,
                                             # UNKNOWN_TABLE, UNKNOWN_COLUMN

@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason_code: ReasonCode | None
    model_message: str        # neutral, actionable, never says "PII" or lists blocked columns
    user_message: str | None  # set only for OUT_OF_SCOPE_BRAND: "You don't have access to brand {X}."
    security_event: bool      # reason_code in SECURITY_EVENT_CODES
    details: dict[str, Any]   # e.g. {"column": "email"}, {"brands": ["Nike"]}; logged, not shown

def validate_sql(sql: str, scope: Scope, catalog: Catalog) -> ValidationResult: ...
```

Rules are evaluated in this order, and the first failure wins: parse → statement count → statement type → forbidden functions / `INFORMATION_SCHEMA` → table references → reserved CTE names → columns (PII before unknown) → brand literals (skipped for `scope.all_brands`) → quasi-identifier (applies to everyone). The binding cases are `eval/security_cases.yaml`.

Model messages, so later stages produce the same wording:

| Code | `model_message` |
|---|---|
| `pii_column` / `unknown_column` | "Column `{col}` is not available. Use only the columns listed in the schema." |
| `unknown_table` / `qualified_table` / `information_schema` | "Only the tables products, order_items, orders and users can be queried, by those names." |
| `reserved_cte_name` | "CTE name `{name}` is reserved; choose another name." |
| `not_select` / `multiple_statements` / `forbidden_function` | "Only a single read-only SELECT statement is allowed." |
| `out_of_scope_brand` | "Brand `{brand}` is outside this user's data. Do not retry with other brand filters; tell the user they have no access to it." |
| `quasi_identifier` | "Customer IDs cannot be combined with demographic columns (age, gender, city, state, country, traffic_source). Return aggregates by demographic group instead, or customer IDs without demographics." |
| `parse_error` | "SQL could not be parsed: {parser message}" |

### 2.4 `security/guards.py`

```python
class FailureKind(StrEnum):
    VALIDATION = "validation"              # validator rejection other than the two below
    OUT_OF_SCOPE_BRAND = "out_of_scope_brand"   # terminal: ends the turn with user_message
    PII_REPEATED = "pii_repeated"          # terminal: pii_attempts_before_stop reached
    BAD_REQUEST = "bad_request"            # dry-run / BigQuery syntax or semantic error
    BYTES_CAP = "bytes_cap"                # terminal: honest message, no retry
    ROW_LIMIT = "row_limit"                # > max_rows_to_model rows
    PII_IN_RESULT = "pii_in_result"        # rows withheld from the model
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"            # terminal: "data source unavailable right now"
    DEADLINE = "deadline"                  # not started: remaining time below the synthesis reserve
    REPEATED = "repeated"                  # same normalised SQL failed earlier in this turn (R11)

@dataclass(frozen=True)
class QuerySuccess:
    query_id: str
    columns: tuple[str, ...]
    rows: list[dict[str, Any]]     # <= max_rows_to_model, cells truncated to max_cell_chars
    row_count: int
    bytes_processed: int
    sql: str                       # the model's SQL
    hint: str | None = None        # set when row_count == 0 (DD §10.2 empty-result hint)
    cached: bool = False           # True when returned from the repeated-query store (R11)

@dataclass(frozen=True)
class QueryFailure:
    query_id: str
    kind: FailureKind
    model_message: str
    user_message: str | None       # shown to the user when terminal (and for ROW_LIMIT if user-requested)
    terminal: bool                 # the analysis loop must stop and surface user_message
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
    deadline: Deadline                         # llm/wrapper.py
    settings: Settings
    catalog: Catalog
    bq: BigQueryRunner
    tracer: Tracer
    executed: dict[str, QueryOutcome] = field(default_factory=dict)   # normalised SQL -> outcome
    pii_attempts: int = 0
    next_query_no: int = 1
    def new_query_id(self) -> str: ...                                 # "Q1", "Q2", ...

def run_scoped_query(sql: str, ctx: QueryContext, *,
                     user_requested_granularity: bool = False) -> QueryOutcome:
    """Repeated check -> deadline check -> validate -> build_scoped_sql -> wrap_with_limit ->
    dry_run (bytes vs cap) -> run_query (timeout = min(query_timeout_s, deadline.remaining()
    - synthesis_reserve_s)) -> PII scan of rows -> outcome. Writes the tool_call trace record
    and any security_event. Never raises for model-caused problems."""

def column_values(column: str, ctx: QueryContext, *, search: str | None = None,
                  limit: int | None = None) -> QueryOutcome:
    """column = "table.column", which must be catalog-categorical. A high-cardinality column
    requires search. Generated SQL: SELECT col AS value, COUNT(*) AS n FROM <table> WHERE
    col IS NOT NULL [AND LOWER(CAST(col AS STRING)) LIKE CONCAT('%', LOWER(@search), '%')]
    GROUP BY value ORDER BY n DESC, value LIMIT <n>. It runs through the same scoped CTEs
    and guards; search is a query parameter; n = min(limit, column_values_limit) is a validated
    int inlined as a literal. The repeated-query key includes the parameters."""

def normalize_sql(sql: str) -> str:
    """sqlglot canonical form (bigquery dialect, normalize=True, comments dropped, pretty=False).
    Falls back to whitespace-collapsed lower-case text if parsing fails."""

@dataclass(frozen=True)
class PiiHit:
    kind: Literal["EMAIL", "PHONE", "ADDRESS", "CARD"]
    column: str | None

def scan_rows_for_pii(rows: Sequence[Mapping[str, Any]]) -> list[PiiHit]: ...   # string cells only
def scan_text_for_pii(text: str) -> list[PiiHit]: ...                           # final answer check
```

Trace: every call writes one `tool_call` record with `tool`, `query_id`, `sql` (the model's SQL),
`sql_executed` (final scoped SQL, when built), `outcome` (`ok` / `empty` / `cached` / a
`FailureKind` value), `error_type`, `rows`, `bytes_scanned`, `latency_ms`; `node` defaults to
`analysis` when the tracer is not bound to a node. Validator rejections with `security_event`
write a `security_event` (`not_select` → kind `forbidden_statement`, other codes → the kind of the
same name); the stop after `pii_attempts_before_stop` PII rejections also writes
`repeated_pii_attempt`. Transient failures (`timeout`, `unavailable`, `deadline`) are not stored
for the repeated-query check.

Row-limit messages:
- `user_requested_granularity=True` → `terminal=False`. `user_message` = "That request returns more than {N} rows, which is above the limit I can analyse. I can aggregate it instead (for example by month or category) if you'd like." The model must tell the user and may only *offer* aggregation.
- `user_requested_granularity=False` → `user_message=None`. `model_message` = "The query returned more than {N} rows. Rewrite it with aggregation."

### 2.5 `security/input_masking.py`

```python
@dataclass(frozen=True)
class MaskResult:
    text: str                       # placeholders <EMAIL>, <PHONE>, <CARD>
    counts: dict[str, int]          # only kinds with count > 0

def mask_input(text: str) -> MaskResult:
    """Order: CARD (13-19 digits, groups of 4 or 4-6-5 or a single run, Luhn-valid, network
    prefix 4 / 51-55 / 2221-2720 / 34 / 37 / 6011 / 65 / 644-649), then EMAIL, then PHONE
    (9-15 digits; either starts with '+', or uses '-' or parentheses as separators, or is a
    single unbroken run; dots and commas are not phone separators). Exact behaviour:
    eval/security_cases.yaml (input_masking)."""
```

---

## 3. LLM wrapper

### 3.1 `llm/errors.py`

```python
class LLMError(Exception):
    error_type: str                          # trace error_type
class QuotaExhaustedError(LLMError):         # error_type "quota_exhausted" (DD §14.1.1)
    def __init__(self, models: Sequence[str], role: str) -> None: ...
    models: tuple[str, ...]                  # every model in the chain whose daily quota is out
    primary_model: str
class LLMUnavailableError(LLMError): ...     # "llm_unavailable": chain exhausted on 5xx / transport
class LLMTimeoutError(LLMError): ...         # "llm_timeout": attempt timed out and no time to retry
class DeadlineExceededError(LLMError): ...   # "deadline": not started, no time left for the call
class LLMRequestError(LLMError): ...         # "llm_request": non-retryable 4xx (bad request, auth)
class LLMOutputError(LLMError): ...          # "llm_output": structured output failed to parse/validate

RateLimitKind = Literal["per_minute", "daily"]
def classify_rate_limit(exc: BaseException) -> RateLimitKind | None:
    """None if not a 429. Rule: docs/facts.md §4 (PerDay / quota_exceeded / 'daily' -> daily)."""
def retry_after_seconds(exc: BaseException) -> float | None:
    """From RetryInfo.retryDelay or the Retry-After header."""
# Also: http_status(exc), is_timeout(exc), is_transport_error(exc); all look through __cause__/__context__.
```

### 3.2 `llm/wrapper.py`

```python
class Deadline:
    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None: ...
    @classmethod
    def at(cls, monotonic_deadline: float, *, clock=time.monotonic) -> "Deadline": ...
    def remaining(self) -> float: ...                  # seconds, may be negative
    def expired(self) -> bool: ...

@dataclass
class TurnBudget:
    tool_calls_limit: int
    tool_calls_used: int = 0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    fallback_used: bool = False
    def try_consume_tool_call(self) -> bool: ...        # False when exhausted
    @property
    def tool_calls_remaining(self) -> int: ...
    def as_dict(self) -> dict[str, int | bool]: ...     # for state persistence

@dataclass
class TurnContext:
    thread_id: str
    turn_id: str
    exec_id: str
    intent: str | None
    deadline: Deadline
    synthesis_reserve_s: float
    budget: TurnBudget
    tracer: Tracer                                      # already bound to thread/turn/exec/persona
    emit: Callable[[UiEvent], None] = lambda e: None    # progress to the CLI (§6.1)

@dataclass(frozen=True)
class LLMResult:
    message: AIMessage                  # raw LangChain message (tool_calls preserved)
    parsed: BaseModel | None            # when schema was given
    model: str                          # the model that answered
    fallback_used: bool
    retry_count: int
    tokens_in: int
    tokens_out: int
    latency_ms: int

ModelFactory = Callable[[ModelSpec, float], BaseChatModel]   # (spec, timeout_s) -> chat model

class LLMClient:
    def __init__(self, settings: Settings, *, factory: ModelFactory | None = None,
                 env: Mapping[str, str] | None = None,
                 sleep: Callable[[float], None] = time.sleep,          # tests only
                 rng: Callable[[], float] = random.random) -> None: ...  # tests only (backoff jitter)
    def call_llm(self, role: Literal["cheap", "expensive"], messages: Sequence[BaseMessage],
                 ctx: TurnContext, *, node: str,
                 schema: type[BaseModel] | None = None,
                 tools: Sequence[BaseTool | type[BaseModel]] | None = None,
                 tool_choice: str | None = None,
                 is_synthesis: bool = False) -> LLMResult: ...
```

Semantics, binding for Stage 3:
- **Chain.** `settings.roles[role]`, in order. A model whose provider key is missing is skipped. A daily-quota 429 moves to the next model without retry. When every model is exhausted for daily quota → `QuotaExhaustedError(models, role)`. When the chain is exhausted for any other reason → `LLMUnavailableError`.
- **Retries** happen per model, at most `llm.max_retries`, only for per-minute 429, 5xx and timeouts. Wait = `retry_after_seconds()` or `min(backoff_max_s, backoff_base_s * 2**attempt) * random()`. During a wait, `ctx.emit(RateLimitWait(seconds))` is sent. A retry starts only if `remaining - wait >= min_time_for_retry_s` (+ reserve when not synthesis); otherwise the wrapper moves to the next model or raises. 4xx other than 429 → `LLMRequestError`, with no retry.
- **Timeout per attempt** = `min(llm.call_timeout_s, deadline.remaining() - (0 if is_synthesis else synthesis_reserve_s))`. If that is ≤ 0 → `DeadlineExceededError` without calling.
- **Structured output.** `schema` → `with_structured_output(schema)`. `tools` → `bind_tools(tools, tool_choice=...)`. A parse/validation failure of `schema` output counts as one retry (same model) and then raises `LLMOutputError`.
- **Accounting.** Every attempt increments `ctx.budget.llm_calls` and the token counters, sets `fallback_used`, and writes one `llm_call` record: role, model, node, attempt, prompt, response, tokens, latency_ms, error_type, retry_count, fallback_used.
- Tests inject `factory` returning fake chat models. No test needs a key.

---

## 4. Agent

### 4.1 Classify output (`agent/nodes.py`, used by `prompts/classify.md`)

```python
Intent = Literal["analysis", "report_generation", "report_library", "schema_question",
                 "clarify", "unanswerable", "off_topic", "malicious"]

class MetricOverride(BaseModel):
    name: Literal["churn_window_days", "analysis_period", "comparison_period", "top_n",
                  "rfm_scores", "age_bands"]
    value: str                         # as the user said it, e.g. "60", "2025", "quartiles"

class ClassifyOutput(BaseModel):
    reason: str                        # one sentence, always logged (routing analysis)
    intent: Intent
    clarification_question: str | None = None                  # required iff intent == "clarify"
    library_action: Literal["save", "list", "delete"] | None = None    # required iff report_library
    delete_scope: Literal["this_conversation", "entity"] | None = None # required iff action == delete
    delete_query: str | None = None    # the entity / topic text for delete_scope == "entity"
    overrides: list[MetricOverride] = []    # session metric overrides stated in this message
```

Post-validation in code, with no extra LLM call:
- `clarify` without a question, or a clarification when `consecutive_clarifications >= max` → `analysis`.
- `report_library` without `library_action` → `list`.
- `delete` without `delete_scope` → `entity` with `delete_query` = the rewritten question.
- `delete_scope == "entity"` without `delete_query` → `delete_query` = the rewritten question.

`reason` comes first so the label is conditioned on it.

### 4.2 Analysis output (`analysis/output.py`, DD §10.4)

Field order is the generation order:

```python
class Finding(BaseModel):
    id: str                            # "F1", "F2", ...
    statement: str                     # one claim with its numbers
    query_ids: list[str]               # "Q1", ...; queries of this turn that support it

class ActionItem(BaseModel):
    action: str
    finding_id: str                    # must reference findings[].id

class AnalysisOutput(BaseModel):
    outcome: Literal["answer", "cannot_answer", "clarification"]
    definitions_used: list[str]        # glossary definitions + overrides + concrete date range + granularity
    findings: list[Finding]
    limitations: list[str]             # gaps, failed sub-queries, partial periods, budget/deadline stops
    action_items: list[ActionItem]     # required (>= 1) for report_generation when outcome == "answer"
    summary: str                       # rendered first by the CLI
    entities: list[str]                # brands, categories, products, states ... named in the answer
    title: str                         # <= 80 chars
    description: str                   # one line, <= 200 chars
    clarification_question: str | None = None   # required iff outcome == "clarification"

def check_output(out: AnalysisOutput, *, executed_query_ids: set[str],
                 report: bool) -> tuple[AnalysisOutput, list[str]]:
    """Deterministic fixes + problems found: drop action items whose finding_id is unknown,
    drop unknown query_ids, and add a limitation line for each fix. Raises nothing."""

def render_markdown(out: AnalysisOutput) -> str: ...   # summary, findings, action items, definitions, limitations
```

How special cases are represented:
- **Early exit** (the model concludes it cannot answer before or after querying): `outcome="cannot_answer"`. `summary` says why and what data exists. `findings` may be empty.
- **Clarification from the analysis model**: `outcome="clarification"` with `clarification_question`. The graph shows it as a clarification and increments `consecutive_clarifications`.
- **Partial answer** (budget or deadline reached, or some queries failed): `outcome="answer"`, and the gap is listed in `limitations`. `AnalysisResult.stopped_by` records why.
- **Out-of-scope brand, bytes cap, data source unavailable, repeated PII**: no `AnalysisOutput`. `AnalysisResult.output=None` and `user_message` is set (below).

```python
@dataclass(frozen=True)
class QueryRecord:
    query_id: str; sql: str; outcome: str            # "ok" | "empty" | FailureKind value
    row_count: int | None; bytes_processed: int | None

@dataclass(frozen=True)
class AnalysisResult:
    output: AnalysisOutput | None
    user_message: str | None                          # set iff output is None
    stopped_by: Literal["budget", "deadline", "out_of_scope_brand", "bytes_cap",
                        "unavailable", "pii_repeated"] | None
    queries: tuple[QueryRecord, ...]
```

### 4.3 Error types, trace `error_type`

`quota_exhausted`, `llm_unavailable`, `llm_timeout`, `llm_request`, `llm_output`, `deadline`, `budget_exhausted`, `bq_unavailable`, `bq_timeout`, `bq_bad_request`, `bytes_cap`, `validation`, `internal`.

### 4.4 Agent state (`agent/state.py`, DD §5)

LangGraph `TypedDict`. Every value must be JSON/msgpack-serialisable, so pydantic models are stored via `.model_dump()`.

```python
class HistoryTurn(TypedDict):
    question: str                    # masked, rewritten question
    answer: str                      # rendered final answer text (never tool results)

class PendingDelete(TypedDict):
    action_id: str                   # uuid4 hex
    owner_id: str
    thread_id: str
    report_ids: list[str]            # snapshot; deleted exactly, never re-searched
    reports: list[dict]              # [{id, title, created_at}] for the confirmation message
    created_turn_id: str

class AgentState(TypedDict, total=False):
    # conversation (persisted across turns)
    thread_id: str
    owner_exec_id: str
    persona_text: str
    persona_hash: str                # sha256(persona_text)[:12]
    history: list[HistoryTurn]       # all turns; prompts use the last turn.history_turns
    metric_overrides: dict[str, str] # MetricOverride.name -> value
    last_answer: dict | None         # AnalysisOutput.model_dump() of the last final answer
    pending_delete: PendingDelete | None
    consecutive_clarifications: int
    turn_no: int
    # current turn (reset at turn start)
    turn_id: str
    user_input: str                  # masked input
    rewritten_question: str
    classify: dict | None            # ClassifyOutput.model_dump()
    deadline_at: float               # time.monotonic() deadline, set when the intent is known
    budget: dict                     # TurnBudget.as_dict()
    notes: list[str]                 # one-line notes shown before the response ("Deletion cancelled.")
    response: dict | None            # AssistantResponse.model_dump(), the turn's output
    outcome: str | None              # trace turn_summary outcome, set by the node that built the response
    stopped_by: str | None           # AnalysisResult.stopped_by (drives deadline_hit / budget_exhausted)
```

`deadline_at` uses the monotonic clock of the running process. A thread resumed in a new process always starts a new turn, which sets a new deadline, so no deadline crosses processes.

### 4.5 Assistant response (`agent/state.py`): what a turn returns to the CLI

```python
class ReportSummary(BaseModel):
    id: str; title: str; description: str; created_at: str     # ISO date-time UTC

class AssistantResponse(BaseModel):
    kind: Literal["analysis", "report", "schema_answer", "clarification", "refusal",
                  "unanswerable", "no_access", "library_list", "saved", "confirm_delete",
                  "deleted", "nothing_to_delete", "notice", "error"]
    text: str | None = None              # markdown for non-analysis kinds
    analysis: AnalysisOutput | None = None
    reports: list[ReportSummary] = []    # library_list, confirm_delete, deleted
    notes: list[str] = []
    thread_id: str
```

### 4.6 Thread registry (`agent/threads.py`, R1)

These rows live in the same SQLite file as identity: `threads(thread_id TEXT PRIMARY KEY, owner_exec_id TEXT NOT NULL, created_at TEXT NOT NULL, title TEXT)`.

```python
class ThreadRegistry:
    def __init__(self, db_path: Path) -> None: ...
    def create(self, owner_exec_id: str) -> str: ...                # returns thread_id
    def owner_of(self, thread_id: str) -> str | None: ...
    def can_open(self, thread_id: str, exec_id: str) -> bool: ...   # False for unknown and foreign
    def list_for(self, exec_id: str, limit: int = 20) -> list[dict]: ...
    def set_title(self, thread_id: str, title: str) -> None: ...    # first question, truncated
```

A `/resume` of a foreign or unknown thread gets the same answer, "Thread not found.", and a `security_event(kind="thread_access_denied")` is written only for foreign threads.

### 4.7 Prompt assembly (`agent/prompt_assembly.py`, DD §13)

```python
def load_prompt(name: str, content_dir: Path) -> str: ...          # content/prompts/<name>.md, header stripped
def render(template: str, **values: str) -> str:
    """Replace {{key}}; raises KeyError on a missing value or a leftover {{...}}."""
def load_persona(content_dir: Path) -> tuple[str, str]: ...         # (text, hash)
def load_user_preferences(exec_id: str) -> list[str]: ...           # seam: returns []
def build_system_prompt(*, safety: str, preferences: list[str], persona: str,
                        executive: Executive, today: date) -> str:
    """Order: safety -> user preferences -> persona -> user context (name, scope description)."""
def history_block(history: Sequence[HistoryTurn], n: int) -> str: ...
def scope_description(scope: Scope) -> str: ...  # "all brands" | "brands: A, B, C"
```

The prompt header format is described in §7. Placeholders use `{{name}}`, so SQL braces and JSON never clash.

### 4.8 Analysis tools and subgraph (`analysis/tools.py`, `analysis/subgraph.py`)

```python
# Tool schemas exposed to the model (names are binding; prompts refer to them)
class RunSqlArgs(BaseModel):
    sql: str                                   # BigQuery SQL over the logical tables
    user_requested_granularity: bool = False   # True only if the user asked for this row-level detail
class GetColumnValuesArgs(BaseModel):
    column: str                                # "table.column"
    search: str | None = None
    limit: int = 50
class FinalAnswer(AnalysisOutput): ...         # final-answer tool; its args are the structured output

def make_tools(ctx: QueryContext) -> list[BaseTool]: ...    # run_sql, get_column_values (+ final_answer schema)
def format_outcome_for_model(outcome: QueryOutcome) -> str:
    """Compact text for the ToolMessage: query_id, columns, rows as CSV-like lines or the
    error/hint message. Never includes the scoped SQL or the dataset path."""
def retrieve_golden_examples(question: str) -> list[dict]: ...   # seam: returns [] (DD §12.1)

def run_analysis(question: str, *, mode: Literal["analysis", "report_generation"],
                 state: AgentState, executive: Executive, ctx: TurnContext,
                 qctx: QueryContext, llm: LLMClient, catalog: Catalog, glossary: str,
                 content_dir: Path, today: date) -> AnalysisResult:
    """ReAct loop with the expensive model (DD §10). Budget: ctx.budget (6 / 10). Every
    executed tool call consumes budget, including several in one step; final_answer does not.
    Stops on a terminal QueryFailure, on budget exhaustion or at the synthesis reserve, then
    forces one final_answer call (tool_choice="final_answer", is_synthesis=True)."""
```

The subgraph may be a compiled LangGraph `StateGraph` (ToolNode + conditional edge) wrapped by `run_analysis`. The function signature is the contract. The Stage 4 implementation is a plain loop. `make_tools` returns three tools named `run_sql`, `get_column_values` and `final_answer`; the loop executes the first two itself, so it can count the budget. `check_output` returns a problem for every fix it made; the loop feeds problems back to the model once. `analysis/subgraph.build_prompt(...)` renders the prompt. A final answer that contains PII is replaced by a refusal and a `pii_in_answer` event is written.

### 4.9 Graph and app facade (`agent/graph.py`, `agent/nodes.py`)

```python
@dataclass
class Deps:
    settings: Settings
    catalog: Catalog
    glossary: str
    tracer: Tracer
    identity: IdentityStore
    threads: ThreadRegistry
    reports: ReportStore
    bq: BigQueryRunner | None            # None in offline tests that never reach SQL
    llm: LLMClient
    today: Callable[[], date] = date.today

def build_graph(deps: Deps, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph: ...
def make_checkpointer(settings: Settings) -> SqliteSaver: ...   # paths.checkpoint_db

class AgentApp:
    """The only object the CLI talks to."""
    def __init__(self, deps: Deps, checkpointer: BaseCheckpointSaver) -> None: ...
    current_exec: Executive | None
    current_thread_id: str | None
    def login(self, exec_id: str) -> Executive:            # raises UnknownExecutiveError
        """Leaves the current thread (cancelling a pending delete) and starts a new thread."""
    def new_thread(self) -> str: ...                       # same leave semantics
    def resume(self, thread_id: str) -> bool: ...          # owner check; False -> "Thread not found."
    def send(self, text: str) -> Iterator[UiEvent]: ...    # one user turn; the last event is final
    def feedback(self, rating: Literal["good", "bad"], comment: str | None) -> None: ...
    def trace_info(self) -> tuple[str | None, Path]: ...   # (thread_id, traces path)
```

Node names, used in traces and progress steps: `mask_input`, `rewrite`, `classify`, `analysis`, `report_generation`, `schema_answer`, `clarify`, `unanswerable`, `refuse`, `report_library`, `confirm_delete`, `finalize`.

Canned texts are owned by `agent/nodes.py`:
- `off_topic`: "I can only help with analysis of our sales, products and customers data."
- `malicious`: "I can't help with that request."
- `unanswerable`: built from the catalog, "That information isn't in our data. Available: …".

### 4.10 Turn wrapper

`AgentApp.send()`:
1. Starts the turn: `turn_no += 1`, `turn_id`, a tracer bound to it, reset turn fields.
2. Runs the graph with `stream_mode=["custom", "updates"]`.
3. Catches every exception and maps it to `ErrorEvent`. Known errors get dedicated texts (§6.2). The checkpointed state stays consistent, because the failed turn's partial updates are not committed beyond the last completed node, and `response` is set to `kind="error"`.
4. Always writes `turn_summary`.

Input masking (§2.5) runs in `send()` before the input reaches the checkpointer, so no checkpoint or trace holds a raw identifier; the `mask_input` node re-masks as defence in depth. After an exception, `send()` closes the run with `update_state(..., as_node="finalize")` and `response.kind="error"`. The first turn of a thread seeds the conversation fields and pins the persona. `rewrite` and `classify` send only the safety rules as system message (their prompts have no persona part); `schema_answer` and the analysis prompts carry the full system prompt.

---

## 5. Reports

### 5.1 `reports/store.py` (DD §11.1)

These tables live in `paths.app_db`:
- `reports(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT, description TEXT, thread_id TEXT, created_at TEXT, content_json TEXT, entities TEXT, search_text TEXT)`, where `entities` is a JSON list;
- `audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT, action TEXT, report_ids TEXT, count INTEGER, created_at TEXT)`, where `report_ids` is a JSON list. It stores metadata only.

```python
@dataclass(frozen=True)
class ReportRecord:
    id: str; owner_id: str; title: str; description: str; thread_id: str
    created_at: str; content: AnalysisOutput; entities: tuple[str, ...]
    def summary(self) -> ReportSummary: ...

def build_search_text(out: AnalysisOutput) -> str:
    """Lower-cased title, description, summary, findings, action items, limitations, entities."""

class ReportStore:
    def __init__(self, db_path: Path) -> None: ...
    def save(self, owner_id: str, thread_id: str, content: AnalysisOutput) -> ReportRecord: ...
    def list(self, owner_id: str) -> list[ReportSummary]: ...           # newest first
    def get(self, owner_id: str, report_id: str) -> ReportRecord | None: ...
    def ids_by_thread(self, owner_id: str, thread_id: str) -> list[str]: ...
    def substring_ids(self, owner_id: str, terms: Sequence[str]) -> list[str]: ...  # case-insensitive
    def catalog_for_selection(self, owner_id: str) -> list[dict]: ...   # [{id, title, description, entities}]
    def filter_owned(self, owner_id: str, ids: Iterable[str]) -> list[str]: ...
    def delete(self, owner_id: str, ids: Sequence[str]) -> int:
        """Hard delete of the owned subset in one transaction plus one audit_log row
        (action='delete', report_ids, count). Returns the number deleted."""
```

Every method filters by `owner_id` in SQL. An empty `terms` list matches nothing.

### 5.2 `reports/search.py` (R3)

```python
class ReportSelectionOutput(BaseModel):          # structured output of prompts/report_selection.md
    search_terms: list[str]                      # terms the model used (brands, topics, synonyms)
    report_ids: list[str]

@dataclass(frozen=True)
class Selection:
    report_ids: list[str]       # union(model ids ∩ owned, substring matches), newest first
    search_terms: list[str]
    model_ids_dropped: list[str]   # hallucinated or foreign IDs (logged, never shown)

def select_reports_by_entity(request: str, owner_id: str, store: ReportStore,
                             llm: LLMClient, ctx: TurnContext, content_dir: Path) -> Selection: ...
def select_reports_by_thread(owner_id: str, thread_id: str, store: ReportStore) -> Selection: ...
```

The substring terms are `search_terms`, plus `ClassifyOutput.delete_query` if set. If the owner has no reports, the function returns an empty selection without calling the LLM.

### 5.3 `reports/deletion.py` (DD §11.3, R4)

```python
CONFIRM_WORD = "confirm"
CANCEL_SENTINEL = "\x00__cancel_pending_delete__"     # resume value sent by AgentApp when leaving a thread

def make_pending_delete(owner_id: str, thread_id: str, selection: Selection,
                        store: ReportStore, turn_id: str) -> PendingDelete: ...
def confirmation_prompt(pending: PendingDelete) -> str:
    """'3 reports will be permanently deleted. Type `confirm` to proceed.' plus the list."""
def is_confirmation(text: str) -> bool: ...           # text.strip().lower() == "confirm"
def execute_delete(pending: PendingDelete, store: ReportStore, tracer: Tracer) -> int:
    """Deletes exactly pending.report_ids (owned), writes the deletion trace event."""
```

---

## 6. CLI–graph interaction

### 6.1 Event stream

`AgentApp.send()` yields `UiEvent`s. Nodes produce progress via LangGraph `get_stream_writer()` (custom stream), and the facade converts them:

```python
@dataclass(frozen=True)
class ProgressEvent:      # spinner text
    step: str             # "Understanding the question", "Querying data (2/6)", "Writing the report",
                          # "Searching your reports", "Deleting reports"
@dataclass(frozen=True)
class RateLimitWait:
    seconds: float        # "Waiting for rate limit (5 s)"
@dataclass(frozen=True)
class ConfirmRequest:
    prompt: str           # confirmation_prompt(pending)
    reports: list[ReportSummary]
@dataclass(frozen=True)
class FinalResponse:
    response: AssistantResponse
@dataclass(frozen=True)
class ErrorEvent:
    message: str          # user-facing, always includes thread_id
    error_type: str

UiEvent = ProgressEvent | RateLimitWait | ConfirmRequest | FinalResponse | ErrorEvent
```

The custom stream payload is `{"type": "progress", "step": ...}` or `{"type": "rate_limit_wait", "seconds": ...}`. Every turn ends with exactly one of `FinalResponse`, `ConfirmRequest` or `ErrorEvent`.

### 6.2 Error messages

- `QuotaExhaustedError`: "The daily quota of your Gemini API key is exhausted for {primary_model}. Wait for the quota reset (midnight Pacific time), switch to `LLM_PROFILE=free`, or enable billing. (thread {thread_id})"
- `BigQueryUnavailableError`: "The data source is unavailable right now. Please try again in a few minutes. (thread {thread_id})"
- Anything else: "Something went wrong while answering. The conversation is intact; you can rephrase or try again. (thread {thread_id})"

### 6.3 Confirmation interrupt

- The `confirm_delete` node calls `interrupt({"type": "confirm_delete", "prompt": ..., "reports": [...]})` after storing `pending_delete`. The facade turns the `__interrupt__` update into a `ConfirmRequest`.
- When the thread's checkpoint has a pending interrupt, the next `send(text)` resumes with `Command(resume=text)` instead of starting a fresh input.
- In the node, a resume value `v` is handled like this:
  - `is_confirmation(v)` → `execute_delete`, then `response.kind = "deleted"`.
  - `v == CANCEL_SENTINEL` → clear `pending_delete` and end without a response. The facade sends this when leaving the thread (`login`, `new_thread`, `resume` of another thread).
  - Anything else → clear `pending_delete`, append "Deletion cancelled." to `notes`, set `user_input = mask_input(v)`, and route to `mask_input`/`rewrite` as a normal turn of the same thread. The reply is its own user turn: the node sets `turn_no + 1`, a new `turn_id`, a fresh budget and deadline, and one `turn_summary` is written for it. The turn that asked for the confirmation has already written its own summary (`confirm_pending`). `finalize` copies `notes` into `response.notes`.
- `confirm_delete` leaves through `Command(goto=...)`: `finalize` (deleted), `mask_input` (new turn) or `END` (cancel). The cancel path writes a `session` record with `detail.action = "pending_delete_cancelled"` and no `turn_summary`.

### 6.4 CLI (`cli/app.py`, `__main__.py`)

```python
def parse_command(line: str) -> Command | None: ...   # None = not a slash command
@dataclass(frozen=True)
class Command:
    name: Literal["login", "new", "resume", "trace", "feedback", "quit", "help"]
    args: tuple[str, ...]
def main(argv: Sequence[str] | None = None) -> int: ...   # python -m retail_agent [--exec ID] [--profile NAME]
```

- Unknown slash command → a help line.
- `/feedback good|bad [comment]` → `AgentApp.feedback`.
- No TTY → plain text with no spinner: one line per `ProgressEvent`, prefixed `... `.

---

## 7. Prompt files (`content/prompts/*.md`)

Each file starts with a header block, which `load_prompt` strips before rendering:

```markdown
<!--
prompt: classify
role: cheap
placeholders: {{question}}, {{compact_catalog}}, ...
output: ClassifyOutput (structured)
-->
```

| File | Role | Used by | Output |
|---|---|---|---|
| `safety.md` | — | every system prompt, first | — |
| `rewrite.md` | cheap | `rewrite` | plain text: the standalone question |
| `classify.md` | cheap | `classify` | `ClassifyOutput` |
| `schema_answer.md` | cheap | `schema_answer` | markdown text |
| `analysis.md` | expensive | `run_analysis(mode="analysis")` | tool calls + `final_answer` |
| `report.md` | expensive | `run_analysis(mode="report_generation")` | tool calls + `final_answer` |
| `report_selection.md` | cheap | `select_reports_by_entity` | `ReportSelectionOutput` |
| `../persona.md` | — | system prompt, last style layer | — |

---

## 8. Scripts and eval files

| Script | Interface |
|---|---|
| `scripts/seed_executives.py` | `[--dry-run]`; prints executives and brands |
| `scripts/inspect_trace.py` | `<thread_id> [--traces PATH] [--full]`; chronological view; `--full` disables truncation of prompts |
| `scripts/metrics.py` | `[--since ISO] [--until ISO] [--traces PATH]`; text table of the DD §15 metrics |
| `scripts/run_routing_eval.py` | `[--rpm N] [--limit N] [--file eval/routing_set.yaml]`; one classify call per case; precision/recall per intent, false refusal rate, misroutes |
| `scripts/run_smoke.py` | `[--rpm N] [--case ID] [--limit N]`; runs `eval/smoke.yaml` through `AgentApp`; pass/fail + p50/p95 per intent |
| `scripts/run_golden.py` | `[--rpm N] [--case ID] [--persona ID] [--limit N] [--judge]`; see the header of `eval/golden_set.yaml` |

`--rpm N` delays LLM calls to at most N per minute (free-tier Flash-Lite: 15, use 14). `scripts/_common.py` holds helpers shared by the eval scripts.

Eval file formats are defined in the header comment of each file in `eval/` and are binding for their loaders.
