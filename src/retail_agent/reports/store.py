"""Saved reports store (contracts §5.1, DD §11.1). Every method filters by owner in SQL."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from retail_agent.agent.state import ReportSummary
from retail_agent.analysis.output import AnalysisOutput

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    title TEXT,
    description TEXT,
    thread_id TEXT,
    created_at TEXT,
    content_json TEXT,
    entities TEXT,
    search_text TEXT
);
CREATE INDEX IF NOT EXISTS reports_owner ON reports (owner_id, created_at);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT,
    action TEXT,
    report_ids TEXT,
    count INTEGER,
    created_at TEXT
);
"""

_COLUMNS = "id, owner_id, title, description, thread_id, created_at, content_json, entities"


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
        return ReportSummary(id=self.id, title=self.title, description=self.description,
                             created_at=self.created_at)


def build_search_text(out: AnalysisOutput) -> str:
    """Lower-cased title, description, summary, findings, action items, limitations, entities."""
    parts = [out.title, out.description, out.summary,
             *(f.statement for f in out.findings),
             *(a.action for a in out.action_items),
             *out.limitations, *out.entities]
    return "\n".join(" ".join(p.split()) for p in parts if p).lower()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


class ReportStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    @staticmethod
    def _record(row: tuple) -> ReportRecord:
        return ReportRecord(
            id=row[0], owner_id=row[1], title=row[2] or "", description=row[3] or "", thread_id=row[4] or "",
            created_at=row[5], content=AnalysisOutput.model_validate_json(row[6]),
            entities=tuple(json.loads(row[7] or "[]")))

    def save(self, owner_id: str, thread_id: str, content: AnalysisOutput) -> ReportRecord:
        record = ReportRecord(
            id=uuid.uuid4().hex[:8], owner_id=owner_id, title=content.title, description=content.description,
            thread_id=thread_id, created_at=_now(), content=content, entities=tuple(content.entities))
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO reports (id, owner_id, title, description, thread_id, created_at, content_json, "
                "entities, search_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.id, owner_id, record.title, record.description, thread_id, record.created_at,
                 content.model_dump_json(), json.dumps(list(record.entities)), build_search_text(content)))
        return record

    def list(self, owner_id: str) -> list[ReportSummary]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, title, description, created_at FROM reports WHERE owner_id = ? "
                "ORDER BY created_at DESC, rowid DESC", (owner_id,)).fetchall()
        return [ReportSummary(id=r[0], title=r[1] or "", description=r[2] or "", created_at=r[3]) for r in rows]

    def get(self, owner_id: str, report_id: str) -> ReportRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM reports WHERE owner_id = ? AND id = ?",
                               (owner_id, report_id)).fetchone()
        return self._record(row) if row else None

    def ids_by_thread(self, owner_id: str, thread_id: str) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id FROM reports WHERE owner_id = ? AND thread_id = ? "
                "ORDER BY created_at DESC, rowid DESC", (owner_id, thread_id)).fetchall()
        return [r[0] for r in rows]

    def substring_ids(self, owner_id: str, terms: Sequence[str]) -> list[str]:
        """Case-insensitive substring match over `search_text`. An empty `terms` matches nothing."""
        needles = [t.strip().lower() for t in terms if t and t.strip()]
        if not needles:
            return []
        clause = " OR ".join("instr(search_text, ?) > 0" for _ in needles)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT id FROM reports WHERE owner_id = ? AND ({clause}) "
                "ORDER BY created_at DESC, rowid DESC", (owner_id, *needles)).fetchall()
        return [r[0] for r in rows]

    def catalog_for_selection(self, owner_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, title, description, entities FROM reports WHERE owner_id = ? "
                "ORDER BY created_at DESC, rowid DESC", (owner_id,)).fetchall()
        return [{"id": r[0], "title": r[1] or "", "description": r[2] or "",
                 "entities": json.loads(r[3] or "[]")} for r in rows]

    def filter_owned(self, owner_id: str, ids: Iterable[str]) -> list[str]:
        """The owned subset of `ids`, newest first, without duplicates."""
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT id FROM reports WHERE owner_id = ? AND id IN ({_placeholders(len(wanted))}) "
                "ORDER BY created_at DESC, rowid DESC", (owner_id, *wanted)).fetchall()
        return [r[0] for r in rows]

    def delete(self, owner_id: str, ids: Sequence[str]) -> int:
        """Hard delete of the owned subset in one transaction plus one audit row (ids only)."""
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return 0
        with closing(self._connect()) as conn, conn:
            owned = [r[0] for r in conn.execute(
                f"SELECT id FROM reports WHERE owner_id = ? AND id IN ({_placeholders(len(wanted))})",
                (owner_id, *wanted))]
            if not owned:
                return 0
            conn.execute(f"DELETE FROM reports WHERE owner_id = ? AND id IN ({_placeholders(len(owned))})",
                         (owner_id, *owned))
            conn.execute(
                "INSERT INTO audit_log (actor_id, action, report_ids, count, created_at) VALUES (?, ?, ?, ?, ?)",
                (owner_id, "delete", json.dumps(owned), len(owned), _now()))
        return len(owned)
