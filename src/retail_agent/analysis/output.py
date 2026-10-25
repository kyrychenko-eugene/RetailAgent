"""Structured analysis output (contracts §4.2). Models are real; check_output/render are Stage 4."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel


class Finding(BaseModel):
    id: str
    statement: str
    query_ids: list[str]


class ActionItem(BaseModel):
    action: str
    finding_id: str


class AnalysisOutput(BaseModel):
    outcome: Literal["answer", "cannot_answer", "clarification"]
    definitions_used: list[str]
    findings: list[Finding]
    limitations: list[str]
    action_items: list[ActionItem]
    summary: str
    entities: list[str]
    title: str
    description: str
    clarification_question: str | None = None


def check_output(out: AnalysisOutput, *, executed_query_ids: set[str],
                 report: bool) -> tuple[AnalysisOutput, list[str]]:
    raise NotImplementedError


def render_markdown(out: AnalysisOutput) -> str:
    raise NotImplementedError


@dataclass(frozen=True)
class QueryRecord:
    query_id: str
    sql: str
    outcome: str
    row_count: int | None
    bytes_processed: int | None


@dataclass(frozen=True)
class AnalysisResult:
    output: AnalysisOutput | None
    user_message: str | None
    stopped_by: Literal["budget", "deadline", "out_of_scope_brand", "bytes_cap",
                        "unavailable", "pii_repeated"] | None
    queries: tuple[QueryRecord, ...]
