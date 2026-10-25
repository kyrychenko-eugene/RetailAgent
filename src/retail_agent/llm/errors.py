"""LLM error types and provider-error classification (contracts §3.1, docs/facts.md §4)."""
from __future__ import annotations

import re
from typing import Iterator, Literal, Sequence


class LLMError(Exception):
    error_type: str = "internal"


class QuotaExhaustedError(LLMError):
    error_type = "quota_exhausted"

    def __init__(self, models: Sequence[str], role: str) -> None:
        self.models = tuple(models)
        self.role = role
        self.primary_model = self.models[0] if self.models else ""
        super().__init__(f"Daily quota exhausted for role '{role}': {', '.join(self.models)}")


class LLMUnavailableError(LLMError):
    error_type = "llm_unavailable"


class LLMTimeoutError(LLMError):
    error_type = "llm_timeout"


class DeadlineExceededError(LLMError):
    error_type = "deadline"


class LLMRequestError(LLMError):
    error_type = "llm_request"


class LLMOutputError(LLMError):
    error_type = "llm_output"


RateLimitKind = Literal["per_minute", "daily"]

_DAILY_RE = re.compile(r"PerDay|quota_exceeded|per\s+day|daily", re.IGNORECASE)
_RETRY_DELAY_RE = re.compile(r"""['"]?retryDelay['"]?\s*[:=]\s*['"]?([\d.]+)\s*(ms|s)""")
_RETRY_IN_RE = re.compile(r"retry in ([\d.]+)\s*(ms|s)", re.IGNORECASE)
_STATUS_RE = re.compile(r"^\W*(\d{3})\b")


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _blob(exc: BaseException) -> str:
    parts: list[str] = []
    for e in _chain(exc):
        parts.append(f"{type(e).__name__}: {e}")
        details = getattr(e, "details", None)
        if details is not None:
            parts.append(str(details))
    return "\n".join(parts)


def http_status(exc: BaseException) -> int | None:
    """HTTP status carried by a provider exception (SDK attribute, response, or leading text)."""
    for e in _chain(exc):
        for attr in ("code", "status_code", "http_status"):
            value = getattr(e, attr, None)
            if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
                return value
        response = getattr(e, "response", None)
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    for e in _chain(exc):
        m = _STATUS_RE.match(str(e))
        if m:
            return int(m.group(1))
    return None


def classify_rate_limit(exc: BaseException) -> RateLimitKind | None:
    """None if not a 429. Rule: docs/facts.md §4 (PerDay / quota_exceeded / 'daily' -> daily)."""
    text = _blob(exc)
    if http_status(exc) != 429 and "RESOURCE_EXHAUSTED" not in text:
        return None
    return "daily" if _DAILY_RE.search(text) else "per_minute"


def _seconds(value: str, unit: str) -> float:
    return float(value) / 1000 if unit == "ms" else float(value)


def retry_after_seconds(exc: BaseException) -> float | None:
    """From RetryInfo.retryDelay or the Retry-After header."""
    for e in _chain(exc):
        headers = getattr(getattr(e, "response", None), "headers", None)
        if headers is not None:
            try:
                raw = headers.get("retry-after") or headers.get("Retry-After")
            except Exception:
                raw = None
            if raw:
                try:
                    return max(0.0, float(raw))
                except ValueError:
                    pass
    text = _blob(exc)
    for pattern in (_RETRY_DELAY_RE, _RETRY_IN_RE):
        m = pattern.search(text)
        if m:
            return _seconds(m.group(1), m.group(2))
    return None


def is_timeout(exc: BaseException) -> bool:
    for e in _chain(exc):
        name = type(e).__name__
        if isinstance(e, TimeoutError) or "Timeout" in name or name == "DeadlineExceeded":
            return True
    return "DEADLINE_EXCEEDED" in _blob(exc) or "timed out" in _blob(exc).lower()


def is_transport_error(exc: BaseException) -> bool:
    return any(isinstance(e, (ConnectionError, OSError)) or "ConnectError" in type(e).__name__
               or "RemoteProtocolError" in type(e).__name__ for e in _chain(exc))
