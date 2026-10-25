"""Two-step report deletion (contracts §5.3). Implemented in Stage 6."""
from __future__ import annotations

from retail_agent.agent.state import PendingDelete
from retail_agent.reports.search import Selection
from retail_agent.reports.store import ReportStore
from retail_agent.tracing import Tracer

CONFIRM_WORD = "confirm"
CANCEL_SENTINEL = "\x00__cancel_pending_delete__"


def make_pending_delete(owner_id: str, thread_id: str, selection: Selection,
                        store: ReportStore, turn_id: str) -> PendingDelete:
    raise NotImplementedError


def confirmation_prompt(pending: PendingDelete) -> str:
    raise NotImplementedError


def is_confirmation(text: str) -> bool:
    raise NotImplementedError


def execute_delete(pending: PendingDelete, store: ReportStore, tracer: Tracer) -> int:
    raise NotImplementedError
