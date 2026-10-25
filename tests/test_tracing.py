from __future__ import annotations

import json
from pathlib import Path

import pytest

from retail_agent.tracing import NullTracer, Tracer, read_traces, validate_record


def _tracer(tmp_path: Path, **kw) -> Tracer:
    return Tracer(tmp_path / "logs" / "t.jsonl", **kw)


def test_write_appends_jsonl_with_bound_context(tmp_path: Path):
    t = _tracer(tmp_path).bind(thread_id="th", exec_id="ceo")
    child = t.bind(turn_id="th-0001", persona_hash="abc", node="classify")
    child.write("node", latency_ms=5)
    t.write("session", detail={"action": "login"})
    lines = (tmp_path / "logs" / "t.jsonl").read_text().splitlines()
    first, second = map(json.loads, lines)
    assert first["thread_id"] == "th" and first["turn_id"] == "th-0001" and first["node"] == "classify"
    assert first["persona_hash"] == "abc" and first["schema_version"] == 1 and first["ts"].endswith("Z")
    assert second["turn_id"] is None and "node" not in second     # parent unaffected by bind


def test_every_event_helper_validates(tmp_path: Path):
    t = _tracer(tmp_path).bind(thread_id="th", turn_id="th-0001", exec_id="ceo", node="analysis")
    records = [
        t.write("node", latency_ms=1),
        t.write("llm_call", model="m", role="cheap", prompt=[{"role": "user", "content": "q"}], response="r",
                tokens_in=1, tokens_out=1, latency_ms=1, attempt=1, retry_count=0, fallback_used=False),
        t.write("tool_call", tool="run_sql", query_id="Q1", outcome="ok", latency_ms=1, sql="SELECT 1"),
        t.security_event("pii_column", "pii_column", column="email"),
        t.pii_in_input({"EMAIL": 1}),
        t.write("deletion", detail={"report_ids": ["a"], "count": 1}),
        t.feedback("good", None),
        t.turn_summary(outcome="answered", latency_ms=1, llm_calls=1, tool_calls=1, tokens_in=1, tokens_out=1),
    ]
    for r in records:
        assert validate_record(r) == [], r
    assert [r["event"] for r in read_traces(tmp_path / "logs" / "t.jsonl")][-1] == "turn_summary"


def test_validate_record_reports_problems():
    assert validate_record({"schema_version": 1, "ts": "2026-01-01T00:00:00Z", "event": "bogus"})
    problems = validate_record({"schema_version": 2, "ts": "x", "event": "feedback"})
    assert any("schema_version" in p for p in problems) and any("ts" in p for p in problems)


def test_unknown_event_kind_and_outcome_rejected(tmp_path: Path):
    t = _tracer(tmp_path)
    with pytest.raises(ValueError):
        t.write("nope")
    with pytest.raises(ValueError):
        t.security_event("made_up")
    with pytest.raises(ValueError):
        t.turn_summary(outcome="made_up")
    with pytest.raises(ValueError):
        t.bind(colour="red")


def test_secrets_are_redacted_everywhere(tmp_path: Path):
    secret = "AIzaSy-super-secret-key"
    t = _tracer(tmp_path, secret_values=[secret, "short"])
    rec = t.write("llm_call", prompt=[{"role": "user", "content": f"key={secret}"}],
                  response={"text": f"{secret} and short"}, model="m", role="cheap")
    raw = (tmp_path / "logs" / "t.jsonl").read_text()
    assert secret not in raw and "<REDACTED>" in raw
    assert "short" in raw                              # values under 8 characters are ignored
    assert secret not in json.dumps(rec)


def test_long_fields_are_truncated_and_flagged(tmp_path: Path):
    t = _tracer(tmp_path, max_field_chars=50)
    rec = t.write("tool_call", sql="x" * 200, tool="run_sql", query_id="Q1", outcome="ok", latency_ms=1)
    assert len(rec["sql"]) == 50 and rec["truncated"] is True
    short = t.write("tool_call", sql="x", tool="run_sql", query_id="Q1", outcome="ok", latency_ms=1)
    assert "truncated" not in short
    big = t.write("llm_call", prompt=[{"role": "user", "content": "y" * 200}])
    assert isinstance(big["prompt"], str) and len(big["prompt"]) == 50 and big["truncated"]


def test_read_traces_skips_malformed_lines(tmp_path: Path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"event": "node"}\nnot json\n\n[1, 2]\n{"event": "session"}\n')
    assert [r["event"] for r in read_traces(p)] == ["node", "session"]
    assert list(read_traces(tmp_path / "missing.jsonl")) == []


def test_null_tracer_keeps_records_and_shares_them_with_children(tmp_path: Path):
    t = NullTracer()
    child = t.bind(thread_id="th")
    child.write("node", latency_ms=1)
    assert len(t.records) == 1 and t.records[0]["thread_id"] == "th"
    assert t.path is None and not list(tmp_path.iterdir())
