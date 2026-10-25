"""Saved reports, deletion and the confirmation interrupt (Stage 6). Offline, fake models."""
from __future__ import annotations

import json
import sqlite3

import pytest

from retail_agent.agent.state import ConfirmRequest, FinalResponse
from retail_agent.analysis.output import AnalysisOutput
from retail_agent.reports.deletion import confirmation_prompt, is_confirmation
from retail_agent.reports.search import ReportSelectionOutput
from retail_agent.reports.store import ReportStore, build_search_text
from retail_agent.tracing import validate_record

from test_agent_graph import Env, analysis_steps, verdict


def make_output(title: str, *, finding: str = "Sales were flat.", entities=("Levi's",)) -> AnalysisOutput:
    return AnalysisOutput(
        outcome="answer", definitions_used=[], findings=[{"id": "F1", "statement": finding, "query_ids": ["Q1"]}],
        limitations=[], action_items=[], summary="Summary text.", entities=list(entities),
        title=title, description=f"About {title}")


def selection(terms=(), ids=()) -> str:
    return ReportSelectionOutput(search_terms=list(terms), report_ids=list(ids)).model_dump_json()


@pytest.fixture
def store(tmp_path) -> ReportStore:
    return ReportStore(tmp_path / "reports.sqlite")


@pytest.fixture
def env(settings, catalog):
    e = Env(settings, catalog)
    e.app.login("ceo")
    return e


def delete_verdict(**fields) -> str:
    return verdict("report_library", library_action="delete", **fields)


def ask_delete(env, text="delete my Nike reports"):
    events = list(env.app.send(text))
    return events[-1]


# -- store -----------------------------------------------------------------------------------

def test_every_store_operation_is_owner_filtered(store):
    mine = store.save("ceo", "t1", make_output("Nike review"))
    assert store.list("bm") == [] and store.get("bm", mine.id) is None
    assert store.ids_by_thread("bm", "t1") == [] and store.substring_ids("bm", ["nike"]) == []
    assert store.catalog_for_selection("bm") == [] and store.filter_owned("bm", [mine.id]) == []
    assert store.delete("bm", [mine.id]) == 0
    assert store.get("ceo", mine.id).title == "Nike review"
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0   # nothing was deleted


def test_substring_search_finds_a_brand_that_is_only_in_a_finding(store):
    rec = store.save("ceo", "t1", make_output("Quarterly sales", finding="Adidas lost share to NIKE.",
                                              entities=()))
    assert "nike" in build_search_text(rec.content)
    assert store.substring_ids("ceo", ["Nike"]) == [rec.id]
    assert store.substring_ids("ceo", []) == [] and store.substring_ids("ceo", ["  "]) == []
    assert store.substring_ids("ceo", ["puma"]) == []


def test_list_is_newest_first_and_round_trips(store):
    a = store.save("ceo", "t1", make_output("First"))
    b = store.save("ceo", "t1", make_output("Second"))
    assert [r.id for r in store.list("ceo")] == [b.id, a.id]
    assert store.get("ceo", a.id).content == make_output("First")
    assert store.ids_by_thread("ceo", "t1") == [b.id, a.id]


def test_delete_is_hard_and_the_audit_log_holds_no_content(store):
    a = store.save("ceo", "t1", make_output("Secret title", finding="Secret finding text"))
    b = store.save("ceo", "t1", make_output("Other"))
    assert store.delete("ceo", [a.id, "nonexistent"]) == 1
    with sqlite3.connect(store.db_path) as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM reports")] == [b.id]
        rows = conn.execute("SELECT actor_id, action, report_ids, count FROM audit_log").fetchall()
        dump = json.dumps(conn.execute("SELECT * FROM audit_log").fetchall())
    assert rows == [("ceo", "delete", json.dumps([a.id]), 1)]
    assert "Secret" not in dump


def test_confirmation_helpers():
    assert is_confirmation("  Confirm ") and not is_confirmation("yes") and not is_confirmation("confirm it")
    prompt = confirmation_prompt({"report_ids": ["a", "b", "c"], "reports": [
        {"id": "a", "title": "A", "created_at": "2026-01-02T00:00:00Z"}]})
    assert prompt.startswith("3 reports will be permanently deleted. Type `confirm` to proceed.")
    assert "- A (2026-01-02)" in prompt


# -- save and list ---------------------------------------------------------------------------

def test_save_only_on_request_and_list(env):
    env.script.classify += [verdict("report_library", library_action="save"), verdict("analysis"),
                            verdict("report_library", library_action="save"),
                            verdict("report_library", library_action="list")]
    env.script.analysis += analysis_steps()
    nothing = env.final("save this")
    assert nothing.kind == "notice" and env.deps.reports.list("ceo") == []

    env.final("How many items per status?")
    assert env.deps.reports.list("ceo") == []   # answering alone saves nothing
    saved = env.final("save it")
    assert saved.kind == "saved" and saved.reports[0].title == "Completed items"
    listed = env.final("list my reports")
    assert listed.kind == "library_list" and [r.id for r in listed.reports] == [saved.reports[0].id]
    assert [s["outcome"] for s in env.summaries()] == ["library", "answered", "library", "library"]


def test_listing_shows_only_the_owners_reports(env):
    env.deps.reports.save("bm", "tx", make_output("Not yours"))
    env.script.classify.append(verdict("report_library", library_action="list"))
    assert env.final("list my reports").reports == []


# -- delete ----------------------------------------------------------------------------------

def test_delete_by_thread_needs_no_llm_and_confirm_deletes_the_snapshot(env):
    thread = env.app.current_thread_id
    mine = env.deps.reports.save("ceo", thread, make_output("From here"))
    env.deps.reports.save("ceo", "other-thread", make_output("Elsewhere"))
    env.script.classify += [delete_verdict(delete_scope="this_conversation"),
                            verdict("report_library", library_action="list")]
    last = ask_delete(env, "delete the reports from this conversation")
    assert isinstance(last, ConfirmRequest)
    assert [r.id for r in last.reports] == [mine.id]
    assert last.prompt.startswith("1 report will be permanently deleted. Type `confirm` to proceed.")
    assert env.script.count("classify") == 1   # no selection call
    assert env.summaries()[-1]["outcome"] == "confirm_pending"

    done = env.final("Confirm")
    assert (done.kind, done.text) == ("deleted", "Deleted 1 report.")
    assert [r.title for r in env.deps.reports.list("ceo")] == ["Elsewhere"]
    [deletion] = [r for r in env.tracer.records if r["event"] == "deletion"]
    assert deletion["detail"] == {"report_ids": [mine.id], "count": 1}
    assert env.summaries()[-1]["outcome"] == "deleted"
    assert all(validate_record(r) == [] for r in env.tracer.records)


def test_hallucinated_and_foreign_ids_never_reach_the_confirmation(env):
    mine = env.deps.reports.save("ceo", "t1", make_output("Nike review"))
    foreign = env.deps.reports.save("bm", "t2", make_output("Nike review of someone else"))
    env.script.classify += [delete_verdict(delete_query="Nike"),
                            selection(["nike"], [mine.id, foreign.id, "deadbeef"])]
    last = ask_delete(env)
    assert isinstance(last, ConfirmRequest) and [r.id for r in last.reports] == [mine.id]
    env.final("confirm")
    assert env.deps.reports.get("bm", foreign.id) is not None


def test_brand_only_inside_a_finding_is_still_found(env):
    hidden = env.deps.reports.save("ceo", "t1", make_output("Quarterly sales", finding="Nike grew 10%.",
                                                            entities=()))
    env.deps.reports.save("ceo", "t1", make_output("Returns", finding="Returns are flat.", entities=()))
    env.script.classify += [delete_verdict(delete_query="Nike"), selection(["nike"], [])]   # model missed it
    last = ask_delete(env)
    assert isinstance(last, ConfirmRequest) and [r.id for r in last.reports] == [hidden.id]


def test_no_match_means_no_confirmation_and_no_reports_means_no_llm_call(env):
    env.script.classify += [delete_verdict(delete_query="Nike")]
    empty = ask_delete(env)
    assert isinstance(empty, FinalResponse) and empty.response.kind == "nothing_to_delete"
    assert env.script.count("classify") == 1   # the selection call was skipped

    env.deps.reports.save("ceo", "t1", make_output("Levi's review"))
    env.script.classify += [delete_verdict(delete_query="Nike"), selection(["nike"], [])]
    none = ask_delete(env)
    assert isinstance(none, FinalResponse) and none.response.text == "No reports match."


def test_confirm_deletes_exactly_the_snapshot(env):
    first = env.deps.reports.save("ceo", "t1", make_output("Nike review"))
    env.script.classify += [delete_verdict(delete_query="Nike"), selection(["nike"], [first.id])]
    assert isinstance(ask_delete(env), ConfirmRequest)
    late = env.deps.reports.save("ceo", "t1", make_output("Nike later report"))   # saved after the search
    done = env.final("confirm")
    assert done.kind == "deleted"
    assert [r.id for r in env.deps.reports.list("ceo")] == [late.id]


def test_another_message_cancels_and_is_answered_as_a_new_turn(env):
    rec = env.deps.reports.save("ceo", "t1", make_output("Nike review"))
    env.script.classify += [delete_verdict(delete_query="Nike"), selection(["nike"], [rec.id]),
                            verdict("off_topic")]
    assert isinstance(ask_delete(env), ConfirmRequest)
    response = env.final("What is the weather?")
    assert response.kind == "refusal" and response.notes == ["Deletion cancelled."]
    assert env.deps.reports.get("ceo", rec.id) is not None
    summaries = env.summaries()
    assert [s["outcome"] for s in summaries] == ["confirm_pending", "refused"]
    assert summaries[0]["turn_id"] != summaries[1]["turn_id"]

    # the pending action is gone: "confirm" now is an ordinary message
    env.script.classify.append(verdict("off_topic"))
    assert env.final("confirm").kind == "refusal"
    assert env.deps.reports.get("ceo", rec.id) is not None


def test_a_correction_becomes_a_fresh_delete_request(env):
    a = env.deps.reports.save("ceo", "t1", make_output("Nike review"))
    b = env.deps.reports.save("ceo", "t1", make_output("Nike returns"))
    env.script.classify += [delete_verdict(delete_query="Nike"), selection(["nike"], [a.id, b.id]),
                            delete_verdict(delete_query="Nike returns"), selection(["nike returns"], [b.id])]
    assert isinstance(ask_delete(env), ConfirmRequest)
    last = ask_delete(env, "yes, but only the returns one")
    assert isinstance(last, ConfirmRequest) and [r.id for r in last.reports] == [b.id]
    env.final("confirm")
    assert [r.id for r in env.deps.reports.list("ceo")] == [a.id]


def test_leaving_the_thread_clears_the_pending_delete(env):
    rec = env.deps.reports.save("ceo", "t1", make_output("Nike review"))
    env.script.classify += [delete_verdict(delete_query="Nike"), selection(["nike"], [rec.id])]
    assert isinstance(ask_delete(env), ConfirmRequest)
    old_thread = env.app.current_thread_id
    env.app.new_thread()
    assert env.app._pending_interrupt(old_thread) is None
    assert env.app.graph.get_state({"configurable": {"thread_id": old_thread}}).values["pending_delete"] is None
    assert env.deps.reports.get("ceo", rec.id) is not None

    env.script.classify += [delete_verdict(delete_query="Nike"), selection(["nike"], [rec.id])]
    assert isinstance(ask_delete(env), ConfirmRequest)
    env.app.login("bm")   # login leaves the thread too
    assert env.app._pending_interrupt(env.app.deps.threads.list_for("ceo")[-1]["thread_id"]) is None
    assert env.deps.reports.get("ceo", rec.id) is not None


def test_a_failed_selection_call_leaves_the_thread_usable(env):
    env.deps.reports.save("ceo", "t1", make_output("Nike review"))
    env.script.classify += [delete_verdict(delete_query="Nike"), RuntimeError("boom"), verdict("off_topic")]
    events = list(env.app.send("delete my Nike reports"))
    assert events[-1].error_type == "internal"
    assert env.final("hello").kind == "refusal"
