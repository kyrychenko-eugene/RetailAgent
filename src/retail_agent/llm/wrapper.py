"""LLM client: fallback chain, retries, deadlines, accounting (contracts §3.2)."""
from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal, Mapping, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from retail_agent.config import ModelSpec, Settings, require_api_key
from retail_agent.llm.errors import (
    DeadlineExceededError, LLMError, LLMOutputError, LLMRequestError, LLMTimeoutError, LLMUnavailableError,
    QuotaExhaustedError, classify_rate_limit, http_status, is_timeout, is_transport_error, retry_after_seconds,
)
from retail_agent.tracing import Tracer

if TYPE_CHECKING:
    from retail_agent.agent.state import UiEvent


log = logging.getLogger(__name__)


class Deadline:
    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._at = clock() + seconds

    @classmethod
    def at(cls, monotonic_deadline: float, *, clock: Callable[[], float] = time.monotonic) -> "Deadline":
        d = cls(0.0, clock=clock)
        d._at = monotonic_deadline
        return d

    def remaining(self) -> float:
        return self._at - self._clock()

    def expired(self) -> bool:
        return self.remaining() <= 0


@dataclass
class TurnBudget:
    tool_calls_limit: int
    tool_calls_used: int = 0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    fallback_used: bool = False

    def try_consume_tool_call(self) -> bool:
        if self.tool_calls_used >= self.tool_calls_limit:
            return False
        self.tool_calls_used += 1
        return True

    @property
    def tool_calls_remaining(self) -> int:
        return max(0, self.tool_calls_limit - self.tool_calls_used)

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "tool_calls_limit": self.tool_calls_limit, "tool_calls_used": self.tool_calls_used,
            "llm_calls": self.llm_calls, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "fallback_used": self.fallback_used,
        }


@dataclass
class TurnContext:
    thread_id: str
    turn_id: str
    exec_id: str
    intent: str | None
    deadline: Deadline
    synthesis_reserve_s: float
    budget: TurnBudget
    tracer: Tracer
    emit: "Callable[[UiEvent], None]" = lambda e: None


@dataclass(frozen=True)
class LLMResult:
    message: AIMessage
    parsed: BaseModel | None
    model: str
    fallback_used: bool
    retry_count: int
    tokens_in: int
    tokens_out: int
    latency_ms: int


ModelFactory = Callable[[ModelSpec, float], BaseChatModel]   # (spec, timeout_s) -> chat model


class _MissingProvider(Exception):
    """The provider's LangChain package is not installed."""


def _default_factory(settings: Settings, env: Mapping[str, str] | None) -> ModelFactory:
    def make(spec: ModelSpec, timeout_s: float) -> BaseChatModel:
        key = require_api_key(settings, spec.provider, env)
        common = {"temperature": settings.llm.temperature, "timeout": timeout_s, "max_retries": 0}
        if spec.provider == "google":
            from langchain_google_genai import ChatGoogleGenerativeAI
            return ChatGoogleGenerativeAI(model=spec.model, google_api_key=key, **common)
        if spec.provider == "anthropic":
            try:
                from langchain_anthropic import ChatAnthropic
            except ImportError as exc:
                raise _MissingProvider(spec.provider) from exc
            return ChatAnthropic(model=spec.model, api_key=key, **common)
        raise LLMRequestError(f"Unknown provider '{spec.provider}'")
    return make


def message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(b if isinstance(b, str) else str(b.get("text", "")) for b in content
                   if isinstance(b, (str, dict)))


def _prompt_record(messages: Sequence[BaseMessage]) -> list[dict[str, str]]:
    return [{"role": m.type, "content": message_text(m)} for m in messages]


def _response_record(message: AIMessage, parsed: BaseModel | None) -> str | dict:
    if parsed is not None:
        return parsed.model_dump(mode="json")
    if message.tool_calls:
        return {"content": message_text(message), "tool_calls": [
            {"name": c.get("name"), "args": c.get("args")} for c in message.tool_calls]}
    return message_text(message)


def _usage(message: AIMessage) -> tuple[int, int]:
    usage = getattr(message, "usage_metadata", None) or {}
    return int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)


class _OutputParseError(Exception):
    pass


class LLMClient:
    def __init__(self, settings: Settings, *, factory: ModelFactory | None = None,
                 env: Mapping[str, str] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 rng: Callable[[], float] = random.random) -> None:
        self.settings = settings
        self._env = env
        self._check_keys = factory is None or env is not None
        self._factory = factory or _default_factory(settings, env)
        self._sleep = sleep
        self._rng = rng

    # -- public ------------------------------------------------------------

    def call_llm(self, role: Literal["cheap", "expensive"], messages: Sequence[BaseMessage],
                 ctx: TurnContext, *, node: str,
                 schema: type[BaseModel] | None = None,
                 tools: Sequence[BaseTool | type[BaseModel]] | None = None,
                 tool_choice: str | None = None,
                 is_synthesis: bool = False) -> LLMResult:
        if schema is not None and tools:
            raise ValueError("call_llm takes either a schema or tools, not both")
        if role not in self.settings.roles:
            raise ValueError(f"Unknown LLM role '{role}'")
        cfg = self.settings.llm
        reserve = 0.0 if is_synthesis or role == "cheap" else ctx.synthesis_reserve_s   # cheap calls never precede a synthesis
        chain = list(self.settings.roles[role])
        env = self._env if self._env is not None else os.environ

        attempt = 0
        retries_total = 0
        daily: list[str] = []
        other_failures: list[LLMError] = []

        for position, spec in enumerate(chain):
            if self._check_keys and not self._has_key(spec, env):
                log.info("Skipping %s: no API key for provider %s", spec.model, spec.provider)
                continue
            retries = 0
            output_failed = False
            while True:
                timeout = min(cfg.call_timeout_s, ctx.deadline.remaining() - reserve)
                if timeout <= 0:
                    raise DeadlineExceededError(
                        f"No time left for an LLM call in node '{node}' "
                        f"({ctx.deadline.remaining():.1f}s remaining, reserve {reserve:.1f}s)")
                timeout = max(timeout, cfg.min_call_timeout_s)
                attempt += 1
                fallback = position > 0
                started = time.perf_counter()
                try:
                    message, parsed = self._invoke(spec, timeout, messages, schema, tools, tool_choice)
                except _MissingProvider:
                    attempt -= 1
                    break
                except Exception as exc:  # noqa: BLE001 - classified below
                    latency = int((time.perf_counter() - started) * 1000)
                    failure = self._classify(exc, spec)
                    self._account(ctx, role, spec, node, attempt, retries, fallback, messages, latency,
                                  error=failure, exc=exc)
                    if failure.kind == "fatal":
                        raise failure.error from exc
                    if failure.kind == "daily":
                        daily.append(spec.model)
                        break
                    if failure.kind == "output":
                        if output_failed:
                            raise failure.error from exc
                        output_failed = True
                        retries += 1
                        retries_total += 1
                        continue
                    other_failures.append(failure.error)
                    if retries >= cfg.max_retries:
                        break
                    wait = failure.retry_after
                    if wait is None:
                        wait = min(cfg.backoff_max_s, cfg.backoff_base_s * 2 ** retries) * self._rng()
                    if ctx.deadline.remaining() - wait < cfg.min_time_for_retry_s + reserve:
                        break
                    self._emit_wait(ctx, wait)
                    self._sleep(wait)
                    retries += 1
                    retries_total += 1
                    continue
                latency = int((time.perf_counter() - started) * 1000)
                tokens_in, tokens_out = _usage(message)
                self._account(ctx, role, spec, node, attempt, retries, fallback, messages, latency,
                              message=message, parsed=parsed, tokens=(tokens_in, tokens_out))
                return LLMResult(message=message, parsed=parsed, model=spec.model, fallback_used=fallback,
                                 retry_count=retries_total, tokens_in=tokens_in, tokens_out=tokens_out,
                                 latency_ms=latency)

        if daily and not other_failures:
            raise QuotaExhaustedError(daily, role)
        if other_failures and all(isinstance(e, LLMTimeoutError) for e in other_failures) and not daily:
            raise LLMTimeoutError(f"Every model for role '{role}' timed out")
        detail = f": {other_failures[-1]}" if other_failures else ""
        raise LLMUnavailableError(f"No model of role '{role}' could answer{detail}")

    # -- internals ---------------------------------------------------------

    def _has_key(self, spec: ModelSpec, env: Mapping[str, str]) -> bool:
        name = self.settings.provider_key_env.get(spec.provider)
        return bool(name) and bool(env.get(name, "").strip())

    def _invoke(self, spec, timeout, messages, schema, tools, tool_choice):
        model = self._factory(spec, timeout)
        if schema is not None:
            runnable = model.with_structured_output(schema, include_raw=True)
            out = runnable.invoke(list(messages))
            raw, parsed, error = out.get("raw"), out.get("parsed"), out.get("parsing_error")
            if error is not None or parsed is None or not isinstance(raw, AIMessage):
                raise _OutputParseError(str(error) if error else "structured output was empty")
            return raw, parsed
        if tools:
            kwargs = {"tool_choice": tool_choice} if tool_choice else {}
            model = model.bind_tools(list(tools), **kwargs)
        return model.invoke(list(messages)), None

    def _classify(self, exc: Exception, spec: ModelSpec) -> "_Failure":
        if isinstance(exc, _OutputParseError):
            return _Failure("output", LLMOutputError(f"{spec.model}: {exc}"), "llm_output")
        kind = classify_rate_limit(exc)
        if kind == "daily":
            return _Failure("daily", QuotaExhaustedError([spec.model], ""), "quota_exhausted")
        if kind == "per_minute":
            return _Failure("retry", LLMUnavailableError(f"{spec.model}: rate limited"), "llm_unavailable",
                            retry_after=retry_after_seconds(exc), rate_limit=kind)
        if is_timeout(exc):
            return _Failure("retry", LLMTimeoutError(f"{spec.model}: timed out"), "llm_timeout")
        status = http_status(exc)
        if status is not None and status >= 500:
            return _Failure("retry", LLMUnavailableError(f"{spec.model}: HTTP {status}"), "llm_unavailable",
                            retry_after=retry_after_seconds(exc))
        if status is not None and 400 <= status < 500:
            return _Failure("fatal", LLMRequestError(f"{spec.model}: HTTP {status}: {exc}"), "llm_request")
        if is_transport_error(exc):
            return _Failure("retry", LLMUnavailableError(f"{spec.model}: {type(exc).__name__}"), "llm_unavailable")
        return _Failure("fatal", LLMError(f"{spec.model}: unexpected {type(exc).__name__}: {exc}"), "internal")

    def _emit_wait(self, ctx: TurnContext, seconds: float) -> None:
        from retail_agent.agent.state import RateLimitWait
        ctx.emit(RateLimitWait(seconds=seconds))

    def _account(self, ctx, role, spec, node, attempt, retries, fallback, messages, latency, *,
                 message=None, parsed=None, tokens=(0, 0), error=None, exc=None) -> None:
        budget = ctx.budget
        budget.llm_calls += 1
        budget.tokens_in += tokens[0]
        budget.tokens_out += tokens[1]
        if fallback:
            budget.fallback_used = True
        fields = dict(node=node, role=role, model=spec.model, prompt=_prompt_record(messages),
                      tokens_in=tokens[0], tokens_out=tokens[1], latency_ms=latency, attempt=attempt,
                      retry_count=retries, fallback_used=fallback)
        if error is None:
            fields["response"] = _response_record(message, parsed)
        else:
            fields.update(response=f"{type(exc).__name__}: {exc}", error_type=error.error_type)
            if error.rate_limit:
                fields["rate_limit"] = error.rate_limit
        ctx.tracer.write("llm_call", **fields)


@dataclass(frozen=True)
class _Failure:
    kind: Literal["daily", "retry", "output", "fatal"]
    error: LLMError
    error_type: str
    retry_after: float | None = None
    rate_limit: str | None = None
