"""Thread registry (contracts §4.6). Implemented in Stage 5."""
from __future__ import annotations

from pathlib import Path


class ThreadRegistry:
    def __init__(self, db_path: Path) -> None:
        raise NotImplementedError

    def create(self, owner_exec_id: str) -> str:
        raise NotImplementedError

    def owner_of(self, thread_id: str) -> str | None:
        raise NotImplementedError

    def can_open(self, thread_id: str, exec_id: str) -> bool:
        raise NotImplementedError

    def list_for(self, exec_id: str, limit: int = 20) -> list[dict]:
        raise NotImplementedError

    def set_title(self, thread_id: str, title: str) -> None:
        raise NotImplementedError
