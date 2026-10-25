"""Thread registry (contracts §4.6, DD §5, R1)."""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

TITLE_MAX = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    owner_exec_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    title TEXT
)
"""


class ThreadRegistry:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def create(self, owner_exec_id: str) -> str:
        thread_id = uuid.uuid4().hex
        created = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO threads (thread_id, owner_exec_id, created_at) VALUES (?, ?, ?)",
                         (thread_id, owner_exec_id, created))
        return thread_id

    def owner_of(self, thread_id: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT owner_exec_id FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
        return row[0] if row else None

    def can_open(self, thread_id: str, exec_id: str) -> bool:
        """False for unknown and foreign threads alike."""
        return self.owner_of(thread_id) == exec_id

    def list_for(self, exec_id: str, limit: int = 20) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT thread_id, created_at, title FROM threads WHERE owner_exec_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?", (exec_id, limit)).fetchall()
        return [{"thread_id": r[0], "created_at": r[1], "title": r[2]} for r in rows]

    def set_title(self, thread_id: str, title: str) -> None:
        title = " ".join(title.split())
        if len(title) > TITLE_MAX:
            title = title[: TITLE_MAX - 1].rstrip() + "…"
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE threads SET title = ? WHERE thread_id = ?", (title, thread_id))
