"""Chronological view of one conversation from the trace file (DD §15, contracts §8).

    python scripts/inspect_trace.py <thread_id> [--traces PATH] [--full]

The thread id may be abbreviated to a unique prefix. Prompts and responses are cut to 300
characters unless --full is given; records that were already truncated when written (tracing.
max_field_chars) are flagged either way.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from retail_agent.config import ConfigError, load_settings
from retail_agent.tracing import read_traces

PREVIEW_CHARS = 300


def as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def clip(value: Any, full: bool) -> str:
    text = as_text(value)
    if full or len(text) <= PREVIEW_CHARS:
        return text
    return f"{text[:PREVIEW_CHARS]}… [+{len(text) - PREVIEW_CHARS} chars, use --full]"


def find_records(records: Sequence[dict], thread_id: str) -> tuple[list[dict], str | None]:
    """Records of the thread (exact id, else a unique prefix), in file order. Second value: the id used."""
    ids = {r.get("thread_id") for r in records if r.get("thread_id")}
    if thread_id in ids:
        chosen = thread_id
    else:
        matches = sorted(i for i in ids if i.startswith(thread_id))
        if len(matches) != 1:
            return [], None
        chosen = matches[0]
    return [r for r in records if r.get("thread_id") == chosen], chosen


def _stamp(r: dict) -> str:
    return str(r.get("ts", ""))[11:23]


def format_record(r: dict, *, full: bool) -> list[str]:
    event = r.get("event")
    head = f"  {_stamp(r)} "
    flag = " [TRUNCATED when written]" if r.get("truncated") else ""
    err = f" error_type={r['error_type']}" if r.get("error_type") else ""
    lines: list[str]
    if event == "node":
        extra = "".join(f" {k}={r[k]}" for k in ("intent", "tool_calls") if r.get(k) is not None)
        lines = [f"{head}node {r.get('node')} {r.get('latency_ms')} ms{extra}{err}"]
        if r.get("classify_reason"):
            lines.append(f"      reason: {clip(r['classify_reason'], full)}")
    elif event == "llm_call":
        retry = f" attempt={r.get('attempt')} retries={r.get('retry_count')}"
        fb = " FALLBACK" if r.get("fallback_used") else ""
        lines = [f"{head}llm {r.get('node')} {r.get('role')}/{r.get('model')} "
                 f"in={r.get('tokens_in')} out={r.get('tokens_out')} {r.get('latency_ms')} ms{retry}{fb}{err}{flag}",
                 f"      prompt:   {clip(r.get('prompt', ''), full)}",
                 f"      response: {clip(r.get('response', ''), full)}"]
    elif event == "tool_call":
        lines = [f"{head}tool {r.get('tool')} {r.get('query_id')} outcome={r.get('outcome')} "
                 f"rows={r.get('rows')} bytes={r.get('bytes_scanned')} {r.get('latency_ms')} ms{err}{flag}",
                 f"      sql: {clip(r.get('sql', ''), full)}"]
        if full and r.get("sql_executed"):
            lines.append(f"      sql_executed: {as_text(r['sql_executed'])}")
    elif event == "security_event":
        lines = [f"{head}SECURITY {r.get('kind')} reason={r.get('reason_code')} detail={clip(r.get('detail'), full)}"]
    elif event == "pii_in_input":
        lines = [f"{head}pii_in_input counts={as_text(r.get('counts'))}"]
    elif event == "deletion":
        lines = [f"{head}deletion {as_text(r.get('detail'))}"]
    elif event == "feedback":
        lines = [f"{head}feedback {r.get('rating')} {r.get('comment') or ''}".rstrip()]
    elif event == "session":
        lines = [f"{head}session {as_text(r.get('detail'))}"]
    elif event == "turn_summary":
        lines = [f"{head}TURN SUMMARY outcome={r.get('outcome')} intent={r.get('intent')} {r.get('latency_ms')} ms "
                 f"llm_calls={r.get('llm_calls')} tool_calls={r.get('tool_calls')} "
                 f"tokens={r.get('tokens_in')}/{r.get('tokens_out')} fallback={r.get('fallback_used')} "
                 f"deadline_hit={r.get('deadline_hit')} budget_exhausted={r.get('budget_exhausted')}{err}"]
    else:
        lines = [f"{head}{event} {clip(r, full)}"]
    return lines


def render_thread(records: Sequence[dict], thread_id: str, *, full: bool = False) -> str:
    out = [f"Thread {thread_id}  exec={records[0].get('exec_id')}  {len(records)} records"]
    current: object = object()
    for r in records:
        turn = r.get("turn_id")
        if turn != current:
            current = turn
            out.append(f"\n== turn {turn or '(outside a turn)'} ==")
        out.extend(format_record(r, full=full))
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("thread_id")
    parser.add_argument("--traces", type=Path, help="trace file (default: paths.traces from config.yaml)")
    parser.add_argument("--full", action="store_true", help="do not shorten prompts, responses and SQL")
    args = parser.parse_args(argv)
    path = args.traces
    if path is None:
        try:
            path = load_settings().paths.traces
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
    records, chosen = find_records(list(read_traces(path)), args.thread_id)
    if chosen is None:
        print(f"No (unique) thread matching '{args.thread_id}' in {path}", file=sys.stderr)
        return 1
    print(render_thread(records, chosen, full=args.full))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
