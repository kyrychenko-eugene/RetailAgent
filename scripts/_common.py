"""Helpers shared by the eval scripts: YAML loaders, percentiles, a throwaway app, placeholders.

Not a script. The scripts import it as `_common` (their own directory is on sys.path).
"""
from __future__ import annotations

import dataclasses
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from retail_agent.config import REPO_ROOT, Settings
from retail_agent.security.identity import IdentityStore

EVAL_DIR = REPO_ROOT / "eval"
PERSONAS = ("ceo", "brand_manager_1", "brand_manager_2")


# -- loaders -----------------------------------------------------------------------------------

def load_yaml(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data


def load_cases(path: Path, key: str, required: Sequence[str]) -> list[dict]:
    """The list under `key`, with every entry checked for its required fields and a unique id."""
    data = load_yaml(path)
    items = data.get(key)
    if not isinstance(items, list) or not items:
        raise ValueError(f"{path}: '{key}' must be a non-empty list")
    seen: set[str] = set()
    for item in items:
        missing = [k for k in required if k not in item]
        if missing:
            raise ValueError(f"{path}: entry {item.get('id', '?')} is missing {missing}")
        if item["id"] in seen:
            raise ValueError(f"{path}: duplicate id {item['id']}")
        seen.add(item["id"])
    return items


def load_routing_set(path: Path = EVAL_DIR / "routing_set.yaml") -> list[dict]:
    cases = load_cases(path, "cases", ("id", "message", "intent"))
    for c in cases:
        if c["intent"] == "report_library" and "library_action" not in c:
            raise ValueError(f"{path}: {c['id']} is a report_library case without library_action")
    return cases


def load_smoke_set(path: Path = EVAL_DIR / "smoke.yaml") -> list[dict]:
    dialogues = load_cases(path, "dialogues", ("id", "persona", "turns"))
    for d in dialogues:
        for turn in d["turns"]:
            if ("say" in turn) == ("command" in turn):
                raise ValueError(f"{path}: {d['id']} has a turn with neither or both of say/command")
    return dialogues


def load_golden_set(path: Path = EVAL_DIR / "golden_set.yaml") -> dict:
    data = load_yaml(path)
    data["cases"] = load_cases(path, "cases", ("id", "category", "question"))
    return data


# -- statistics --------------------------------------------------------------------------------

def percentile(values: Iterable[float], q: float) -> float | None:
    """Nearest-rank percentile, q in [0, 100]; None for no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, -(-len(ordered) * q // 100))   # ceil
    return ordered[int(rank) - 1]


def fmt_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f} ms"


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Plain text table, first column left-aligned, the rest right-aligned."""
    cells = [[str(c) for c in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]

    def line(row: Sequence[str]) -> str:
        return "  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(row, widths)))

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in cells]
    return "\n".join(out)


# -- throwaway app (smoke, golden) -------------------------------------------------------------

def make_eval_settings(settings: Settings, work_dir: Path) -> Settings:
    """Same config, but data and traces under work_dir so the eval never touches real threads or reports."""
    paths = dataclasses.replace(
        settings.paths, data_dir=work_dir, app_db=work_dir / "retail_agent.sqlite",
        checkpoint_db=work_dir / "checkpoints.sqlite", traces=work_dir / "traces.jsonl")
    return dataclasses.replace(settings, paths=paths)


def copy_executives(source_db: Path, target_db: Path) -> list[str]:
    """Copy the seeded executives (and their brands) into the eval database. Returns their ids."""
    if not Path(source_db).exists():
        raise FileNotFoundError(f"{source_db} not found; run scripts/seed_executives.py first")
    source, target = IdentityStore(source_db), IdentityStore(target_db)
    copied = []
    for exec_id in PERSONAS:
        e = source.get_executive(exec_id)
        target.upsert_executive(e.exec_id, e.display_name, scope_all=e.scope.all_brands, brands=e.scope.brands)
        copied.append(exec_id)
    return copied


def new_work_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"retail_agent_{prefix}_"))


def remove_work_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


# -- placeholders and reference SQL ------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def sql_escape(value: str) -> str:
    """BigQuery-style escaping for a value inside a string literal: backslash before ' and \\."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def substitute(text: str, values: dict[str, Any], *, escape: bool = False) -> str:
    """Replace {name} for the known names only; other braces are left alone."""
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in values:
            return m.group(0)
        s = str(values[name])
        return sql_escape(s) if escape else s
    return _PLACEHOLDER.sub(repl, text)


def used_placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def run_scoped(bq, settings: Settings, catalog, scope, sql: str, *, label: str) -> list[dict]:
    """Run trusted eval SQL through the scoped CTEs (never directly on the base tables)."""
    from retail_agent.security.scoped_sql import build_scoped_sql
    scoped = build_scoped_sql(sql, scope, catalog)
    result = bq.run_query(scoped.sql, params=scoped.params, max_bytes_billed=settings.bigquery.bytes_cap,
                          timeout_s=settings.bigquery.query_timeout_s,
                          labels={"app": "retail_agent", "exec_id": "eval", "purpose": label})
    return result.rows


def resolve_values(resolver_names: Sequence[str], resolvers: dict, *, bq, settings: Settings, catalog,
                   identity: IdentityStore, exec_id: str) -> dict[str, Any]:
    """Placeholder values for one persona. Raises LookupError when a resolver is not defined for it."""
    executive = identity.get_executive(exec_id)
    values: dict[str, Any] = {}
    for name in resolver_names:
        spec = resolvers[name]
        if spec.get("source") == "identity":
            if name != "other_brand":
                raise LookupError(f"resolver {name}: unsupported identity source")
            others = {"brand_manager_1": "brand_manager_2", "brand_manager_2": "brand_manager_1"}
            if exec_id not in others:
                raise LookupError(f"{name} is not defined for {exec_id}")
            values["other_brand"] = identity.get_executive(others[exec_id]).scope.brands[0]
            continue
        rows = run_scoped(bq, settings, catalog, executive.scope, spec["sql"], label=f"resolve_{name}")
        if not rows or any(rows[0].get(o) is None for o in spec["outputs"]):
            raise LookupError(f"resolver {name} returned no value for {exec_id}")
        values.update({o: rows[0][o] for o in spec["outputs"]})
    return values


# -- running one turn and reading what it left behind -------------------------------------------

@dataclasses.dataclass
class TurnResult:
    kind: str                       # AssistantResponse.kind, "confirm_request" or "error"
    text: str                       # everything the user would read
    response: Any                   # AssistantResponse | None
    reports: list                   # ReportSummary list of a ConfirmRequest / library answer
    records: list[dict]             # trace records written during the turn
    events: list

    @property
    def summary(self) -> dict:
        return next((r for r in reversed(self.records) if r.get("event") == "turn_summary"), {})

    def llm_calls(self) -> list[dict]:
        return [r for r in self.records if r.get("event") == "llm_call"]


def run_turn(app, text: str) -> TurnResult:
    """Send one message through AgentApp and collect the events and the trace records it produced."""
    from retail_agent.agent.state import ConfirmRequest, ErrorEvent, FinalResponse
    from retail_agent.analysis.output import render_markdown
    from retail_agent.tracing import read_traces

    wait = getattr(app.deps.llm, "wait_for_turn", None)
    if wait is not None:     # throttle between turns, outside the turn deadline
        wait()
    path = app.deps.settings.paths.traces
    before = sum(1 for _ in read_traces(path))
    events = list(app.send(text))
    records = list(read_traces(path))[before:]
    last = events[-1]
    response, reports = None, []
    if isinstance(last, FinalResponse):
        response, kind = last.response, last.response.kind
        reports = list(response.reports)
        parts = [response.text or ""]
        if response.analysis is not None:
            parts.append(render_markdown(response.analysis))
        parts += [r.title for r in reports] + list(response.notes)
        shown = "\n".join(p for p in parts if p)
    elif isinstance(last, ConfirmRequest):
        kind, reports = "confirm_request", list(last.reports)
        shown = "\n".join([last.prompt] + [r.title for r in reports])
    else:
        kind = "error"
        shown = last.message if isinstance(last, ErrorEvent) else ""
    return TurnResult(kind=kind, text=shown, response=response, reports=reports, records=records, events=events)


def build_eval_app(*, prefix: str):
    """A real AgentApp (live LLM and BigQuery) on a temporary data dir with the seeded executives.
    Returns (app, work_dir). Caller removes work_dir (it also holds the traces)."""
    from retail_agent.cli.app import build_app
    from retail_agent.config import load_settings

    real = load_settings()
    work = new_work_dir(prefix)
    settings = make_eval_settings(real, work)
    copy_executives(real.paths.app_db, settings.paths.app_db)
    return build_app(settings), work


TURN_HEADROOM = 6     # calls a turn may make without waiting inside it (rewrite, classify, first analysis steps)


def throttle_llm(llm, rpm: int, *, clock=None, sleep=None) -> None:
    """Keep the client under `rpm` LLM calls per minute (sliding window) by delaying call_llm.
    Free-tier Flash-Lite allows 15 per minute. rpm <= 0 leaves the client alone.
    Also sets `llm.wait_for_turn()`, which run_turn calls before a turn: it waits until the window
    has room for TURN_HEADROOM calls, so waits fall between turns and not inside a turn's deadline
    (a wait inside `rewrite` could use up the 30 s pre-routing deadline)."""
    import time
    from collections import deque
    if rpm <= 0:
        return
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    sent: deque[float] = deque()
    original = llm.call_llm

    def limited(*args, **kwargs):
        now = clock()
        while sent and now - sent[0] >= 60:
            sent.popleft()
        if len(sent) >= rpm:
            sleep(60 - (now - sent[0]) + 0.1)
            now = clock()
            while sent and now - sent[0] >= 60:
                sent.popleft()
        sent.append(now)
        return original(*args, **kwargs)

    def wait_for_turn() -> None:
        now = clock()
        while sent and now - sent[0] >= 60:
            sent.popleft()
        room = max(1, min(TURN_HEADROOM, rpm))
        if len(sent) > rpm - room:
            sleep(60 - (now - sent[len(sent) - (rpm - room) - 1]) + 0.1)

    llm.call_llm = limited
    llm.wait_for_turn = wait_for_turn
