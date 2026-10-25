"""Analysis subgraph offline tests: scripted fake model, fake BigQuery runner."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from retail_agent.agent.prompt_assembly import history_block, load_prompt, render
from retail_agent.analysis.output import (
    ActionItem, AnalysisOutput, Finding, check_output, render_markdown,
)
from retail_agent.analysis.subgraph import build_prompt, run_analysis
from retail_agent.analysis.tools import format_outcome_for_model, make_tools
from retail_agent.bq_client import DryRunResult, QueryResult
from retail_agent.config import load_text
from retail_agent.llm.wrapper import Deadline, LLMClient, TurnBudget, TurnContext
from retail_agent.security.guards import QueryContext
from retail_agent.security.identity import Executive, Scope
from retail_agent.tracing import NullTracer

BM = Executive("bm", "Brand Manager", Scope.of(["Calvin Klein", "Levi's"]))
SQL = "SELECT status, COUNT(*) AS n FROM order_items GROUP BY status"
SQL2 = "SELECT status, SUM(sale_price) AS revenue FROM order_items GROUP BY status"
SQL3 = "SELECT COUNT(*) AS n FROM orders"


class Clock:
    now = 0.0


@dataclass
class FakeRunner:
    rows: list[dict] = field(default_factory=lambda: [{"status": "Complete", "n": 3}])
    dry_bytes: int = 10_000_000
    clock: Clock | None = None
    advance: float = 0.0
    run_calls: list = field(default_factory=list)

    def dry_run(self, sql, params=None):
        return DryRunResult(ok=True, bytes_processed=self.dry_bytes, error=None)

    def run_query(self, sql, *, params=None, max_bytes_billed, timeout_s, labels=None):
        self.run_calls.append(sql)
        if self.clock:
            self.clock.now += self.advance
        columns = tuple(self.rows[0]) if self.rows else ()
        return QueryResult(columns=columns, rows=list(self.rows), bytes_processed=self.dry_bytes,
                           bytes_billed=self.dry_bytes, job_id="j", elapsed_ms=1)


class Fake(BaseChatModel):
    script: Any = None
    seen: Any = None

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        self.seen.append({"tools": [getattr(t, "name", None) for t in tools], **kwargs})
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append({"messages": list(messages)})
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


def call(name: str, args: dict, id_: str) -> dict:
    return {"name": name, "args": args, "id": id_, "type": "tool_call"}


def step(*calls: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls))


def sql_call(sql: str, id_: str) -> dict:
    return call("run_sql", {"sql": sql}, id_)


def answer_args(**over) -> dict:
    base = dict(outcome="answer", definitions_used=["Counted items exclude cancelled and returned"],
                findings=[{"id": "F1", "statement": "3 items completed.", "query_ids": ["Q1"]}],
                limitations=[], action_items=[], summary="Three items completed.", entities=[],
                title="Completed items", description="Count of completed items.")
    base.update(over)
    return base


def final(id_="f", **over) -> AIMessage:
    return step(call("final_answer", answer_args(**over), id_))


class Env:
    def __init__(self, settings, catalog, script, *, runner=None, scope=BM.scope, seconds=60.0, budget=6):
        self.clock = Clock()
        self.runner = runner or FakeRunner(clock=self.clock)
        self.runner.clock = self.runner.clock or self.clock
        self.seen: list = []
        self.script = list(script)
        self.tracer = NullTracer()
        deadline = Deadline(seconds, clock=lambda: self.clock.now)
        self.ctx = TurnContext(thread_id="t" * 32, turn_id="tttttttt-0001", exec_id="bm", intent="analysis",
                               deadline=deadline, synthesis_reserve_s=settings.turn.synthesis_reserve_s,
                               budget=TurnBudget(tool_calls_limit=budget), tracer=self.tracer)
        self.qctx = QueryContext(scope=scope, exec_id="bm", thread_id="t" * 32, turn_id="tttttttt-0001",
                                 deadline=deadline, settings=settings, catalog=catalog, bq=self.runner,
                                 tracer=self.tracer)
        self.llm = LLMClient(settings, factory=lambda spec, t: Fake(script=self.script, seen=self.seen))
        self.settings, self.catalog = settings, catalog

    def run(self, mode="analysis", question="How many items per status?", state=None):
        return run_analysis(
            question, mode=mode, state=state or {}, executive=BM, ctx=self.ctx, qctx=self.qctx, llm=self.llm,
            catalog=self.catalog, glossary=load_text(self.settings.paths.content_dir / "glossary.md"),
            content_dir=self.settings.paths.content_dir, today=date(2026, 10, 25))

    def tool_messages(self) -> list[str]:
        last = [s["messages"] for s in self.seen if "messages" in s][-1]
        return [m.content for m in last if m.type == "tool"]


def test_normal_finish_needs_no_extra_call(settings, catalog):
    env = Env(settings, catalog, [step(sql_call(SQL, "a")), final()])
    result = env.run()
    assert result.stopped_by is None and result.user_message is None
    assert env.ctx.budget.llm_calls == 2 and env.ctx.budget.tool_calls_used == 1
    assert result.output.outcome == "answer" and result.output.findings[0].query_ids == ["Q1"]
    assert [q.outcome for q in result.queries] == ["ok"]
    assert not any("tool_choice" in s for s in env.seen)


def test_budget_exhaustion_gives_answer_with_gaps(settings, catalog):
    env = Env(settings, catalog, [
        step(sql_call(SQL, "a"), sql_call(SQL2, "b"), sql_call(SQL3, "c")),   # 3 calls, budget 2
        final("f", findings=[{"id": "F1", "statement": "x", "query_ids": ["Q1"]}]),
    ], budget=2)
    result = env.run()
    assert len(env.runner.run_calls) == 2 and env.ctx.budget.tool_calls_used == 2
    assert result.stopped_by == "budget"
    assert any("budget" in x for x in result.output.limitations)
    forced = [s for s in env.seen if s.get("tool_choice")]
    assert forced and forced[0]["tool_choice"] == "final_answer" and forced[0]["tools"] == ["final_answer"]
    assert any("Not run" in m for m in env.tool_messages())
    assert env.ctx.budget.llm_calls == 2   # one loop step + one forced call


def test_budget_used_by_last_call_goes_straight_to_synthesis(settings, catalog):
    env = Env(settings, catalog, [step(sql_call(SQL, "a")), final()], budget=1)
    result = env.run()
    assert result.stopped_by == "budget" and env.ctx.budget.llm_calls == 2


def test_repeated_query_is_not_executed_twice(settings, catalog):
    env = Env(settings, catalog, [
        step(sql_call(SQL, "a")),
        step(sql_call("select status, count(*) as n from order_items group by status", "b")),
        final(),
    ])
    result = env.run()
    assert len(env.runner.run_calls) == 1
    assert env.ctx.budget.tool_calls_used == 2           # the repeat still counts
    assert "already run" in env.tool_messages()[1]
    assert [q.query_id for q in result.queries] == ["Q1", "Q2"]


def test_deadline_gives_partial_answer(settings, catalog):
    runner = FakeRunner(advance=45.0)
    env = Env(settings, catalog, [step(sql_call(SQL, "a")), final()], runner=runner)
    result = env.run()
    assert result.stopped_by == "deadline"
    assert env.ctx.budget.llm_calls == 2 and len(runner.run_calls) == 1
    assert any("time limit" in x for x in result.output.limitations)
    assert result.output.outcome == "answer"


def test_validator_rejection_is_fed_back(settings, catalog):
    env = Env(settings, catalog, [
        step(sql_call("SELECT email FROM users", "a")), step(sql_call(SQL, "b")),
        final(findings=[{"id": "F1", "statement": "s", "query_ids": ["Q2"]}])])
    result = env.run()
    assert "Column `email` is not available" in env.tool_messages()[0]
    assert [q.outcome for q in result.queries] == ["validation", "ok"]
    assert len(env.runner.run_calls) == 1 and result.output is not None


def test_out_of_scope_brand_ends_turn_after_one_expensive_call(settings, catalog):
    sql = ("SELECT SUM(oi.sale_price) AS revenue FROM order_items oi JOIN products p ON p.id = oi.product_id "
           "WHERE p.brand = 'Nike'")
    env = Env(settings, catalog, [step(sql_call(sql, "a"))])
    result = env.run()
    assert result.output is None and result.stopped_by == "out_of_scope_brand"
    assert result.user_message == "You don't have access to brand Nike."
    assert env.ctx.budget.llm_calls == 1 and env.runner.run_calls == []


def test_bytes_cap_ends_turn_with_honest_message(settings, catalog):
    runner = FakeRunner(dry_bytes=settings.bigquery.bytes_cap + 1)
    env = Env(settings, catalog, [step(sql_call(SQL, "a"))], runner=runner)
    result = env.run()
    assert result.output is None and result.stopped_by == "bytes_cap"
    assert "too large" in result.user_message and env.ctx.budget.llm_calls == 1


def test_repeated_pii_attempts_end_turn(settings, catalog):
    env = Env(settings, catalog, [step(sql_call("SELECT email FROM users", "a")),
                                  step(sql_call("SELECT first_name FROM users", "b"))])
    result = env.run()
    assert result.stopped_by == "pii_repeated" and result.user_message
    assert env.ctx.budget.llm_calls == 2


def test_empty_result_hint_reaches_model(settings, catalog):
    runner = FakeRunner(rows=[])
    env = Env(settings, catalog, [step(sql_call(SQL, "a")), final(findings=[], outcome="cannot_answer")],
              runner=runner)
    result = env.run()
    assert "no rows" in env.tool_messages()[0] and result.queries[0].outcome == "empty"


def test_column_values_tool_counts_budget(settings, catalog):
    gcv = call("get_column_values", {"column": "products.category"}, "a")
    env = Env(settings, catalog, [step(gcv), final()])
    env.runner.rows = [{"value": "Jeans", "n": 4}]
    env.run()
    assert env.ctx.budget.tool_calls_used == 1 and "Jeans,4" in env.tool_messages()[0]


def test_invalid_final_answer_is_fed_back_once(settings, catalog):
    bad = step(call("final_answer", {"outcome": "answer"}, "f1"))
    env = Env(settings, catalog, [step(sql_call(SQL, "a")), bad, final("f2")])
    result = env.run()
    assert "invalid" in env.tool_messages()[1] and result.output.outcome == "answer"


def test_unusable_synthesis_falls_back_to_valid_output(settings, catalog):
    env = Env(settings, catalog, [step(sql_call(SQL, "a")), AIMessage(content="no tool")], budget=1)
    result = env.run()
    assert result.output.outcome == "cannot_answer" and result.output.limitations


def test_output_always_references_findings(settings, catalog):
    bad = final(findings=[{"id": "F1", "statement": "s", "query_ids": ["Q1", "Q9"]}],
                action_items=[{"action": "Do A", "finding_id": "F1"}, {"action": "Do B", "finding_id": "F7"}])
    env = Env(settings, catalog, [step(sql_call(SQL, "a")), bad, bad])
    out = env.run().output
    ids = {f.id for f in out.findings}
    assert all(a.finding_id in ids for a in out.action_items) and len(out.action_items) == 1
    assert out.findings[0].query_ids == ["Q1"]
    assert any("Q9" not in x and "removed" in x for x in out.limitations)


def test_report_requires_action_item_and_retries_once(settings, catalog):
    no_actions = final("f1")
    with_actions = final("f2", action_items=[{"action": "Restock Jeans", "finding_id": "F1"}])
    env = Env(settings, catalog, [step(sql_call(SQL, "a")), no_actions, with_actions], budget=10)
    result = env.run(mode="report_generation")
    assert result.output.action_items and "action item" in env.tool_messages()[1]
    assert env.ctx.budget.llm_calls == 3


def test_report_without_action_items_after_retry_notes_limitation(settings, catalog):
    env = Env(settings, catalog, [step(sql_call(SQL, "a")), final("f1"), final("f2")], budget=10)
    out = env.run(mode="report_generation").output
    assert out.action_items == [] and any("action items" in x for x in out.limitations)


def test_pii_in_final_answer_is_blocked(settings, catalog):
    env = Env(settings, catalog, [step(sql_call(SQL, "a")), final(summary="Mail jane.doe@example.com now")])
    result = env.run()
    assert result.output.outcome == "cannot_answer" and "example.com" not in render_markdown(result.output)
    assert [r["kind"] for r in env.tracer.records if r["event"] == "security_event"] == ["pii_in_answer"]


def test_prompt_contents(settings, catalog):
    env = Env(settings, catalog, [])
    state = {"metric_overrides": {"churn_window_days": "60"},
             "history": [{"question": "q1", "answer": "a1"}]}
    for mode, marker in (("analysis", "# Question"), ("report_generation", "# Request")):
        prompt = build_prompt("What {{sold}}?", mode=mode, state=state, executive=BM, ctx=env.ctx,
                              qctx=env.qctx, catalog=catalog, glossary="GLOSSARY",
                              content_dir=settings.paths.content_dir, today=date(2026, 10, 25))
        assert "2026-10-25" in prompt and "brands: Calvin Klein, Levi's" in prompt
        assert "churn_window_days: 60" in prompt and "User: q1" in prompt and "GLOSSARY" in prompt
        assert "What {{sold}}?" in prompt and marker in prompt
        assert "at most 6 tool calls" in prompt
        for blocked in catalog.blocked_columns:
            assert f"`{blocked}`" not in prompt


def test_render_and_history_helpers(settings):
    assert render("a {{x}} b", x="{{y}}") == "a {{y}} b"
    with pytest.raises(KeyError):
        render("{{x}} {{z}}", x="1")
    assert history_block([], 6) == "(none)"
    assert "q2" in history_block([{"question": "q1", "answer": "a"}, {"question": "q2", "answer": "b"}], 1)
    assert not load_prompt("analysis", settings.paths.content_dir).startswith("<!--")


def test_format_outcome_and_tools(settings, catalog):
    env = Env(settings, catalog, [])
    names = [t.name for t in make_tools(env.qctx)]
    assert names == ["run_sql", "get_column_values", "final_answer"]
    run_sql = make_tools(env.qctx)[0]
    text = run_sql.invoke({"sql": SQL})
    assert text.startswith("Q1: 1 row(s)") and "status,n" in text and "Complete,3" in text
    assert "bigquery-public-data" not in text and "WITH" not in text
    assert "failed (validation)" in run_sql.invoke({"sql": "DELETE FROM orders"})


def test_check_output_fixes_and_markdown():
    out = AnalysisOutput(
        outcome="clarification", definitions_used=["d"], findings=[Finding(id="F1", statement="s", query_ids=["Q2"])],
        limitations=[], action_items=[ActionItem(action="a", finding_id="F2")], summary="sum", entities=[],
        title="t" * 120, description="d" * 300)
    fixed, problems = check_output(out, executed_query_ids={"Q1"}, report=False)
    assert fixed.outcome == "cannot_answer" and fixed.action_items == [] and len(fixed.title) <= 80
    assert len(fixed.description) <= 200 and problems and fixed.limitations
    md = render_markdown(fixed)
    assert md.startswith("sum") and "Limitations" in md and "Definitions used" in md
