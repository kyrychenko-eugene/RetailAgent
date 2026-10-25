"""Saved reports store (contracts §5.1). Implemented in Stage 6."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from retail_agent.agent.state import ReportSummary
from retail_agent.analysis.output import AnalysisOutput


@dataclass(frozen=True)
class ReportRecord:
    id: str
    owner_id: str
    title: str
    description: str
    thread_id: str
    created_at: str
    content: AnalysisOutput
    entities: tuple[str, ...]

    def summary(self) -> ReportSummary:
        raise NotImplementedError


def build_search_text(out: AnalysisOutput) -> str:
    raise NotImplementedError


class ReportStore:
    def __init__(self, db_path: Path) -> None:
        raise NotImplementedError

    def save(self, owner_id: str, thread_id: str, content: AnalysisOutput) -> ReportRecord:
        raise NotImplementedError

    def list(self, owner_id: str) -> list[ReportSummary]:
        raise NotImplementedError

    def get(self, owner_id: str, report_id: str) -> ReportRecord | None:
        raise NotImplementedError

    def ids_by_thread(self, owner_id: str, thread_id: str) -> list[str]:
        raise NotImplementedError

    def substring_ids(self, owner_id: str, terms: Sequence[str]) -> list[str]:
        raise NotImplementedError

    def catalog_for_selection(self, owner_id: str) -> list[dict]:
        raise NotImplementedError

    def filter_owned(self, owner_id: str, ids: Iterable[str]) -> list[str]:
        raise NotImplementedError

    def delete(self, owner_id: str, ids: Sequence[str]) -> int:
        raise NotImplementedError
