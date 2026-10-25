"""Prompt assembly (contracts §4.7, DD §13)."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Sequence

from retail_agent.agent.state import HistoryTurn
from retail_agent.config import load_text
from retail_agent.security.identity import Executive, Scope

_HEADER = re.compile(r"\A\s*<!--.*?-->\s*", re.DOTALL)
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def load_prompt(name: str, content_dir: Path) -> str:
    """content/prompts/<name>.md with the header comment stripped."""
    return _HEADER.sub("", load_text(content_dir / "prompts" / f"{name}.md"), count=1).strip()


def render(template: str, **values: str) -> str:
    """Replace {{key}} in one pass, so substituted text is never rescanned.
    Raises KeyError on a placeholder without a value."""
    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"Missing value for placeholder {{{{{key}}}}}")
        return str(values[key])
    return _PLACEHOLDER.sub(sub, template)


def load_persona(content_dir: Path) -> tuple[str, str]:
    import hashlib
    text = _HEADER.sub("", load_text(content_dir / "persona.md"), count=1).strip()
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def load_user_preferences(exec_id: str) -> list[str]:
    """Seam (DD §13): per-user preferences are not stored in the prototype."""
    return []


def build_system_prompt(*, safety: str, preferences: list[str], persona: str,
                        executive: Executive, today: date) -> str:
    """Order: safety -> user preferences -> persona -> user context."""
    parts = [safety.strip()]
    if preferences:
        parts.append("# User preferences\n" + "\n".join(f"- {p}" for p in preferences))
    parts.append(persona.strip())
    parts.append(f"# User\n{executive.display_name}. Data scope: {scope_description(executive.scope)}.")
    return "\n\n".join(parts)


def history_block(history: Sequence[HistoryTurn], n: int) -> str:
    """The last n turns as question + final answer; tool results are never part of history."""
    turns = list(history)[-n:] if n > 0 else []
    if not turns:
        return "(none)"
    return "\n\n".join(f"User: {t['question']}\nAssistant: {t['answer']}" for t in turns)


def scope_description(scope: Scope) -> str:
    return "all brands" if scope.all_brands else "brands: " + ", ".join(scope.brands)
