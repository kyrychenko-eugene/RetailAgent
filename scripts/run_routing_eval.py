"""Routing eval (DD §16.1 level 2, R8): one cheap classify call per case of eval/routing_set.yaml.

    python scripts/run_routing_eval.py [--limit N] [--file PATH]

Reports precision and recall per intent, the false refusal rate, library_action / delete_scope
accuracy and the list of misroutes. Use LLM_PROFILE=free on a free-tier key.

Scoring: a case is correct when the routed intent equals the expected one or is in `also_accept`.
For precision and recall a case routed to an accepted intent counts as the expected intent.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Sequence

from langchain_core.messages import HumanMessage, SystemMessage

from _common import throttle_llm, EVAL_DIR, load_routing_set, table
from retail_agent.agent.nodes import _NO_CLARIFY, ClassifyOutput, validate_classification
from retail_agent.agent.prompt_assembly import load_prompt, render
from retail_agent.config import ConfigError, Settings, load_catalog, load_settings
from retail_agent.llm.errors import LLMError, QuotaExhaustedError
from retail_agent.llm.wrapper import Deadline, LLMClient, TurnBudget, TurnContext
from retail_agent.tracing import NullTracer

LEGIT = frozenset({"analysis", "report_generation", "report_library", "schema_question"})
REFUSING = frozenset({"off_topic", "malicious", "unanswerable"})


def make_classifier(settings: Settings, llm: LLMClient, catalog):
    """classify(message) -> ClassifyOutput, with the same prompt, safety message and post-validation as the graph."""
    content = settings.paths.content_dir
    safety = SystemMessage(load_prompt("safety", content))
    template = load_prompt("classify", content)
    compact = catalog.compact_prompt()
    turn = settings.turn

    def classify(message: str) -> ClassifyOutput:
        ctx = TurnContext(thread_id="routing-eval", turn_id="routing-eval", exec_id="eval", intent=None,
                          deadline=Deadline(settings.llm.call_timeout_s * 3), synthesis_reserve_s=0.0,
                          budget=TurnBudget(0), tracer=NullTracer())
        prompt = render(template, question=message, compact_catalog=compact,
                        can_clarify="" if turn.max_consecutive_clarifications > 0 else _NO_CLARIFY)
        result = llm.call_llm("cheap", [safety, HumanMessage(prompt)], ctx, node="classify", schema=ClassifyOutput)
        return validate_classification(result.parsed, message, consecutive_clarifications=0,
                                       max_clarifications=turn.max_consecutive_clarifications)
    return classify


def accepted(case: dict) -> set[str]:
    return {case["intent"], *case.get("also_accept", [])}


def score(cases: Sequence[dict], results: Sequence[dict | None]) -> dict:
    """results[i] is {"intent", "library_action", "delete_scope"} or None when the call failed."""
    labels = sorted({c["intent"] for c in cases} | {r["intent"] for r in results if r})
    tp, fp, fn = defaultdict(int), defaultdict(int), defaultdict(int)
    misroutes, errors = [], []
    legit = refused = 0
    lib_total = lib_ok = scope_total = scope_ok = 0
    for case, res in zip(cases, results):
        expected = case["intent"]
        if res is None:
            errors.append(case["id"])
            continue
        got = res["intent"]
        ok = got in accepted(case)
        effective = expected if ok else got
        if effective == expected:
            tp[expected] += 1
        else:
            fn[expected] += 1
            fp[effective] += 1
            misroutes.append({"id": case["id"], "message": case["message"], "expected": expected, "got": got,
                              "borderline": bool(case.get("borderline"))})
        if expected in LEGIT:
            legit += 1
            refused += got in REFUSING
        if expected == "report_library" and ok and "library_action" in case:
            lib_total += 1
            lib_ok += res.get("library_action") == case["library_action"]
            if "delete_scope" in case:
                scope_total += 1
                scope_ok += res.get("delete_scope") == case["delete_scope"]
    per_intent = {}
    for label in labels:
        p = tp[label] / (tp[label] + fp[label]) if tp[label] + fp[label] else None
        r = tp[label] / (tp[label] + fn[label]) if tp[label] + fn[label] else None
        per_intent[label] = {"precision": p, "recall": r, "support": tp[label] + fn[label]}
    scored = len(cases) - len(errors)
    return {
        "per_intent": per_intent, "misroutes": misroutes, "errors": errors,
        "accuracy": (scored - len(misroutes)) / scored if scored else None,
        "false_refusal_rate": refused / legit if legit else None, "legit_cases": legit, "false_refusals": refused,
        "library_action_accuracy": (lib_ok, lib_total), "delete_scope_accuracy": (scope_ok, scope_total),
    }


def format_report(s: dict, n_cases: int, profile: str) -> str:
    fmt = lambda x: "-" if x is None else f"{x * 100:.0f} %"   # noqa: E731
    out = [f"Routing eval: {n_cases} cases, profile {profile}, accuracy {fmt(s['accuracy'])}"]
    out.append(table(["intent", "cases", "precision", "recall"],
                     [[i, v["support"], fmt(v["precision"]), fmt(v["recall"])] for i, v in s["per_intent"].items()]))
    out.append(f"\nFalse refusal rate: {fmt(s['false_refusal_rate'])} "
               f"({s['false_refusals']} of {s['legit_cases']} legitimate cases routed to off_topic/malicious/unanswerable)")
    la, ds = s["library_action_accuracy"], s["delete_scope_accuracy"]
    out.append(f"library_action correct: {la[0]}/{la[1]}   delete_scope correct: {ds[0]}/{ds[1]}")
    if s["errors"]:
        out.append(f"\nCalls that failed (not scored): {', '.join(s['errors'])}")
    out.append("\nMisroutes" if s["misroutes"] else "\nNo misroutes.")
    for m in s["misroutes"]:
        flag = " (borderline)" if m["borderline"] else ""
        out.append(f"  {m['id']}{flag}: expected {m['expected']}, got {m['got']}: {m['message']}")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, help="run only the first N cases")
    parser.add_argument("--file", type=Path, default=EVAL_DIR / "routing_set.yaml")
    parser.add_argument("--rpm", type=int, default=0, help="max LLM calls per minute (free tier Flash-Lite: 14)")
    args = parser.parse_args(argv)
    try:
        cases = load_routing_set(args.file)
        settings = load_settings()
        catalog = load_catalog(settings.paths.content_dir / "catalog.yaml")
        llm = LLMClient(settings)
        throttle_llm(llm, args.rpm)
        classify = make_classifier(settings, llm, catalog)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"Setup problem: {exc}", file=sys.stderr)
        return 2
    if args.limit:
        cases = cases[:args.limit]

    results: list[dict | None] = []
    started = time.monotonic()
    for i, case in enumerate(cases, 1):
        try:
            out = classify(case["message"])
            results.append({"intent": out.intent, "library_action": out.library_action,
                            "delete_scope": out.delete_scope})
            print(f"[{i}/{len(cases)}] {case['id']}: {out.intent}", file=sys.stderr)
        except QuotaExhaustedError as exc:
            print(f"Quota exhausted after {i - 1} calls: {exc}", file=sys.stderr)
            results.extend([None] * (len(cases) - len(results)))
            break
        except LLMError as exc:
            print(f"[{i}/{len(cases)}] {case['id']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            results.append(None)
    print(format_report(score(cases, results), len(cases), settings.profile))
    print(f"\nLive LLM calls: {sum(r is not None for r in results)} classify calls, "
          f"{time.monotonic() - started:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
