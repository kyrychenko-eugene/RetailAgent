"""Prompt assembly (contracts §4.7, DD §13). Implemented in Stage 5."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Sequence

from retail_agent.agent.state import HistoryTurn
from retail_agent.security.identity import Executive, Scope


def load_prompt(name: str, content_dir: Path) -> str:
    raise NotImplementedError


def render(template: str, **values: str) -> str:
    raise NotImplementedError


def load_persona(content_dir: Path) -> tuple[str, str]:
    raise NotImplementedError


def load_user_preferences(exec_id: str) -> list[str]:
    """Seam (DD §13): per-user preferences are not stored in the prototype."""
    return []


def build_system_prompt(*, safety: str, preferences: list[str], persona: str,
                        executive: Executive, today: date) -> str:
    raise NotImplementedError


def history_block(history: Sequence[HistoryTurn], n: int) -> str:
    raise NotImplementedError


def scope_description(scope: Scope) -> str:
    raise NotImplementedError
