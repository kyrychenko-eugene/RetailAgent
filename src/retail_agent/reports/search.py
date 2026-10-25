"""Report selection by entity or thread (contracts §5.2). Implemented in Stage 6."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from retail_agent.llm.wrapper import LLMClient, TurnContext
from retail_agent.reports.store import ReportStore


class ReportSelectionOutput(BaseModel):
    search_terms: list[str]
    report_ids: list[str]


@dataclass(frozen=True)
class Selection:
    report_ids: list[str]
    search_terms: list[str]
    model_ids_dropped: list[str]


def select_reports_by_entity(request: str, owner_id: str, store: ReportStore,
                             llm: LLMClient, ctx: TurnContext, content_dir: Path) -> Selection:
    raise NotImplementedError


def select_reports_by_thread(owner_id: str, thread_id: str, store: ReportStore) -> Selection:
    raise NotImplementedError
