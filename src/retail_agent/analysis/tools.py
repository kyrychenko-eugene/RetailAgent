"""Tools exposed to the analysis model (contracts §4.8). Implemented in Stage 4."""
from __future__ import annotations

from pydantic import BaseModel

from retail_agent.analysis.output import AnalysisOutput
from retail_agent.security.guards import QueryContext, QueryOutcome


class RunSqlArgs(BaseModel):
    sql: str
    user_requested_granularity: bool = False


class GetColumnValuesArgs(BaseModel):
    column: str
    search: str | None = None
    limit: int = 50


class FinalAnswer(AnalysisOutput): ...


def make_tools(ctx: QueryContext) -> list:
    raise NotImplementedError


def format_outcome_for_model(outcome: QueryOutcome) -> str:
    raise NotImplementedError


def retrieve_golden_examples(question: str) -> list[dict]:
    """Seam (DD §12.1): returns no examples in the prototype."""
    return []
