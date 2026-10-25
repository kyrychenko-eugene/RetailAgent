"""Agent health metrics from the trace file (DD §15, R6).

    python scripts/metrics.py [--since ISO] [--until ISO] [--traces PATH]

Answers "is the agent failing, and why". The window is on the record timestamp: --since is
inclusive, --until exclusive; both accept an ISO date or date-time (UTC when no zone is given).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from _common import fmt_ms, percentile, table
from retail_agent.config import ConfigError, load_settings
from retail_agent.tracing import read_traces


def parse_when(text: str) -> datetime:
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def record_time(record: Mapping) -> datetime | None:
    ts = record.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return parse_when(ts)
    except ValueError:
        return None


def in_window(records: Iterable[dict], since: datetime | None, until: datetime | None) -> list[dict]:
    out = []
    for r in records:
        when = record_time(r)
        if when is None or (since and when < since) or (until and when >= until):
            continue
        out.append(r)
    return out


def _rate(n: int, total: int) -> float:
    return n / total if total else 0.0


def estimate_cost(llm_calls: Iterable[Mapping], prices: Mapping[str, tuple[float, float]]) -> tuple[float, set[str]]:
    """USD from the price table (per 1M tokens); also the models that have no price."""
    total, unpriced = 0.0, set()
    for r in llm_calls:
        price = prices.get(r.get("model"))
        if price is None:
            unpriced.add(str(r.get("model")))
            continue
        total += (r.get("tokens_in") or 0) * price[0] / 1e6 + (r.get("tokens_out") or 0) * price[1] / 1e6
    return total, unpriced


def compute_metrics(records: Sequence[dict], prices: Mapping[str, tuple[float, float]] | None = None) -> dict:
    prices = prices or {}
    turns = [r for r in records if r.get("event") == "turn_summary"]
    llm = [r for r in records if r.get("event") == "llm_call"]
    n = len(turns)
    latency_by_intent: dict[str, list[float]] = defaultdict(list)
    for t in turns:
        if t.get("latency_ms") is not None:
            latency_by_intent[t.get("intent") or "(none)"].append(t["latency_ms"])
    cost, unpriced = estimate_cost(llm, prices)
    return {
        "turns": n,
        "outcomes": Counter(t.get("outcome") for t in turns),
        "turn_errors": Counter(t["error_type"] for t in turns if t.get("error_type")),
        "attempt_errors": Counter(r["error_type"] for r in records
                                  if r.get("event") in ("llm_call", "tool_call") and r.get("error_type")),
        "error_rate": _rate(sum(1 for t in turns if t.get("outcome") == "error"), n),
        "fallback_rate": _rate(sum(1 for t in turns if t.get("fallback_used")), n),
        "budget_rate": _rate(sum(1 for t in turns if t.get("budget_exhausted")), n),
        "deadline_rate": _rate(sum(1 for t in turns if t.get("deadline_hit")), n),
        "clarification_rate": _rate(sum(1 for t in turns if t.get("outcome") == "clarification"), n),
        "security_events": Counter(r.get("kind") for r in records if r.get("event") == "security_event"),
        "latency": {i: (len(v), percentile(v, 50), percentile(v, 95)) for i, v in sorted(latency_by_intent.items())},
        "llm_calls_per_turn": _rate(sum(t.get("llm_calls") or 0 for t in turns), n),
        "tool_calls_per_turn": _rate(sum(t.get("tool_calls") or 0 for t in turns), n),
        "tokens_in_per_turn": _rate(sum(t.get("tokens_in") or 0 for t in turns), n),
        "tokens_out_per_turn": _rate(sum(t.get("tokens_out") or 0 for t in turns), n),
        "cost_usd": cost,
        "cost_per_turn_usd": _rate(cost, n) if n else 0.0,
        "unpriced_models": sorted(unpriced),
        "feedback": Counter(r.get("rating") for r in records if r.get("event") == "feedback"),
        "llm_attempts": len(llm),
    }


def _counter_rows(counter: Counter) -> list[list]:
    return [[k, v] for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))] or [["(none)", 0]]


def format_metrics(m: dict, window: str = "") -> str:
    n = m["turns"]
    pct = lambda x: f"{x * 100:.1f} %"   # noqa: E731
    parts = [f"Agent metrics{window}: {n} turns, {m['llm_attempts']} LLM attempts"]
    if n == 0:
        return parts[0] + "\n(no turn_summary records in the window)"
    parts.append("\nRates (share of turns)\n" + table(["metric", "value"], [
        ["error rate", pct(m["error_rate"])], ["fallback rate", pct(m["fallback_rate"])],
        ["budget exhaustion rate", pct(m["budget_rate"])], ["deadline (partial answer) rate", pct(m["deadline_rate"])],
        ["clarification rate", pct(m["clarification_rate"])]]))
    parts.append("\nTurn outcomes\n" + table(["outcome", "turns"], _counter_rows(m["outcomes"])))
    parts.append("\nTurn errors by error_type\n" + table(["error_type", "turns"], _counter_rows(m["turn_errors"])))
    parts.append("\nFailed LLM/tool attempts by error_type (incl. retried ones)\n"
                 + table(["error_type", "attempts"], _counter_rows(m["attempt_errors"])))
    parts.append("\nSecurity events by kind\n" + table(["kind", "events"], _counter_rows(m["security_events"])))
    parts.append("\nLatency per intent\n" + table(
        ["intent", "turns", "p50", "p95"],
        [[i, c, fmt_ms(p50), fmt_ms(p95)] for i, (c, p50, p95) in m["latency"].items()]))
    cost_note = f" (no price for: {', '.join(m['unpriced_models'])})" if m["unpriced_models"] else ""
    parts.append("\nUsage per turn\n" + table(["metric", "value"], [
        ["LLM calls", f"{m['llm_calls_per_turn']:.2f}"], ["tool calls", f"{m['tool_calls_per_turn']:.2f}"],
        ["tokens in", f"{m['tokens_in_per_turn']:,.0f}"], ["tokens out", f"{m['tokens_out_per_turn']:,.0f}"],
        ["est. cost (USD)", f"{m['cost_per_turn_usd']:.5f}"], ["est. cost, window total (USD)", f"{m['cost_usd']:.4f}"]])
        + cost_note)
    parts.append("\n/feedback\n" + table(["rating", "count"], _counter_rows(m["feedback"])))
    return "\n".join(parts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", help="start of the window (inclusive), ISO date or date-time")
    parser.add_argument("--until", help="end of the window (exclusive)")
    parser.add_argument("--traces", type=Path, help="trace file (default: paths.traces from config.yaml)")
    args = parser.parse_args(argv)
    try:
        since = parse_when(args.since) if args.since else None
        until = parse_when(args.until) if args.until else None
    except ValueError as exc:
        print(f"Bad --since/--until: {exc}", file=sys.stderr)
        return 2
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    path = args.traces or settings.paths.traces
    if not Path(path).exists():
        print(f"Trace file {path} not found", file=sys.stderr)
        return 1
    records = in_window(read_traces(path), since, until)
    window = f" ({args.since or 'start'} .. {args.until or 'now'})"
    print(format_metrics(compute_metrics(records, settings.prices), window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
