"""PII masking of user input (contracts §2.5, DD §8.5, R2).

Structured identifiers typed by the executive are replaced with placeholders before any LLM
call and before logging. Names are deliberately not detected: many brands are personal names.
Exact behaviour is fixed by `eval/security_cases.yaml` (input_masking).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 13-19 digits: groups of 4 (space or dash, the same separator throughout), Amex-style
# 4-6-5, or a single unbroken run.
_CARD = re.compile(
    r"(?<![\d-])(?:\d{4}([ -])\d{4}\1\d{4}\1\d{4}(?:\1\d{1,3})?|\d{4}([ -])\d{6}\2\d{5}|\d{13,19})(?![\d-])"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Starts with '+', or uses '-' / parentheses as separators, or is one unbroken run.
# Dots and commas are not phone separators (amounts, decimals).
_PHONE = re.compile(
    r"(?<![\w.,])(?:\+\d[\d \-()]{7,}\d|\(?\d{2,4}\)?(?:-|\) ?)\d[\d\-]{5,}\d|\d{9,15})(?![\w.,]*\d)"
)
_CARD_PREFIX = re.compile(r"^(?:4|5[1-5]|222[1-9]|22[3-9]\d|2[3-6]\d\d|27[01]\d|2720|3[47]|6011|65|64[4-9])")


@dataclass(frozen=True)
class MaskResult:
    text: str                       # placeholders <EMAIL>, <PHONE>, <CARD>
    counts: dict[str, int]          # only kinds with count > 0


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _is_card(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    return 13 <= len(digits) <= 19 and bool(_CARD_PREFIX.match(digits)) and _luhn_ok(digits)


def _is_phone(candidate: str) -> bool:
    return 9 <= sum(ch.isdigit() for ch in candidate) <= 15


def mask_input(text: str) -> MaskResult:
    """Order: CARD, then EMAIL, then PHONE (see contracts §2.5)."""
    counts: dict[str, int] = {}

    def replacer(kind: str, accept=None):
        def repl(m: re.Match) -> str:
            if accept is not None and not accept(m.group(0)):
                return m.group(0)
            counts[kind] = counts.get(kind, 0) + 1
            return f"<{kind}>"
        return repl

    masked = _CARD.sub(replacer("CARD", _is_card), text)
    masked = _EMAIL.sub(replacer("EMAIL"), masked)
    masked = _PHONE.sub(replacer("PHONE", _is_phone), masked)
    return MaskResult(text=masked, counts=counts)
