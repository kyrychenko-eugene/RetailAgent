"""Graph nodes and the classify output model (contracts §4.1, §4.9, DD §4).

Each node is a function `(deps, state) -> state update`; `graph.py` binds `deps`. Per-turn
runtime objects (deadline, budget, tracer, progress writer) are rebuilt from state, so every
value that crosses a node boundary is checkpoint-serialisable.
"""
from __future__ import annotations

import dataclasses
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from pydantic import BaseModel

from retail_agent.agent.prompt_assembly import (
    build_system_prompt, history_block, load_prompt, load_user_preferences, render, scope_description,
)
from retail_agent.agent.state import (
    AgentState, AssistantResponse, ProgressEvent, RateLimitWait, UiEvent,
)
from retail_agent.analysis.output import AnalysisOutput, render_markdown
from retail_agent.analysis.subgraph import run_analysis
from retail_agent.llm.wrapper import Deadline, TurnBudget, TurnContext, message_text
from retail_agent.security.guards import QueryContext
from retail_agent.security.input_masking import mask_input
from retail_agent.tracing import Tracer

if TYPE_CHECKING:
    from retail_agent.agent.graph import Deps

Intent = Literal["analysis", "report_generation", "report_library", "schema_question",
                 "clarify", "unanswerable", "off_topic", "malicious"]

OFF_TOPIC_TEXT = "I can only help with analysis of our sales, products and customers data."
MALICIOUS_TEXT = "I can't help with that request."
LIBRARY_STUB_TEXT = "Saved reports are not available yet."
_NO_CLARIFY = "Do not choose `clarify` now; pick the most likely reading instead."
_REWRITE_MAX_CHARS = 2000
_NO_HISTORY_KINDS = {"error", "refusal"}


class MetricOverride(BaseModel):
    name: Literal["churn_window_days", "analysis_period", "comparison_period", "top_n",
                  "rfm_scores", "age_bands"]
    value: str


class ClassifyOutput(BaseModel):
    reason: str
    intent: Intent
    clarification_question: str | None = None
    library_action: Literal["save", "list", "delete"] | None = None
    delete_scope: Literal["this_conversation", "entity"] | None = None
    delete_query: str | None = None
    overrides: list[MetricOverride] = []


def validate_classification(out: ClassifyOutput, question: str, *, consecutive_clarifications: int,
                            max_clarifications: int) -> ClassifyOutput:
    """Deterministic post-validation of the classifier output (contracts §4.1)."""
    update: dict = {}
    if out.intent == "clarify" and (not (out.clarification_question or "").strip()
                                    or consecutive_clarifications >= max_clarifications):
        update["intent"] = "analysis"
        update["clarification_question"] = None
    elif out.intent == "report_library":
        action = out.library_action or "list"
        update["library_action"] = action
        if action == "delete":
            scope = out.delete_scope or "entity"
            update["delete_scope"] = scope
            if scope == "entity" and not (out.delete_query or "").strip():
                update["delete_query"] = question
    return out.model_copy(update=update) if update else out


# -- shared helpers ------------------------------------------------------------------------

def _tracer(deps: "Deps", state: AgentState, node: str) -> Tracer:
    return deps.tracer.bind(thread_id=state["thread_id"], turn_id=state.get("turn_id"),
                            exec_id=state["owner_exec_id"], persona_hash=state.get("persona_hash"),
                            node=node)


def _emit(event: UiEvent) -> None:
    writer = get_stream_writer()
    if isinstance(event, ProgressEvent):
        writer({"type": "progress", "step": event.step})
    elif isinstance(event, RateLimitWait):
        writer({"type": "rate_limit_wait", "seconds": event.seconds})


def _context(deps: "Deps", state: AgentState, tracer: Tracer, intent: str | None) -> TurnContext:
    return TurnContext(
        thread_id=state["thread_id"], turn_id=state.get("turn_id", ""), exec_id=state["owner_exec_id"],
        intent=intent, deadline=Deadline.at(state["deadline_at"]),
        synthesis_reserve_s=deps.settings.turn.synthesis_reserve_s,
        budget=TurnBudget(**state["budget"]), tracer=tracer, emit=_emit)


@contextmanager
def _span(tracer: Tracer, **extra) -> Iterator[dict]:
    """Writes one `node` record with the latency of the block, also when it raises."""
    started = time.monotonic()
    fields: dict = dict(extra)
    try:
        yield fields
    except Exception as exc:
        fields["error_type"] = getattr(exc, "error_type", "internal")
        raise
    finally:
        tracer.write("node", latency_ms=int((time.monotonic() - started) * 1000), **fields)


def _system_prompt(deps: "Deps", state: AgentState) -> str:
    executive = deps.identity.get_executive(state["owner_exec_id"])
    return build_system_prompt(
        safety=load_prompt("safety", deps.settings.paths.content_dir),
        preferences=load_user_preferences(executive.exec_id),
        persona=state["persona_text"], executive=executive, today=deps.today())


def _safety(deps: "Deps") -> SystemMessage:
    return SystemMessage(load_prompt("safety", deps.settings.paths.content_dir))


def _response(state: AgentState, kind: str, **fields) -> dict:
    return AssistantResponse(kind=kind, thread_id=state["thread_id"], **fields).model_dump()


# -- nodes ---------------------------------------------------------------------------------

def mask_input_node(deps: "Deps", state: AgentState) -> dict:
    """Defence in depth: the facade already masks before the input is checkpointed."""
    tracer = _tracer(deps, state, "mask_input")
    with _span(tracer):
        masked = mask_input(state["user_input"])
        if masked.counts:
            tracer.pii_in_input(masked.counts)
    return {"user_input": masked.text, "rewritten_question": masked.text}


def rewrite_node(deps: "Deps", state: AgentState) -> dict:
    tracer = _tracer(deps, state, "rewrite")
    ctx = _context(deps, state, tracer, None)
    ctx.emit(ProgressEvent("Understanding the question"))
    with _span(tracer):
        prompt = render(load_prompt("rewrite", deps.settings.paths.content_dir),
                        history=history_block(state.get("history", []), deps.settings.turn.history_turns),
                        message=state["user_input"])
        result = deps.llm.call_llm("cheap", [_safety(deps), HumanMessage(prompt)], ctx, node="rewrite")
        text = message_text(result.message).strip()
    rewritten = text if 0 < len(text) <= _REWRITE_MAX_CHARS else state["user_input"]
    return {"rewritten_question": rewritten, "budget": ctx.budget.as_dict()}


def classify_node(deps: "Deps", state: AgentState) -> dict:
    tracer = _tracer(deps, state, "classify")
    ctx = _context(deps, state, tracer, None)
    ctx.emit(ProgressEvent("Understanding the question"))
    turn = deps.settings.turn
    consecutive = state.get("consecutive_clarifications", 0)
    question = state["rewritten_question"]
    with _span(tracer) as span:
        prompt = render(load_prompt("classify", deps.settings.paths.content_dir), question=question,
                        compact_catalog=deps.catalog.compact_prompt(),
                        can_clarify="" if consecutive < turn.max_consecutive_clarifications else _NO_CLARIFY)
        result = deps.llm.call_llm("cheap", [_safety(deps), HumanMessage(prompt)], ctx,
                                   node="classify", schema=ClassifyOutput)
        out = validate_classification(result.parsed, question, consecutive_clarifications=consecutive,
                                      max_clarifications=turn.max_consecutive_clarifications)
        span.update(intent=out.intent, classify_reason=out.reason)

    overrides = dict(state.get("metric_overrides") or {})
    if out.intent != "malicious":
        overrides.update({o.name: o.value for o in out.overrides})
    budget = dataclasses.replace(ctx.budget, tool_calls_limit=turn.budget_for(out.intent))
    return {"classify": out.model_dump(), "metric_overrides": overrides, "budget": budget.as_dict(),
            "deadline_at": time.monotonic() + turn.deadline_for(out.intent)}


def route_by_intent(state: AgentState) -> str:
    return {"analysis": "analysis", "report_generation": "report_generation",
            "report_library": "report_library", "schema_question": "schema_answer",
            "clarify": "clarify", "unanswerable": "unanswerable",
            "off_topic": "refuse", "malicious": "refuse"}[state["classify"]["intent"]]


def _analysis(deps: "Deps", state: AgentState, mode: Literal["analysis", "report_generation"]) -> dict:
    tracer = _tracer(deps, state, mode)
    ctx = _context(deps, state, tracer, mode)
    executive = deps.identity.get_executive(state["owner_exec_id"])
    if deps.bq is None:
        raise RuntimeError("BigQuery runner is not configured")
    qctx = QueryContext(
        scope=executive.scope, exec_id=executive.exec_id, thread_id=state["thread_id"],
        turn_id=state.get("turn_id", ""), deadline=ctx.deadline, settings=deps.settings,
        catalog=deps.catalog, bq=deps.bq, tracer=tracer)
    with _span(tracer) as span:
        result = run_analysis(
            state["rewritten_question"], mode=mode, state=state, executive=executive, ctx=ctx, qctx=qctx,
            llm=deps.llm, catalog=deps.catalog, glossary=deps.glossary,
            content_dir=deps.settings.paths.content_dir, today=deps.today())
        span.update(tool_calls=ctx.budget.tool_calls_used)

    update: dict = {"budget": ctx.budget.as_dict(), "stopped_by": result.stopped_by}
    output = result.output
    if output is None:
        message = result.user_message or "I could not answer that."
        kind, outcome = {
            "out_of_scope_brand": ("no_access", "no_access"),
            "pii_repeated": ("refusal", "refused"),
        }.get(result.stopped_by or "", ("error", "error"))
        update.update(response=_response(state, kind, text=message), outcome=outcome)
    elif output.outcome == "clarification":
        update.update(response=_response(state, "clarification", text=output.clarification_question),
                      outcome="clarification")
    else:
        answered = output.outcome == "answer"
        kind = "report" if answered and mode == "report_generation" else "analysis"
        update.update(
            response=_response(state, kind, analysis=output),
            outcome=("partial" if result.stopped_by in ("budget", "deadline") else "answered")
            if answered else "cannot_answer")
        if answered:
            update["last_answer"] = output.model_dump()
    return update


def analysis_node(deps: "Deps", state: AgentState) -> dict:
    return _analysis(deps, state, "analysis")


def report_generation_node(deps: "Deps", state: AgentState) -> dict:
    return _analysis(deps, state, "report_generation")


def schema_answer_node(deps: "Deps", state: AgentState) -> dict:
    tracer = _tracer(deps, state, "schema_answer")
    ctx = _context(deps, state, tracer, "schema_question")
    ctx.emit(ProgressEvent("Reading the data catalog"))
    executive = deps.identity.get_executive(state["owner_exec_id"])
    with _span(tracer):
        prompt = render(
            load_prompt("schema_answer", deps.settings.paths.content_dir),
            system=_system_prompt(deps, state), catalog=deps.catalog.schema_prompt(),
            derived_metrics="\n".join(f"- {m}" for m in deps.catalog.derived_metrics),
            scope=scope_description(executive.scope), question=state["rewritten_question"])
        result = deps.llm.call_llm("cheap", [HumanMessage(prompt)], ctx, node="schema_answer")
    text = message_text(result.message).strip()
    return {"response": _response(state, "schema_answer", text=text), "outcome": "schema_answer",
            "budget": ctx.budget.as_dict()}


def clarify_node(deps: "Deps", state: AgentState) -> dict:
    tracer = _tracer(deps, state, "clarify")
    with _span(tracer):
        question = state["classify"]["clarification_question"]
    return {"response": _response(state, "clarification", text=question), "outcome": "clarification"}


def unanswerable_node(deps: "Deps", state: AgentState) -> dict:
    tracer = _tracer(deps, state, "unanswerable")
    with _span(tracer):
        text = "That information isn't in our data. Available:\n" + deps.catalog.compact_prompt()
    return {"response": _response(state, "unanswerable", text=text), "outcome": "cannot_answer"}


def refuse_node(deps: "Deps", state: AgentState) -> dict:
    tracer = _tracer(deps, state, "refuse")
    intent = state["classify"]["intent"]
    with _span(tracer):
        if intent == "malicious":
            tracer.security_event("malicious_intent", None, reason=state["classify"]["reason"])
    text = MALICIOUS_TEXT if intent == "malicious" else OFF_TOPIC_TEXT
    return {"response": _response(state, "refusal", text=text), "outcome": "refused"}


def report_library_node(deps: "Deps", state: AgentState) -> dict:
    """Hand-off point for Stage 6 (save / list / delete). Stub until then."""
    tracer = _tracer(deps, state, "report_library")
    with _span(tracer):
        pass
    return {"response": _response(state, "notice", text=LIBRARY_STUB_TEXT), "outcome": "library"}


def finalize_node(deps: "Deps", state: AgentState) -> dict:
    """Commits the turn to the conversation: history, clarification counter, thread title."""
    tracer = _tracer(deps, state, "finalize")
    response = state["response"]
    with _span(tracer):
        kind = response["kind"]
        if response.get("analysis"):
            answer = render_markdown(AnalysisOutput(**response["analysis"]))
        else:
            answer = response.get("text") or ""
        history = list(state.get("history", []))
        if kind not in _NO_HISTORY_KINDS and answer:
            history.append({"question": state["rewritten_question"], "answer": answer})
        if state.get("turn_no", 0) == 1:
            deps.threads.set_title(state["thread_id"], state["user_input"])
    return {"history": history,
            "consecutive_clarifications": state.get("consecutive_clarifications", 0) + 1
            if kind == "clarification" else 0}
