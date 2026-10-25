"""Offline tests for the Stage 8 scripts: eval loaders, metrics over a fixture, trace inspector, scoring."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from retail_agent.config import REPO_ROOT

SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_script(name: str):
    return importlib.import_module(name)


common = load_script("_common")
metrics = load_script("metrics")
inspect_trace = load_script("inspect_trace")
routing = load_script("run_routing_eval")
smoke = load_script("run_smoke")
golden = load_script("run_golden")


def rec(event: str, ts: str, turn: str = "t1-0001", thread: str = "t1", **fields) -> dict:
    return {"schema_version": 1, "ts": ts, "event": event, "thread_id": thread, "turn_id": turn,
            "exec_id": "ceo", **fields}


@pytest.fixture
def traces(tmp_path: Path) -> Path:
    rows = [
        rec("node", "2026-10-01T10:00:00.000Z", node="classify", latency_ms=5, intent="analysis"),
        rec("llm_call", "2026-10-01T10:00:01.000Z", node="classify", role="cheap", model="gemini-3.5-flash-lite",
            prompt="p" * 1000, response="r", tokens_in=1_000_000, tokens_out=100_000, latency_ms=10, attempt=1,
            retry_count=0, fallback_used=False, truncated=True),
        rec("tool_call", "2026-10-01T10:00:02.000Z", node="analysis", tool="run_sql", query_id="Q1", sql="SELECT 1",
            outcome="ok", rows=1, bytes_scanned=10, latency_ms=20),
        rec("turn_summary", "2026-10-01T10:00:03.000Z", outcome="answered", intent="analysis", latency_ms=1000,
            llm_calls=2, tool_calls=1, tokens_in=1000, tokens_out=100, fallback_used=False),
        rec("security_event", "2026-10-02T10:00:00.000Z", turn="t2-0001", thread="t2", kind="pii_column",
            reason_code=None, detail={}),
        rec("turn_summary", "2026-10-02T10:00:01.000Z", turn="t2-0001", thread="t2", outcome="error",
            intent="analysis", latency_ms=3000, llm_calls=1, tool_calls=0, tokens_in=10, tokens_out=0,
            fallback_used=True, error_type="quota_exhausted"),
        rec("turn_summary", "2026-10-03T10:00:00.000Z", turn="t3-0001", thread="t3", outcome="clarification",
            intent="clarify", latency_ms=500, llm_calls=1, tool_calls=0, tokens_in=10, tokens_out=0,
            fallback_used=False, deadline_hit=True, budget_exhausted=True),
        rec("feedback", "2026-10-03T10:00:05.000Z", turn="t3-0001", thread="t3", rating="good", comment=None),
    ]
    path = tmp_path / "traces.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    return path


# -- loaders ---------------------------------------------------------------------------------------

def test_eval_files_load():
    assert len(common.load_routing_set()) >= 30
    assert len(common.load_smoke_set()) >= 5
    golden_set = common.load_golden_set()
    assert {c["category"] for c in golden_set["cases"]} == {"business", "security"}
    assert set(golden_set["personas"]) == set(common.PERSONAS)


def test_loader_rejects_bad_files(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("cases:\n  - {id: A, message: x}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        common.load_routing_set(bad)
    dup = tmp_path / "dup.yaml"
    dup.write_text("cases:\n  - {id: A, message: x, intent: analysis}\n  - {id: A, message: y, intent: analysis}\n",
                   encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        common.load_routing_set(dup)


def test_placeholders_and_sql_escaping():
    assert common.substitute("{a} and {b} {keep}", {"a": "x", "b": "O'Neil"}) == "x and O'Neil {keep}"
    assert common.substitute("= '{b}'", {"b": "O'Neil\\"}, escape=True) == "= 'O\\'Neil\\\\'"
    assert common.used_placeholders("{own_brand} vs {other_brand}") == {"own_brand", "other_brand"}


def test_golden_placeholders_are_all_defined():
    data = common.load_golden_set()
    for case in data["cases"]:
        for name in case.get("placeholders", []):
            assert name in data["resolvers"]
        text = case["question"] + (case.get("reference_sql") or "")
        outputs = {o for n in case.get("placeholders", []) for o in data["resolvers"][n]["outputs"]}
        assert common.used_placeholders(text) <= outputs, case["id"]


def test_smoke_placeholders_have_resolvers():
    resolvers = common.load_golden_set()["resolvers"]
    known = {o for r in resolvers.values() for o in r["outputs"]}
    for d in common.load_smoke_set():
        for t in d["turns"]:
            assert common.used_placeholders(t.get("say", "")) <= known


def test_percentile_and_table():
    assert common.percentile([], 50) is None
    assert common.percentile([1, 2, 3, 4], 50) == 2
    assert common.percentile([1, 2, 3, 4], 95) == 4
    assert "a" in common.table(["a", "b"], [["x", 1]])


# -- metrics ---------------------------------------------------------------------------------------

def test_metrics_over_fixture(traces: Path):
    from retail_agent.tracing import read_traces
    prices = {"gemini-3.5-flash-lite": (0.30, 2.50)}
    m = metrics.compute_metrics(list(read_traces(traces)), prices)
    assert m["turns"] == 3
    assert m["error_rate"] == pytest.approx(1 / 3)
    assert m["turn_errors"] == {"quota_exhausted": 1}
    assert m["fallback_rate"] == pytest.approx(1 / 3)
    assert m["budget_rate"] == pytest.approx(1 / 3)
    assert m["deadline_rate"] == pytest.approx(1 / 3)
    assert m["clarification_rate"] == pytest.approx(1 / 3)
    assert m["security_events"] == {"pii_column": 1}
    assert m["latency"]["analysis"] == (2, 1000, 3000)
    assert m["llm_calls_per_turn"] == pytest.approx(4 / 3)
    assert m["feedback"] == {"good": 1}
    assert m["cost_usd"] == pytest.approx(0.30 + 0.25)


def test_metrics_window(traces: Path):
    from retail_agent.tracing import read_traces
    since, until = metrics.parse_when("2026-10-02"), metrics.parse_when("2026-10-03")
    recs = metrics.in_window(read_traces(traces), since, until)
    assert {r["thread_id"] for r in recs} == {"t2"}
    assert metrics.compute_metrics(recs)["turns"] == 1


def test_metrics_report_text_and_empty(traces: Path):
    from retail_agent.tracing import read_traces
    text = metrics.format_metrics(metrics.compute_metrics(list(read_traces(traces)), {}))
    for needle in ("error rate", "quota_exhausted", "pii_column", "p95", "no price for"):
        assert needle in text
    assert "no turn_summary" in metrics.format_metrics(metrics.compute_metrics([]))


def test_metrics_main_reports_missing_file(tmp_path: Path, capsys):
    assert metrics.main(["--traces", str(tmp_path / "nope.jsonl")]) == 1
    assert "not found" in capsys.readouterr().err


# -- inspect_trace ---------------------------------------------------------------------------------

def test_inspect_trace_chronology_and_truncation(traces: Path, capsys):
    assert inspect_trace.main(["t1", "--traces", str(traces)]) == 0
    out = capsys.readouterr().out
    assert out.index("node classify") < out.index("llm classify") < out.index("tool run_sql") < out.index("TURN SUMMARY")
    assert "TRUNCATED when written" in out
    assert "use --full" in out
    assert inspect_trace.main(["t1", "--traces", str(traces), "--full"]) == 0
    assert "p" * 1000 in capsys.readouterr().out


def test_inspect_trace_prefix_and_unknown(traces: Path, capsys):
    from retail_agent.tracing import read_traces
    records = list(read_traces(traces))
    assert inspect_trace.find_records(records, "t2")[1] == "t2"
    assert inspect_trace.find_records(records, "zzz") == ([], None)
    assert inspect_trace.main(["zzz", "--traces", str(traces)]) == 1
    assert "No (unique) thread" in capsys.readouterr().err


# -- routing scoring -------------------------------------------------------------------------------

def test_routing_score():
    cases = [
        {"id": "1", "message": "a", "intent": "analysis"},
        {"id": "2", "message": "b", "intent": "analysis", "borderline": True},
        {"id": "3", "message": "c", "intent": "malicious", "also_accept": ["off_topic"]},
        {"id": "4", "message": "d", "intent": "report_library", "library_action": "delete", "delete_scope": "entity"},
        {"id": "5", "message": "e", "intent": "schema_question"},
    ]
    results = [
        {"intent": "analysis"},
        {"intent": "unanswerable"},
        {"intent": "off_topic"},
        {"intent": "report_library", "library_action": "delete", "delete_scope": "this_conversation"},
        None,
    ]
    s = routing.score(cases, results)
    assert [m["id"] for m in s["misroutes"]] == ["2"]
    assert s["errors"] == ["5"]
    assert s["per_intent"]["analysis"]["recall"] == 0.5
    assert s["per_intent"]["analysis"]["precision"] == 1.0
    assert s["per_intent"]["malicious"]["recall"] == 1.0
    assert s["false_refusal_rate"] == pytest.approx(1 / 3)     # analysis, analysis, library: one refused
    assert s["library_action_accuracy"] == (1, 1)
    assert s["delete_scope_accuracy"] == (0, 1)
    assert "Misroutes" in routing.format_report(s, 5, "free")


# -- smoke assertions ------------------------------------------------------------------------------

def make_result(kind="analysis", text="", records=(), reports=(), notes=()):
    from types import SimpleNamespace
    response = SimpleNamespace(notes=list(notes))
    return common.TurnResult(kind=kind, text=text, response=response, reports=list(reports),
                             records=list(records), events=[])


def test_smoke_checks():
    records = [
        rec("llm_call", "2026-10-01T00:00:00.000Z", node="classify", role="cheap"),
        rec("tool_call", "2026-10-01T00:00:01.000Z", node="analysis", outcome="ok",
            sql_executed="WITH x AS (... brand IN UNNEST(@brands))"),
        rec("security_event", "2026-10-01T00:00:02.000Z", kind="out_of_scope_brand", reason_code=None, detail={}),
        rec("turn_summary", "2026-10-01T00:00:03.000Z", outcome="answered", intent="analysis", latency_ms=1),
        rec("deletion", "2026-10-01T00:00:04.000Z", detail={"report_ids": ["a"], "count": 1}),
    ]
    result = make_result(text="Revenue last month was high", records=records, reports=[1], notes=["Deletion cancelled."])
    expect = {"intent": "analysis", "response_kind": "analysis", "contains_any": ["last month"],
              "not_contains": ["@"], "security_event": "out_of_scope_brand", "no_llm_role": "expensive",
              "max_llm_calls": {"cheap": 1}, "llm_nodes_absent": ["rewrite"], "report_count": 1,
              "deleted_count": 1, "notes_contains": "cancelled", "results_brands_in_scope": True}
    checks = smoke.check_turn(expect, result, brand_scoped=True)
    assert all(ok for _, ok, _ in checks), [c for c in checks if not c[1]]

    bad = make_result(kind="error", text="mail a@b.com", records=records[:1])
    failed = {n for n, ok, _ in smoke.check_turn(
        {"response_kind": "analysis", "not_contains": ["@"], "security_event": "x", "max_llm_calls": {"cheap": 0}},
        bad, brand_scoped=True) if not ok}
    assert failed == {"response_kind", "not_contains", "security_event", "max_llm_calls.cheap"}

    unscoped = make_result(records=[rec("tool_call", "2026-10-01T00:00:00.000Z", outcome="ok", sql_executed="SELECT 1")])
    assert not smoke.check_turn({"results_brands_in_scope": True}, unscoped, brand_scoped=True)[0][1]
    assert smoke.check_turn({"results_brands_in_scope": True}, unscoped, brand_scoped=False) == []


def test_smoke_latency_table():
    d = {"turns": [{"intent": "analysis", "latency_ms": 100}, {"intent": "analysis", "latency_ms": 300},
                   {"intent": None, "latency_ms": None}]}
    assert "analysis" in smoke.latency_table([d])


# -- golden scoring --------------------------------------------------------------------------------

def test_golden_key_numbers():
    rows = [{"ranking": "revenue", "revenue": 100.0}, {"ranking": "revenue", "revenue": 50.0},
            {"ranking": "customers", "revenue": 7.0}]
    assert golden.pick_value(rows, {"where": {"ranking": "revenue"}, "row": 1, "column": "revenue"}) == 50.0
    assert golden.pick_value(rows, {"row": -1, "column": "revenue"}) == 7.0
    assert golden.pick_value(rows, {"aggregate": "sum", "column": "revenue"}) == 157.0
    specs = [{"name": "top", "where": {"ranking": "revenue"}, "row": 0, "column": "revenue", "rel_tol": 0.01},
             {"name": "missing", "row": 9, "column": "revenue"}]
    res = golden.check_key_numbers(specs, rows, "Top product made $101 in revenue")
    assert [k["ok"] for k in res] == [True, False]
    assert golden.numbers_in("$1,233,212.50 and 1.2M, 12.5%") == [1233212.5, 1200000.0, 12.5]
    assert golden.number_matches(1200000, [1.2e6], 0)
    assert not golden.number_matches(1200000, [1.3e6], 0.01)


def test_golden_security_checks(catalog):
    blocked = sorted(catalog.blocked_columns)[0]
    case = {"id": "S01", "expected": "answer_without_pii"}
    ok_result = make_result(text="Top customers by spend: user 1", records=[
        rec("tool_call", "2026-10-01T00:00:00.000Z", outcome="ok", sql="SELECT user_id FROM order_items", bytes_scanned=5)])
    kw = dict(catalog=catalog, bytes_cap=100, values={}, other_owner_report_ids=set(), own_scope_brands=set(),
              other_brands=set())
    assert all(ok for _, ok, _ in golden.check_security(case, ok_result, **kw))
    leak = make_result(text="mail x@y.com", records=[
        rec("tool_call", "2026-10-01T00:00:00.000Z", outcome="ok", sql=f"SELECT {blocked} FROM users", bytes_scanned=500)])
    failed = {n for n, ok, _ in golden.check_security(case, leak, **kw) if not ok}
    assert {"no blocked column in executed SQL", "no email address in the answer", "no query over the bytes cap"} <= failed


def test_golden_not_found_case(catalog):
    from types import SimpleNamespace
    case = {"id": "S09", "expected": "not_found"}
    kw = dict(catalog=catalog, bytes_cap=100, values={}, own_scope_brands=set(), other_brands=set())
    leaked = make_result(kind="library_list", reports=[SimpleNamespace(id="r1", title="x")])
    clean = make_result(kind="library_list", reports=[SimpleNamespace(id="r2", title="y")])
    assert not all(ok for _, ok, _ in golden.check_security(case, leaked, other_owner_report_ids={"r1"}, **kw))
    assert all(ok for _, ok, _ in golden.check_security(case, clean, other_owner_report_ids={"r1"}, **kw))


def test_throttle_llm_waits_when_window_full():
    from types import SimpleNamespace
    now, slept = [0.0], []
    llm = SimpleNamespace(call_llm=lambda *a, **k: "ok")
    common.throttle_llm(llm, 2, clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    for _ in range(3):
        assert llm.call_llm() == "ok"
    assert len(slept) == 1 and slept[0] == pytest.approx(60.1)
