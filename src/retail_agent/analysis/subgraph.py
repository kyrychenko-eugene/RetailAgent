"""ReAct analysis loop (contracts §4.8). Implemented in Stage 4."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from retail_agent.agent.state import AgentState
from retail_agent.analysis.output import AnalysisResult
from retail_agent.config import Catalog
from retail_agent.llm.wrapper import LLMClient, TurnContext
from retail_agent.security.guards import QueryContext
from retail_agent.security.identity import Executive


def run_analysis(question: str, *, mode: Literal["analysis", "report_generation"],
                 state: AgentState, executive: Executive, ctx: TurnContext,
                 qctx: QueryContext, llm: LLMClient, catalog: Catalog, glossary: str,
                 content_dir: Path, today: date) -> AnalysisResult:
    raise NotImplementedError
