"""Conversation graph and AgentApp facade (contracts §4.9, §4.10). Implemented in Stage 5."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterator, Literal

from retail_agent.agent.state import UiEvent
from retail_agent.agent.threads import ThreadRegistry
from retail_agent.bq_client import BigQueryRunner
from retail_agent.config import Catalog, Settings
from retail_agent.llm.wrapper import LLMClient
from retail_agent.reports.store import ReportStore
from retail_agent.security.identity import Executive, IdentityStore
from retail_agent.tracing import Tracer


@dataclass
class Deps:
    settings: Settings
    catalog: Catalog
    glossary: str
    tracer: Tracer
    identity: IdentityStore
    threads: ThreadRegistry
    reports: ReportStore
    bq: BigQueryRunner | None
    llm: LLMClient
    today: Callable[[], date] = date.today


def build_graph(deps: Deps, checkpointer):
    raise NotImplementedError


class AgentApp:
    """The only object the CLI talks to."""
    current_exec: Executive | None
    current_thread_id: str | None

    def __init__(self, deps: Deps, checkpointer) -> None:
        raise NotImplementedError

    def login(self, exec_id: str) -> Executive:
        raise NotImplementedError

    def new_thread(self) -> str:
        raise NotImplementedError

    def resume(self, thread_id: str) -> bool:
        raise NotImplementedError

    def send(self, text: str) -> Iterator[UiEvent]:
        raise NotImplementedError

    def feedback(self, rating: Literal["good", "bad"], comment: str | None) -> None:
        raise NotImplementedError

    def trace_info(self) -> tuple[str | None, Path]:
        raise NotImplementedError
