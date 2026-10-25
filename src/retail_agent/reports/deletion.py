"""Two-step report deletion (contracts §5.3, DD §11.3, R4)."""
from __future__ import annotations

import uuid

from retail_agent.agent.state import PendingDelete
from retail_agent.reports.search import Selection
from retail_agent.reports.store import ReportStore
from retail_agent.tracing import Tracer

CONFIRM_WORD = "confirm"
CANCEL_SENTINEL = "\x00__cancel_pending_delete__"


def make_pending_delete(owner_id: str, thread_id: str, selection: Selection,
                        store: ReportStore, turn_id: str) -> PendingDelete:
    """Snapshot of the exact IDs to delete, re-checked against the owner's reports."""
    ids = store.filter_owned(owner_id, selection.report_ids)
    by_id = {s.id: s for s in store.list(owner_id)}
    return PendingDelete(
        action_id=uuid.uuid4().hex, owner_id=owner_id, thread_id=thread_id, report_ids=ids,
        reports=[{"id": i, "title": by_id[i].title, "created_at": by_id[i].created_at}
                 for i in ids if i in by_id],
        created_turn_id=turn_id)


def confirmation_prompt(pending: PendingDelete) -> str:
    n = len(pending["report_ids"])
    head = (f"{n} report{' will' if n == 1 else 's will'} be permanently deleted. "
            f"Type `{CONFIRM_WORD}` to proceed.")
    lines = [f"- {r['title'] or r['id']} ({r['created_at'][:10]})" for r in pending["reports"]]
    return "\n".join([head, "", *lines])


def is_confirmation(text: str) -> bool:
    return text.strip().lower() == CONFIRM_WORD


def execute_delete(pending: PendingDelete, store: ReportStore, tracer: Tracer) -> int:
    """Deletes exactly `pending.report_ids` (owned subset); the search is never re-run."""
    count = store.delete(pending["owner_id"], pending["report_ids"])
    tracer.write("deletion", detail={"report_ids": list(pending["report_ids"]), "count": count})
    return count
