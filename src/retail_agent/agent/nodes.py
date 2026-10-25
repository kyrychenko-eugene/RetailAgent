"""Graph nodes and the classify output model (contracts §4.1, §4.9). Nodes are Stage 5."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Intent = Literal["analysis", "report_generation", "report_library", "schema_question",
                 "clarify", "unanswerable", "off_topic", "malicious"]

OFF_TOPIC_TEXT = "I can only help with analysis of our sales, products and customers data."
MALICIOUS_TEXT = "I can't help with that request."


class MetricOverride(BaseModel):
    name: Literal["churn_window_days", "analysis_period", "comparison_period", "top_n",
                  "rfm_scores", "age_bands"]
    value: str


class ClassifyOutput(BaseModel):
    reason: str
    intent: Intent
    clarification_question: str | None = None
    library_action: Literal["save", "list", "delete"] | None = None
    delete_scope: Literal["this_conversation", "entity"] | None = None
    delete_query: str | None = None
    overrides: list[MetricOverride] = []
