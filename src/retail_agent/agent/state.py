"""Agent state, assistant response and UI events (contracts §4.4, §4.5, §6.1)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

from pydantic import BaseModel

from retail_agent.analysis.output import AnalysisOutput


class HistoryTurn(TypedDict):
    question: str
    answer: str


class PendingDelete(TypedDict):
    action_id: str
    owner_id: str
    thread_id: str
    report_ids: list[str]
    reports: list[dict]
    created_turn_id: str


class AgentState(TypedDict, total=False):
    # conversation (persisted across turns)
    thread_id: str
    owner_exec_id: str
    persona_text: str
    persona_hash: str
    history: list[HistoryTurn]
    metric_overrides: dict[str, str]
    last_answer: dict | None
    pending_delete: PendingDelete | None
    consecutive_clarifications: int
    turn_no: int
    # current turn (reset at turn start)
    turn_id: str
    user_input: str
    rewritten_question: str
    classify: dict | None
    deadline_at: float
    budget: dict
    notes: list[str]
    response: dict | None
    outcome: str | None        # turn_summary outcome, set by the node that produced the response
    stopped_by: str | None     # AnalysisResult.stopped_by


class ReportSummary(BaseModel):
    id: str
    title: str
    description: str
    created_at: str


class AssistantResponse(BaseModel):
    kind: Literal["analysis", "report", "schema_answer", "clarification", "refusal",
                  "unanswerable", "no_access", "library_list", "saved", "confirm_delete",
                  "deleted", "nothing_to_delete", "notice", "error"]
    text: str | None = None
    analysis: AnalysisOutput | None = None
    reports: list[ReportSummary] = []
    notes: list[str] = []
    thread_id: str


@dataclass(frozen=True)
class ProgressEvent:
    step: str


@dataclass(frozen=True)
class RateLimitWait:
    seconds: float


@dataclass(frozen=True)
class ConfirmRequest:
    prompt: str
    reports: list[ReportSummary]


@dataclass(frozen=True)
class FinalResponse:
    response: AssistantResponse


@dataclass(frozen=True)
class ErrorEvent:
    message: str
    error_type: str


UiEvent = ProgressEvent | RateLimitWait | ConfirmRequest | FinalResponse | ErrorEvent
