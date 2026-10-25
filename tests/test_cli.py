"""CLI: command parsing, the owner check and a piped session against fakes (Stage 7). Offline."""
from __future__ import annotations

import io

import pytest
from rich.console import Console

from retail_agent.cli.app import Command, Session, parse_command
from test_agent_graph import Env, analysis_steps, verdict


@pytest.mark.parametrize("line, expected", [
    ("hello", None),
    ("  how is revenue?", None),
    ("/login ceo", Command("login", ("ceo",))),
    ("/LOGIN  brand_manager_1 ", Command("login", ("brand_manager_1",))),
    ("/new", Command("new", ())),
    ("/resume abc123", Command("resume", ("abc123",))),
    ("/trace", Command("trace", ())),
    ("/feedback bad too slow", Command("feedback", ("bad", "too", "slow"))),
    ("/quit", Command("quit", ())),
    ("/reports", Command("help", ())),
    ("/", Command("help", ())),
])
def test_parse_command(line, expected):
    assert parse_command(line) == expected


def run_piped(env: Env, lines: list[str]) -> str:
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=100, soft_wrap=True)
    queue = iter(lines)
    session = Session(env.app, console, lambda prompt: next(queue, None), interactive=False)
    assert session.run() == 0
    return out.getvalue()


@pytest.fixture
def env(settings, catalog):
    return Env(settings, catalog)


def test_piped_session_login_question_foreign_resume_and_confirmed_delete(env):
    foreign = env.deps.threads.create("bm")
    env.script.classify += [verdict("analysis"), verdict("report_library", library_action="save"),
                            verdict("report_library", library_action="delete", delete_scope="this_conversation"),
                            verdict("report_library", library_action="list")]
    env.script.analysis += analysis_steps()
    text = run_piped(env, [
        "/login nobody", "/login ceo", "How many items per status?", f"/resume {foreign}", "/resume nope",
        "save this", "delete the reports from this conversation", "confirm", "list my reports",
        "/trace", "/feedback good nice", "/quit", "never reached"])
    assert "Unknown executive 'nobody'" in text
    assert "Logged in as CEO (ceo)" in text
    assert "... Querying data (1/6)" in text            # plain progress lines, no spinner
    assert "Three items completed." in text             # summary of the structured answer
    assert text.count("Thread not found.") == 2         # foreign and unknown look the same
    assert "permanently deleted. Type `confirm` to proceed." in text
    assert "Deleted 1 report." in text
    assert "You have no saved reports." in text
    assert "trace log:" in text and "feedback recorded" in text
    assert "never reached" not in text
    assert env.deps.reports.list("ceo") == []
    assert any(r["event"] == "feedback" for r in env.tracer.records)
    assert env.app.current_thread_id != foreign
    actions = [r["detail"]["action"] for r in env.tracer.records if r["event"] == "session"]
    assert actions == ["login", "quit"]


def test_unconfirmed_delete_is_cancelled_by_the_next_message(env):
    env.script.classify += [verdict("analysis"), verdict("report_library", library_action="save"),
                            verdict("report_library", library_action="delete", delete_scope="this_conversation"),
                            verdict("report_library", library_action="list")]
    env.script.analysis += analysis_steps()
    text = run_piped(env, ["/login ceo", "q", "save this", "delete these", "list my reports"])
    assert "Deletion cancelled." in text
    assert len(env.deps.reports.list("ceo")) == 1


def test_login_clears_a_pending_delete_and_questions_need_a_login(env):
    env.script.classify += [verdict("analysis"), verdict("report_library", library_action="save"),
                            verdict("report_library", library_action="delete", delete_scope="this_conversation")]
    env.script.analysis += analysis_steps()
    text = run_piped(env, ["hello?", "/login ceo", "q", "save this", "delete these", "/login bm"])
    assert "Please log in first: /login <exec_id>" in text
    assert len(env.deps.reports.list("ceo")) == 1
    assert env.app.current_exec.exec_id == "bm"


def test_errors_show_the_thread_id_and_the_loop_continues(env, monkeypatch):
    from retail_agent.llm.errors import QuotaExhaustedError
    real = env.deps.llm.call_llm
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise QuotaExhaustedError(["gemini-x"], "cheap")
        return real(*args, **kwargs)
    monkeypatch.setattr(env.deps.llm, "call_llm", flaky)
    env.script.classify.append(verdict("off_topic"))
    text = run_piped(env, ["/login ceo", "first", "second"])
    assert "daily quota" in text and "gemini-x" in text
    assert f"(thread {env.app.current_thread_id})" in text
    assert "I can only help with analysis" in text
