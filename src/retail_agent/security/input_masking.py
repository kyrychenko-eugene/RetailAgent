"""PII masking of user input (contracts §2.5). Implemented in Stage 2."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MaskResult:
    text: str                       # placeholders <EMAIL>, <PHONE>, <CARD>
    counts: dict[str, int]          # only kinds with count > 0


def mask_input(text: str) -> MaskResult:
    raise NotImplementedError
