"""Tools exposed to the analysis model (contracts §4.8)."""
from __future__ import annotations

import csv
import io

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from retail_agent.analysis.output import AnalysisOutput
from retail_agent.security.guards import (
    QueryContext, QueryFailure, QueryOutcome, QuerySuccess, column_values, run_scoped_query,
)

FINAL_ANSWER = "final_answer"


class RunSqlArgs(BaseModel):
    sql: str
    user_requested_granularity: bool = False


class GetColumnValuesArgs(BaseModel):
    column: str
    search: str | None = None
    limit: int = 50


class TableRef(BaseModel):
    title: str = Field(description="Short title of the table, e.g. 'Top 10 products by revenue'.")
    query_id: str = Field(description="ID of the query (Q1, Q2, ...) whose rows answer the list request.")


class FinalAnswer(AnalysisOutput):
    # The model only names a query; the loop attaches its real rows (analysis/output.attach_tables).
    tables: list[TableRef] = Field(  # type: ignore[assignment]
        default=[], description="Lists, rankings, breakdowns or series the user asked for: one entry per "
                                "query whose rows should be shown as a table.")


def make_tools(ctx: QueryContext) -> list[BaseTool]:
    """run_sql, get_column_values and the final_answer tool. The loop calls run_sql and
    get_column_values itself, so it can count the budget; final_answer only carries the schema."""
    def run_sql(sql: str, user_requested_granularity: bool = False) -> str:
        return format_outcome_for_model(
            run_scoped_query(sql, ctx, user_requested_granularity=user_requested_granularity))

    def get_column_values(column: str, search: str | None = None, limit: int = 50) -> str:
        return format_outcome_for_model(column_values(column, ctx, search=search, limit=limit))

    def final_answer(**_: object) -> str:
        return "Answer recorded."

    return [
        StructuredTool.from_function(
            run_sql, name="run_sql", args_schema=RunSqlArgs,
            description="Run one read-only BigQuery SELECT over products, order_items, orders and users."),
        StructuredTool.from_function(
            get_column_values, name="get_column_values", args_schema=GetColumnValuesArgs,
            description="List the values of a categorical column (table.column) with counts; "
                        "pass `search` for high-cardinality columns such as products.name."),
        StructuredTool.from_function(
            final_answer, name=FINAL_ANSWER, args_schema=FinalAnswer,
            description="Give the final structured answer. Call it exactly once, when you are done."),
    ]


def _cell(value: object) -> str:
    return "" if value is None else str(value)


def format_outcome_for_model(outcome: QueryOutcome) -> str:
    """Compact text for the ToolMessage. Never includes the scoped SQL or the dataset path."""
    if isinstance(outcome, QueryFailure):
        return f"{outcome.query_id} failed ({outcome.kind.value}): {outcome.model_message}"
    lines = [f"{outcome.query_id}: {outcome.row_count} row(s)"
             + (" (this exact query was already run in this turn; same result)" if outcome.cached else "")]
    if outcome.rows:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(outcome.columns)
        for row in outcome.rows:
            writer.writerow([_cell(row.get(c)) for c in outcome.columns])
        lines.append(buf.getvalue().rstrip("\n"))
    if outcome.hint:
        lines.append(outcome.hint)
    return "\n".join(lines)


def retrieve_golden_examples(question: str) -> list[dict]:
    """Seam (DD §12.1): returns no examples in the prototype."""
    return []
