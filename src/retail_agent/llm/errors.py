"""LLM error types (contracts §3.1). Exception classes are real; classification is Stage 3."""
from __future__ import annotations

from typing import Literal, Sequence


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


def classify_rate_limit(exc: BaseException) -> RateLimitKind | None:
    raise NotImplementedError


def retry_after_seconds(exc: BaseException) -> float | None:
    raise NotImplementedError
