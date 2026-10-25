"""Live analysis check: free profile, real BigQuery, one brand manager (Stage 4 acceptance)."""
from __future__ import annotations

import os
from datetime import date

import pytest

from retail_agent.analysis.output import render_markdown
from retail_agent.analysis.subgraph import run_analysis
from retail_agent.bq_client import create_runner
from retail_agent.config import load_catalog, load_settings, load_text
from retail_agent.llm.wrapper import Deadline, LLMClient, TurnBudget, TurnContext
from retail_agent.security.guards import QueryContext
from retail_agent.security.identity import IdentityStore
from retail_agent.tracing import NullTracer

pytestmark = [pytest.mark.llm, pytest.mark.bq]

QUESTIONS = ["Which countries generate the most revenue?", "Show monthly revenue for the last 12 months."]


@pytest.mark.parametrize("question", QUESTIONS)
def test_golden_questions_for_brand_manager(question):
    settings = load_settings(env={**os.environ, "LLM_PROFILE": "free"})
    if not settings.paths.app_db.exists():
        pytest.skip("run scripts/seed_executives.py first")
    executive = IdentityStore(settings.paths.app_db).get_executive("brand_manager_1")
    catalog = load_catalog(settings.paths.content_dir / "catalog.yaml")
    tracer = NullTracer()
    deadline = Deadline(settings.turn.deadline_for("analysis"))
    ctx = TurnContext(thread_id="t" * 32, turn_id="tttttttt-0001", exec_id=executive.exec_id, intent="analysis",
                      deadline=deadline, synthesis_reserve_s=settings.turn.synthesis_reserve_s,
                      budget=TurnBudget(tool_calls_limit=settings.turn.budget_for("analysis")), tracer=tracer)
    qctx = QueryContext(scope=executive.scope, exec_id=executive.exec_id, thread_id=ctx.thread_id,
                        turn_id=ctx.turn_id, deadline=deadline, settings=settings, catalog=catalog,
                        bq=create_runner(settings), tracer=tracer)
    result = run_analysis(
        question, mode="analysis", state={}, executive=executive, ctx=ctx, qctx=qctx, llm=LLMClient(settings),
        catalog=catalog, glossary=load_text(settings.paths.content_dir / "glossary.md"),
        content_dir=settings.paths.content_dir, today=date.today())
    print(f"\n{question}\n{render_markdown(result.output) if result.output else result.user_message}")
    print(f"llm_calls={ctx.budget.llm_calls} tool_calls={ctx.budget.tool_calls_used} stopped_by={result.stopped_by}")
    assert result.output is not None and result.output.outcome == "answer"
    ids = {f.id for f in result.output.findings}
    assert result.output.findings and all(a.finding_id in ids for a in result.output.action_items)
    assert all(q in {r.query_id for r in result.queries} for f in result.output.findings for q in f.query_ids)
