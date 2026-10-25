"""Conversation graph and AgentApp facade (contracts §4.9, §4.10, DD §4, §5)."""
from __future__ import annotations

import functools
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterator, Literal

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from retail_agent.agent import nodes
from retail_agent.agent.prompt_assembly import load_persona
from retail_agent.agent.state import (
    AgentState, AssistantResponse, ErrorEvent, FinalResponse, ProgressEvent, RateLimitWait, UiEvent,
)
from retail_agent.agent.threads import ThreadRegistry
from retail_agent.bq_client import BigQueryError, BigQueryRunner, BigQueryUnavailableError
from retail_agent.config import Catalog, Settings
from retail_agent.llm.errors import QuotaExhaustedError
from retail_agent.llm.wrapper import LLMClient, TurnBudget
from retail_agent.reports.store import ReportStore
from retail_agent.security.identity import Executive, IdentityStore
from retail_agent.security.input_masking import mask_input
from retail_agent.tracing import Tracer

log = logging.getLogger(__name__)

QUOTA_MESSAGE = ("The daily quota of your Gemini API key is exhausted for {model}. Wait for the quota reset "
                 "(midnight Pacific time), switch to `LLM_PROFILE=free`, or enable billing. (thread {thread_id})")
BQ_MESSAGE = ("The data source is unavailable right now. Please try again in a few minutes. "
              "(thread {thread_id})")
GENERIC_MESSAGE = ("Something went wrong while answering. The conversation is intact; you can rephrase "
                   "or try again. (thread {thread_id})")
NO_LOGIN_MESSAGE = "Please log in first."


@dataclass
class Deps:
    settings: Settings
    catalog: Catalog
    glossary: str
    tracer: Tracer
    identity: IdentityStore
    threads: ThreadRegistry
    reports: ReportStore
    bq: BigQueryRunner | None
    llm: LLMClient
    today: Callable[[], date] = date.today



def make_checkpointer(settings: Settings) -> SqliteSaver:
    path = settings.paths.checkpoint_db
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def build_graph(deps: Deps, checkpointer):
    g = StateGraph(AgentState)
    for name, fn in [
        ("mask_input", nodes.mask_input_node), ("rewrite", nodes.rewrite_node),
        ("classify", nodes.classify_node), ("analysis", nodes.analysis_node),
        ("report_generation", nodes.report_generation_node), ("schema_answer", nodes.schema_answer_node),
        ("clarify", nodes.clarify_node), ("unanswerable", nodes.unanswerable_node),
        ("refuse", nodes.refuse_node), ("report_library", nodes.report_library_node),
        ("finalize", nodes.finalize_node),
    ]:
        g.add_node(name, functools.partial(fn, deps))
    g.add_edge(START, "mask_input")
    g.add_conditional_edges("mask_input", lambda s: "rewrite" if s.get("history") else "classify",
                            ["rewrite", "classify"])
    g.add_edge("rewrite", "classify")
    branches = ["analysis", "report_generation", "report_library", "schema_answer", "clarify",
                "unanswerable", "refuse"]
    g.add_conditional_edges("classify", nodes.route_by_intent, branches)
    for name in branches:
        g.add_edge(name, "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def error_for(exc: BaseException, thread_id: str) -> tuple[str, str]:
    """(user message, trace error_type) for an exception escaping a turn (contracts §6.2)."""
    if isinstance(exc, QuotaExhaustedError):
        return QUOTA_MESSAGE.format(model=exc.primary_model, thread_id=thread_id), exc.error_type
    if isinstance(exc, BigQueryUnavailableError):
        return BQ_MESSAGE.format(thread_id=thread_id), "bq_unavailable"
    error_type = getattr(exc, "error_type", None)
    if error_type is None:
        error_type = "bq_unavailable" if isinstance(exc, BigQueryError) else "internal"
    return GENERIC_MESSAGE.format(thread_id=thread_id), error_type


_STOP_ERROR_TYPE = {"bytes_cap": "bytes_cap", "unavailable": "bq_unavailable"}


class AgentApp:
    """The only object the CLI talks to."""
    current_exec: Executive | None
    current_thread_id: str | None

    def __init__(self, deps: Deps, checkpointer) -> None:
        self.deps = deps
        self.graph = build_graph(deps, checkpointer)
        self.current_exec = None
        self.current_thread_id = None

    # -- session control ---------------------------------------------------------------

    def login(self, exec_id: str) -> Executive:
        executive = self.deps.identity.get_executive(exec_id)   # raises UnknownExecutiveError
        self.current_exec = executive
        self.current_thread_id = self.deps.threads.create(exec_id)
        return executive

    def new_thread(self) -> str:
        if self.current_exec is None:
            raise RuntimeError(NO_LOGIN_MESSAGE)
        self.current_thread_id = self.deps.threads.create(self.current_exec.exec_id)
        return self.current_thread_id

    def resume(self, thread_id: str) -> bool:
        if self.current_exec is None:
            return False
        exec_id = self.current_exec.exec_id
        if not self.deps.threads.can_open(thread_id, exec_id):
            if self.deps.threads.owner_of(thread_id) is not None:   # foreign, not unknown
                self.deps.tracer.bind(exec_id=exec_id).security_event(
                    "thread_access_denied", None, thread_id=thread_id)
            return False
        self.current_thread_id = thread_id
        return True

    def feedback(self, rating: Literal["good", "bad"], comment: str | None) -> None:
        self.deps.tracer.bind(thread_id=self.current_thread_id,
                              exec_id=self.current_exec.exec_id if self.current_exec else None
                              ).feedback(rating, comment)

    def trace_info(self) -> tuple[str | None, Path]:
        return self.current_thread_id, self.deps.settings.paths.traces

    # -- one turn ----------------------------------------------------------------------

    def send(self, text: str) -> Iterator[UiEvent]:
        """One user turn (contracts §4.10). Ends with exactly one FinalResponse or ErrorEvent."""
        if self.current_exec is None:
            yield ErrorEvent(NO_LOGIN_MESSAGE, "internal")
            return
        executive = self.current_exec
        thread_id = self.current_thread_id or self.new_thread()
        config = {"configurable": {"thread_id": thread_id}}
        started = time.monotonic()
        turn = self.deps.settings.turn

        values = self.graph.get_state(config).values
        seed: dict = {}
        if not values:   # first turn of the thread: pin the persona for the whole conversation
            persona_text, persona_hash = load_persona(self.deps.settings.paths.content_dir)
            seed = {"thread_id": thread_id, "owner_exec_id": executive.exec_id, "persona_text": persona_text,
                    "persona_hash": persona_hash, "history": [], "metric_overrides": {}, "last_answer": None,
                    "pending_delete": None, "consecutive_clarifications": 0}
            values = seed
        turn_no = values.get("turn_no", 0) + 1
        turn_id = f"{thread_id[:8]}-{turn_no:04d}"
        tracer = self.deps.tracer.bind(thread_id=thread_id, turn_id=turn_id, exec_id=executive.exec_id,
                                       persona_hash=values.get("persona_hash"))

        masked = mask_input(text)   # before the checkpointer sees the input
        if masked.counts:
            tracer.pii_in_input(masked.counts)
        turn_input = {
            **seed, "turn_no": turn_no, "turn_id": turn_id, "user_input": masked.text,
            "rewritten_question": masked.text, "classify": None, "notes": [], "response": None,
            "outcome": None, "stopped_by": None, "budget": TurnBudget(0).as_dict(),
            "deadline_at": time.monotonic() + turn.deadline_for("default"),
        }

        error: tuple[str, str] | None = None
        try:
            for mode, chunk in self.graph.stream(turn_input, config, stream_mode=["custom", "updates"]):
                if mode == "custom":
                    event = _ui_event(chunk)
                    if event is not None:
                        yield event
        except Exception as exc:  # the CLI must never crash; the thread stays usable
            log.exception("Turn failed in thread %s", thread_id)
            error = error_for(exc, thread_id)

        state = self.graph.get_state(config).values
        if error is not None:
            message, error_type = error
            # Close the interrupted run as if `finalize` had completed, so the next turn starts clean.
            self.graph.update_state(config, {
                "response": AssistantResponse(kind="error", text=message, thread_id=thread_id).model_dump(),
                "outcome": "error"}, as_node="finalize")
            state = self.graph.get_state(config).values
        else:
            error_type = _STOP_ERROR_TYPE.get(state.get("stopped_by") or "")

        budget = state.get("budget") or {}
        classify = state.get("classify") or {}
        outcome = "error" if error else (state.get("outcome") or "error")
        tracer.turn_summary(
            outcome=outcome, intent=classify.get("intent"), classify_reason=classify.get("reason"),
            latency_ms=int((time.monotonic() - started) * 1000), llm_calls=budget.get("llm_calls", 0),
            tool_calls=budget.get("tool_calls_used", 0), tokens_in=budget.get("tokens_in", 0),
            tokens_out=budget.get("tokens_out", 0), fallback_used=bool(budget.get("fallback_used")),
            deadline_hit=state.get("stopped_by") == "deadline",
            budget_exhausted=state.get("stopped_by") == "budget", error_type=error_type)

        if error is not None:
            yield ErrorEvent(error[0], error[1])
        else:
            yield FinalResponse(AssistantResponse(**state["response"]))


def _ui_event(chunk: object) -> UiEvent | None:
    if not isinstance(chunk, dict):
        return None
    if chunk.get("type") == "progress":
        return ProgressEvent(str(chunk["step"]))
    if chunk.get("type") == "rate_limit_wait":
        return RateLimitWait(float(chunk["seconds"]))
    return None
