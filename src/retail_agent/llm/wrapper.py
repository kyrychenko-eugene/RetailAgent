"""LLM client: fallback chain, retries, deadlines, accounting (contracts §3.2). Implemented in Stage 3."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal, Mapping, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from retail_agent.config import ModelSpec, Settings
from retail_agent.tracing import Tracer

if TYPE_CHECKING:
    from retail_agent.agent.state import UiEvent


class Deadline:
    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        raise NotImplementedError

    @classmethod
    def at(cls, monotonic_deadline: float, *, clock: Callable[[], float] = time.monotonic) -> "Deadline":
        raise NotImplementedError

    def remaining(self) -> float:
        raise NotImplementedError

    def expired(self) -> bool:
        raise NotImplementedError


@dataclass
class TurnBudget:
    tool_calls_limit: int
    tool_calls_used: int = 0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    fallback_used: bool = False

    def try_consume_tool_call(self) -> bool:
        raise NotImplementedError

    @property
    def tool_calls_remaining(self) -> int:
        raise NotImplementedError

    def as_dict(self) -> dict[str, int | bool]:
        raise NotImplementedError


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


class LLMClient:
    def __init__(self, settings: Settings, *, factory: ModelFactory | None = None,
                 env: Mapping[str, str] | None = None) -> None:
        raise NotImplementedError

    def call_llm(self, role: Literal["cheap", "expensive"], messages: Sequence[BaseMessage],
                 ctx: TurnContext, *, node: str,
                 schema: type[BaseModel] | None = None,
                 tools: Sequence[BaseTool | type[BaseModel]] | None = None,
                 tool_choice: str | None = None,
                 is_synthesis: bool = False) -> LLMResult:
        raise NotImplementedError
