"""Interactive CLI (contracts §6.4). Implemented in Stage 7."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence


@dataclass(frozen=True)
class Command:
    name: Literal["login", "new", "resume", "trace", "feedback", "quit", "help"]
    args: tuple[str, ...]


def parse_command(line: str) -> Command | None:
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    raise NotImplementedError
