"""Configuration: config.yaml (profiles, limits), environment overrides, and content loaders.

Model IDs, budgets, limits, timeouts and paths come from here only (docs/contracts.md §1.1, §1.2).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"

# Values copied from .env.example that must be treated as "not set".
PLACEHOLDER_VALUES = frozenset({"your-gemini-api-key", "your-gcp-project-id"})


class ConfigError(Exception):
    """Readable message naming the key, file and expected value."""


@dataclass(frozen=True)
class ModelSpec:
    provider: str            # "google" | "anthropic"
    model: str               # e.g. "gemini-3.8-flash"


@dataclass(frozen=True)
class LLMSettings:
    temperature: float
    call_timeout_s: float
    max_retries: int
    backoff_base_s: float
    backoff_max_s: float
    min_time_for_retry_s: float
    min_call_timeout_s: float = 0.0     # provider floor for a request deadline (Gemini: 10 s)


@dataclass(frozen=True)
class TurnSettings:
    deadline_s: dict[str, float]
    synthesis_reserve_s: float
    tool_budget: dict[str, int]
    history_turns: int
    max_consecutive_clarifications: int
    pii_attempts_before_stop: int

    def deadline_for(self, intent: str) -> float:
        return self.deadline_s.get(intent, self.deadline_s["default"])

    def budget_for(self, intent: str) -> int:
        return self.tool_budget.get(intent, 0)


@dataclass(frozen=True)
class BigQuerySettings:
    project: str | None
    dataset: str
    location: str
    bytes_cap: int
    query_timeout_s: float
    max_rows_to_model: int
    column_values_limit: int
    max_cell_chars: int


@dataclass(frozen=True)
class PathSettings:
    data_dir: Path
    app_db: Path
    checkpoint_db: Path
    traces: Path
    content_dir: Path


@dataclass(frozen=True)
class EvalSettings:
    judge_role: str
    judge_model: ModelSpec | None


@dataclass(frozen=True)
class Settings:
    profile: str
    roles: dict[str, tuple[ModelSpec, ...]]
    provider_key_env: dict[str, str]
    llm: LLMSettings
    turn: TurnSettings
    bigquery: BigQuerySettings
    paths: PathSettings
    tracing_max_field_chars: int
    prices: dict[str, tuple[float, float]]
    eval: EvalSettings


# ---------------------------------------------------------------------------
# Loading helpers


class _Reader:
    """Typed access to a nested mapping with errors that name the key and the file."""

    def __init__(self, data: Mapping[str, Any], file: Path) -> None:
        self.data = data
        self.file = file

    def _fail(self, key: str, expected: str, value: Any = None) -> ConfigError:
        got = "missing" if value is None else f"got {value!r}"
        return ConfigError(f"{self.file}: key '{key}' must be {expected} ({got})")

    def get(self, key: str, expected: type | tuple[type, ...], label: str, *, optional: bool = False) -> Any:
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, Mapping) or part not in node:
                if optional:
                    return None
                raise self._fail(key, label)
            node = node[part]
        if node is None and optional:
            return None
        # bool is an int subclass; never accept it as a number.
        if isinstance(node, bool) or not isinstance(node, expected):
            raise self._fail(key, label, node)
        return node

    def number(self, key: str) -> float:
        return float(self.get(key, (int, float), "a number"))

    def integer(self, key: str) -> int:
        return int(self.get(key, int, "an integer"))

    def string(self, key: str) -> str:
        return self.get(key, str, "a non-empty string")

    def mapping(self, key: str) -> Mapping[str, Any]:
        return self.get(key, Mapping, "a mapping")


def _model_spec(raw: Any, key: str, file: Path) -> ModelSpec:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("provider"), str) or not isinstance(raw.get("model"), str):
        raise ConfigError(f"{file}: '{key}' entries must be {{provider: <name>, model: <id>}} (got {raw!r})")
    return ModelSpec(provider=raw["provider"], model=raw["model"])


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    """Value of an environment variable, or None when unset, empty or a .env.example placeholder."""
    value = env.get(name, "").strip()
    return None if not value or value in PLACEHOLDER_VALUES else value


def _provider_key_present(env: Mapping[str, str], key_env: str) -> bool:
    return _env_value(env, key_env) is not None


def load_settings(path: Path | None = None, env: Mapping[str, str] | None = None) -> Settings:
    """Load config.yaml (default: repo root), apply the profile and env overrides, validate.
    Loads `.env` (python-dotenv) only when env is None. Raises ConfigError."""
    file = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    root = file.resolve().parent
    if env is None:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
        env = os.environ

    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {file}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{file}: invalid YAML: {exc}") from None
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{file}: the top level must be a mapping")
    r = _Reader(raw, file)

    # Profile and model roles
    profiles = r.mapping("profiles")
    profile = env.get("LLM_PROFILE", "").strip() or r.string("default_profile")
    if profile not in profiles:
        raise ConfigError(
            f"LLM_PROFILE / default_profile '{profile}' is unknown in {file}; "
            f"expected one of: {', '.join(sorted(profiles))}"
        )
    role_cfg = r.mapping(f"profiles.{profile}.roles")
    provider_key_env = {
        name: _Reader(cfg, file).string("api_key_env") if isinstance(cfg, Mapping) else ""
        for name, cfg in r.mapping("providers").items()
    }
    if not all(provider_key_env.values()):
        raise ConfigError(f"{file}: every entry of 'providers' needs an 'api_key_env' string")

    extra_raw = r.get("extra_fallbacks", list, "a list", optional=True) or []
    extras = [_model_spec(e, "extra_fallbacks", file) for e in extra_raw]
    for spec in extras:
        if spec.provider not in provider_key_env:
            raise ConfigError(f"{file}: extra_fallbacks provider '{spec.provider}' is not listed under 'providers'")
    active_extras = tuple(s for s in extras if _provider_key_present(env, provider_key_env[s.provider]))

    roles: dict[str, tuple[ModelSpec, ...]] = {}
    for role in ("cheap", "expensive"):
        chain = role_cfg.get(role)
        if not isinstance(chain, list) or not chain:
            raise ConfigError(f"{file}: key 'profiles.{profile}.roles.{role}' must be a non-empty list of models")
        specs = [_model_spec(e, f"profiles.{profile}.roles.{role}", file) for e in chain]
        for spec in specs:
            if spec.provider not in provider_key_env:
                raise ConfigError(f"{file}: provider '{spec.provider}' in role '{role}' is not listed under 'providers'")
        override = _env_value(env, f"RA_MODEL_{role.upper()}")
        if override:
            specs[0] = ModelSpec("google", override)
        roles[role] = tuple(specs) + tuple(s for s in active_extras if s not in specs)

    llm = LLMSettings(
        temperature=r.number("llm.temperature"),
        call_timeout_s=r.number("llm.call_timeout_s"),
        max_retries=r.integer("llm.max_retries"),
        backoff_base_s=r.number("llm.backoff_base_s"),
        backoff_max_s=r.number("llm.backoff_max_s"),
        min_time_for_retry_s=r.number("llm.min_time_for_retry_s"),
        min_call_timeout_s=r.number("llm.min_call_timeout_s"),
    )

    deadlines = {k: float(v) for k, v in r.mapping("turn.deadline_s").items()}
    if "default" not in deadlines:
        raise ConfigError(f"{file}: key 'turn.deadline_s.default' must be a number (missing)")
    turn = TurnSettings(
        deadline_s=deadlines,
        synthesis_reserve_s=r.number("turn.synthesis_reserve_s"),
        tool_budget={k: int(v) for k, v in r.mapping("turn.tool_budget").items()},
        history_turns=r.integer("turn.history_turns"),
        max_consecutive_clarifications=r.integer("turn.max_consecutive_clarifications"),
        pii_attempts_before_stop=r.integer("turn.pii_attempts_before_stop"),
    )

    bytes_cap_raw = _env_value(env, "RA_BYTES_CAP")
    try:
        bytes_cap = int(bytes_cap_raw) if bytes_cap_raw else r.integer("bigquery.bytes_cap")
    except ValueError:
        raise ConfigError(f"RA_BYTES_CAP must be an integer (got {bytes_cap_raw!r})") from None
    if bytes_cap <= 0:
        raise ConfigError("bigquery.bytes_cap (or RA_BYTES_CAP) must be a positive integer")
    bigquery = BigQuerySettings(
        project=_env_value(env, r.string("bigquery.project_env")),
        dataset=r.string("bigquery.dataset"),
        location=r.string("bigquery.location"),
        bytes_cap=bytes_cap,
        query_timeout_s=r.number("bigquery.query_timeout_s"),
        max_rows_to_model=r.integer("bigquery.max_rows_to_model"),
        column_values_limit=r.integer("bigquery.column_values_limit"),
        max_cell_chars=r.integer("bigquery.max_cell_chars"),
    )

    def resolve(p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (root / p).resolve()

    data_dir_override = _env_value(env, "RA_DATA_DIR")
    if data_dir_override:
        data_dir = resolve(data_dir_override)
        app_db = data_dir / Path(r.string("paths.app_db")).name
        checkpoint_db = data_dir / Path(r.string("paths.checkpoint_db")).name
    else:
        data_dir = resolve(r.string("paths.data_dir"))
        app_db = resolve(r.string("paths.app_db"))
        checkpoint_db = resolve(r.string("paths.checkpoint_db"))
    traces_override = _env_value(env, "RA_TRACES_PATH")
    paths = PathSettings(
        data_dir=data_dir,
        app_db=app_db,
        checkpoint_db=checkpoint_db,
        traces=resolve(traces_override or r.string("paths.traces")),
        content_dir=resolve(r.string("paths.content_dir")),
    )

    prices: dict[str, tuple[float, float]] = {}
    for model, p in r.mapping("prices").items():
        pr = _Reader(p if isinstance(p, Mapping) else {}, file)
        prices[model] = (pr.number("input"), pr.number("output"))

    judge_raw = r.get("eval.judge_model", Mapping, "a {provider, model} mapping or null", optional=True)
    judge_role = r.string("eval.judge_role")
    if judge_role not in roles:
        raise ConfigError(f"{file}: key 'eval.judge_role' must be one of {sorted(roles)} (got {judge_role!r})")
    eval_settings = EvalSettings(
        judge_role=judge_role,
        judge_model=_model_spec(judge_raw, "eval.judge_model", file) if judge_raw else None,
    )

    return Settings(
        profile=profile,
        roles=roles,
        provider_key_env=provider_key_env,
        llm=llm,
        turn=turn,
        bigquery=bigquery,
        paths=paths,
        tracing_max_field_chars=r.integer("tracing.max_field_chars"),
        prices=prices,
        eval=eval_settings,
    )


def require_api_key(settings: Settings, provider: str, env: Mapping[str, str] | None = None) -> str:
    """Return the key or raise ConfigError("GOOGLE_API_KEY is not set ..."). Never logs it."""
    if provider not in settings.provider_key_env:
        raise ConfigError(f"Unknown provider '{provider}'; expected one of: {', '.join(sorted(settings.provider_key_env))}")
    name = settings.provider_key_env[provider]
    value = _env_value(os.environ if env is None else env, name)
    if value is None:
        raise ConfigError(
            f"{name} is not set (or still has the .env.example placeholder). "
            f"Put your {provider} API key in .env as {name}=... (see .env.example)."
        )
    return value


# ---------------------------------------------------------------------------
# Content loaders (configuration that is not code)


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    description: str
    categorical: bool = False
    high_cardinality: bool = False
    customer_identifier: bool = False
    demographic: bool = False


@dataclass(frozen=True)
class Table:
    name: str                     # logical name == CTE name
    source: str                   # base table name in the dataset
    description: str
    columns: tuple[Column, ...]   # CTE column order
    scope_filter: str | None      # "brand" for products
    join: str | None              # "products" for order_items
    filter: str | None            # SQL predicate text from catalog.yaml


@dataclass(frozen=True)
class Catalog:
    dataset: str
    tables: dict[str, Table]
    blocked_columns: frozenset[str]        # lower-case; NEVER rendered into prompts
    derived_metrics: tuple[str, ...]

    def column(self, table: str, column: str) -> Column | None:
        t = self.tables.get(table.lower())
        if t is None:
            return None
        wanted = column.lower()
        return next((c for c in t.columns if c.name.lower() == wanted), None)

    def allowed_columns(self) -> dict[str, frozenset[str]]:
        return {name: frozenset(c.name.lower() for c in t.columns) for name, t in self.tables.items()}

    def schema_prompt(self) -> str:
        """Tables, columns, types and descriptions (analysis, schema answer)."""
        blocks = []
        for t in self.tables.values():
            lines = [f"### {t.name}", " ".join(t.description.split())]
            for c in t.columns:
                flags = []
                if c.categorical:
                    flags.append("categorical, high-cardinality" if c.high_cardinality else "categorical")
                suffix = f" [{'; '.join(flags)}]" if flags else ""
                lines.append(f"- {c.name} ({c.type}){suffix}: {' '.join(c.description.split())}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def compact_prompt(self) -> str:
        """`table: col, col, ...` per table plus the derived metrics (classify)."""
        lines = [f"{t.name}: {', '.join(c.name for c in t.columns)}" for t in self.tables.values()]
        lines.append("Derived metrics: " + ", ".join(self.derived_metrics))
        return "\n".join(lines)


def load_catalog(path: Path) -> Catalog:
    """Load content/catalog.yaml. Raises ConfigError."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Catalog file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(raw, Mapping) or not isinstance(raw.get("tables"), Mapping) or not raw["tables"]:
        raise ConfigError(f"{path}: key 'tables' must be a non-empty mapping")
    if not isinstance(raw.get("dataset"), str):
        raise ConfigError(f"{path}: key 'dataset' must be a string")

    tables: dict[str, Table] = {}
    for name, t in raw["tables"].items():
        if not isinstance(t, Mapping) or not isinstance(t.get("source"), str) or not isinstance(t.get("columns"), list):
            raise ConfigError(f"{path}: table '{name}' needs 'source' (string) and 'columns' (list)")
        columns = []
        for c in t["columns"]:
            if not isinstance(c, Mapping) or not isinstance(c.get("name"), str) or not isinstance(c.get("type"), str):
                raise ConfigError(f"{path}: every column of table '{name}' needs 'name' and 'type' strings (got {c!r})")
            columns.append(
                Column(
                    name=c["name"],
                    type=c["type"],
                    description=str(c.get("description", "")),
                    categorical=bool(c.get("categorical", False)),
                    high_cardinality=bool(c.get("high_cardinality", False)),
                    customer_identifier=bool(c.get("customer_identifier", False)),
                    demographic=bool(c.get("demographic", False)),
                )
            )
        if not columns:
            raise ConfigError(f"{path}: table '{name}' has no columns")
        tables[name.lower()] = Table(
            name=name.lower(),
            source=t["source"],
            description=str(t.get("description", "")),
            columns=tuple(columns),
            scope_filter=t.get("scope_filter"),
            join=t.get("join"),
            filter=t.get("filter"),
        )

    blocked = frozenset(str(b).lower() for b in raw.get("blocked_columns") or [])
    allowed = {c.name.lower() for t in tables.values() for c in t.columns}
    overlap = blocked & allowed
    if overlap:
        raise ConfigError(f"{path}: blocked_columns must not appear in tables: {sorted(overlap)}")
    return Catalog(
        dataset=raw["dataset"],
        tables=tables,
        blocked_columns=blocked,
        derived_metrics=tuple(str(m) for m in raw.get("derived_metrics") or []),
    )


def load_text(path: Path) -> str:
    """Read glossary.md, persona.md or a prompt file."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Content file not found: {path}") from None
