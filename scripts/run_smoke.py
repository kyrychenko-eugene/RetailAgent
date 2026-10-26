"""End-to-end smoke dialogues with a live LLM (DD §16.1 level 3).

    python scripts/run_smoke.py [--case ID] [--limit N]

Runs eval/smoke.yaml through AgentApp on a temporary data dir (executives copied from the real
app database, so run scripts/seed_executives.py first), checks the assertions of every turn
and prints pass/fail plus p50/p95 turn latency per intent. Needs GOOGLE_API_KEY and
GOOGLE_CLOUD_PROJECT; use LLM_PROFILE=free on a free-tier key (budget: about 25 LLM calls).

Assertions are described in the header of eval/smoke.yaml. `results_brands_in_scope` is checked
on the executed SQL (every brand-scoped query carries the scope filter), because trace records
do not hold result rows. The free-text `compare` of a dialogue is printed, not checked.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Sequence

from _common import (
    throttle_llm,
    EVAL_DIR, TurnResult, build_eval_app, load_golden_set, load_smoke_set, percentile,
    resolve_values, run_turn, substitute, table, used_placeholders, fmt_ms,
)
from retail_agent.config import ConfigError

SCOPE_MARKER = "UNNEST(@brands)"


def check_turn(expect: dict, result: TurnResult, *, brand_scoped: bool) -> list[tuple[str, bool, str]]:
    """(assertion, passed, detail) for every key of `expect`."""
    checks: list[tuple[str, bool, str]] = []
    text = result.text.lower()
    summary, records = result.summary, result.records

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    for key, want in expect.items():
        if key == "intent":
            allowed = {want, *expect.get("also_accept", [])}
            add(key, summary.get("intent") in allowed, f"got {summary.get('intent')}, want {sorted(allowed)}")
        elif key == "also_accept":
            continue
        elif key == "response_kind":
            add(key, result.kind == want, f"got {result.kind}, want {want}")
        elif key == "response_kind_any":
            add(key, result.kind in want, f"got {result.kind}, want one of {want}")
        elif key == "contains_any":
            add(key, any(s.lower() in text for s in want), f"none of {want} in the response")
        elif key == "not_contains":
            hit = [s for s in want if s.lower() in text]
            add(key, not hit, f"found {hit}")
        elif key == "security_event":
            kinds = [r.get("kind") for r in records if r.get("event") == "security_event"]
            add(key, want in kinds, f"events: {kinds}")
        elif key == "no_llm_role":
            n = sum(1 for r in result.llm_calls() if r.get("role") == want)
            add(key, n == 0, f"{n} {want} calls")
        elif key == "max_llm_calls":
            for role, limit in want.items():
                n = sum(1 for r in result.llm_calls() if r.get("role") == role)
                add(f"max_llm_calls.{role}", n <= limit, f"{n} calls, max {limit}")
        elif key == "llm_nodes_present":
            used = {r.get("node") for r in result.llm_calls()}
            add(key, set(want) <= used, f"no LLM call in {sorted(set(want) - used)}")
        elif key == "response_kind_not":
            add(key, result.kind not in want, f"got {result.kind}")
        elif key == "has_number":
            add(key, bool(re.search(r"\d", text)) == bool(want), "no number in the response")
        elif key == "min_action_items":
            items = getattr(getattr(result.response, "analysis", None), "action_items", None) or []
            add(key, len(items) >= want, f"{len(items)} action items, want at least {want}")
        elif key == "max_tool_calls":
            n = sum(1 for r in records if r.get("event") == "tool_call")
            add(key, n <= want, f"{n} tool calls, max {want}")
        elif key == "llm_nodes_absent":
            used = {r.get("node") for r in result.llm_calls()}
            add(key, not used & set(want), f"LLM calls in {sorted(used & set(want))}")
        elif key == "report_count":
            add(key, len(result.reports) == want, f"got {len(result.reports)}, want {want}")
        elif key == "deleted_count":
            counts = [(r.get("detail") or {}).get("count") for r in records if r.get("event") == "deletion"]
            add(key, counts == [want], f"deletion records: {counts}")
        elif key == "notes_contains":
            notes = result.response.notes if result.response is not None else []
            add(key, any(want.lower() in n.lower() for n in notes), f"notes: {notes}")
        elif key == "results_brands_in_scope":
            if not want or not brand_scoped:
                continue
            sqls = [r.get("sql_executed") or "" for r in records if r.get("event") == "tool_call"
                    and r.get("outcome") in ("ok", "empty")]
            bad = sum(1 for s in sqls if SCOPE_MARKER not in s)
            add(key, bad == 0, f"{bad} of {len(sqls)} executed queries without the brand filter")
        else:
            add(key, False, "unknown assertion")
    return checks


def run_command(app, line: str) -> str:
    """The subset of CLI commands a dialogue may use. Returns a short description for the report."""
    parts = line.split()
    name, args = parts[0].lstrip("/").lower(), parts[1:]
    if name == "login" and args:
        app.login(args[0])
    elif name == "new":
        app.new_thread()
    elif name == "feedback" and args and args[0] in ("good", "bad"):
        app.feedback(args[0], " ".join(args[1:]) or None)
    else:
        raise ValueError(f"unsupported command in a dialogue: {line}")
    return line


def run_dialogue(app, dialogue: dict, resolvers: dict, catalog) -> dict:
    """Returns {"id", "turns": [{"label", "checks", "intent", "latency_ms", "llm_calls"}], "error"}."""
    persona = dialogue["persona"]
    out: dict = {"id": dialogue["id"], "title": dialogue.get("title", ""), "turns": [], "error": None}
    needed = set()
    for t in dialogue["turns"]:
        needed |= used_placeholders(t.get("say", ""))
    values: dict = {}
    try:
        if needed:
            values = resolve_values(sorted(needed), resolvers, bq=app.deps.bq, settings=app.deps.settings,
                                    catalog=catalog, identity=app.deps.identity, exec_id=persona)
        app.login(persona)
        for i, turn in enumerate(dialogue["turns"], 1):
            if "command" in turn:
                out["turns"].append({"label": f"turn {i}: {run_command(app, turn['command'])}", "checks": [],
                                     "intent": None, "latency_ms": None, "llm_calls": 0})
                continue
            text = substitute(turn["say"], values)
            result = run_turn(app, text)
            scope = app.current_exec.scope
            checks = check_turn(turn.get("expect", {}), result, brand_scoped=not scope.all_brands)
            out["turns"].append({
                "label": f"turn {i}: {text[:70]}", "checks": checks, "intent": result.summary.get("intent"),
                "latency_ms": result.summary.get("latency_ms"), "llm_calls": result.summary.get("llm_calls", 0),
                "kind": result.kind})
    except Exception as exc:   # an eval run reports the failure and goes on with the next dialogue
        out["error"] = f"{type(exc).__name__}: {exc}"
    if "compare" in dialogue:
        out["compare"] = dialogue["compare"]
    return out


def dialogue_passed(d: dict) -> bool:
    return d["error"] is None and all(ok for t in d["turns"] for _, ok, _ in t["checks"])


def latency_table(dialogues: Sequence[dict]) -> str:
    by_intent: dict[str, list[float]] = defaultdict(list)
    for d in dialogues:
        for t in d["turns"]:
            if t["latency_ms"] is not None:
                by_intent[t["intent"] or "(none)"].append(t["latency_ms"])
    return table(["intent", "turns", "p50", "p95"],
                 [[i, len(v), fmt_ms(percentile(v, 50)), fmt_ms(percentile(v, 95))] for i, v in sorted(by_intent.items())])


def format_report(dialogues: Sequence[dict]) -> str:
    out = []
    for d in dialogues:
        out.append(f"\n{d['id']} {'PASS' if dialogue_passed(d) else 'FAIL'}  {d['title']}")
        for t in d["turns"]:
            out.append(f"  {t['label']}")
            for name, ok, detail in t["checks"]:
                out.append(f"    {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": {detail}"))
        if d["error"]:
            out.append(f"  ERROR {d['error']}")
        if d.get("compare"):
            out.append(f"  (not checked) compare: {d['compare']}")
    passed = sum(dialogue_passed(d) for d in dialogues)
    calls = sum(t["llm_calls"] for d in dialogues for t in d["turns"])
    checks = [ok for d in dialogues for t in d["turns"] for _, ok, _ in t["checks"]]
    out.append(f"\nDialogues passed: {passed}/{len(dialogues)}; assertions passed: {sum(checks)}/{len(checks)}; "
               f"LLM calls: {calls}")
    out.append("\nTurn latency per intent\n" + latency_table(dialogues))
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", help="run only this dialogue id, e.g. SM-04")
    parser.add_argument("--limit", type=int, help="run only the first N dialogues")
    parser.add_argument("--file", type=Path, default=EVAL_DIR / "smoke.yaml")
    parser.add_argument("--rpm", type=int, default=0, help="max LLM calls per minute (free tier Flash-Lite: 14)")
    args = parser.parse_args(argv)
    try:
        dialogues = load_smoke_set(args.file)
        resolvers = load_golden_set()["resolvers"]
        if args.case:
            dialogues = [d for d in dialogues if d["id"] == args.case]
            if not dialogues:
                print(f"No dialogue '{args.case}'", file=sys.stderr)
                return 2
        if args.limit:
            dialogues = dialogues[:args.limit]
        app, work = build_eval_app(prefix="smoke")
        throttle_llm(app.deps.llm, args.rpm)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"Setup problem: {exc}", file=sys.stderr)
        return 2

    from retail_agent.config import load_catalog
    catalog = load_catalog(app.deps.settings.paths.content_dir / "catalog.yaml")
    results = []
    try:
        for d in dialogues:
            print(f"Running {d['id']} ...", file=sys.stderr)
            started = time.monotonic()
            results.append(run_dialogue(app, d, resolvers, catalog))
            print(f"  done in {time.monotonic() - started:.0f} s", file=sys.stderr)
    finally:
        trace_copy = Path(app.deps.settings.paths.traces)
        print(f"(traces of this run: {trace_copy})", file=sys.stderr)
    print(f"Smoke run, profile {app.deps.settings.profile}")
    print(format_report(results))
    # Keep the work dir: its traces are the evidence of the run. It lives under the system temp dir.
    return 0 if all(dialogue_passed(d) for d in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
