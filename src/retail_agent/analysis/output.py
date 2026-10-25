"""Structured analysis output (contracts §4.2). Models are real; check_output/render are Stage 4."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel


class Finding(BaseModel):
    id: str
    statement: str
    query_ids: list[str]


class ActionItem(BaseModel):
    action: str
    finding_id: str


class AnalysisOutput(BaseModel):
    outcome: Literal["answer", "cannot_answer", "clarification"]
    definitions_used: list[str]
    findings: list[Finding]
    limitations: list[str]
    action_items: list[ActionItem]
    summary: str
    entities: list[str]
    title: str
    description: str
    clarification_question: str | None = None


TITLE_MAX = 80
DESCRIPTION_MAX = 200


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def check_output(out: AnalysisOutput, *, executed_query_ids: set[str],
                 report: bool) -> tuple[AnalysisOutput, list[str]]:
    """Deterministic fixes plus the problems found. Every fix adds a limitation line.
    A problem the model could still repair (missing action items in a report) is returned
    in the list without being hidden; the caller decides whether to feed it back."""
    problems: list[str] = []
    limitations = list(out.limitations)

    findings = []
    for f in out.findings:
        valid = [q for q in f.query_ids if q in executed_query_ids]
        dropped = [q for q in f.query_ids if q not in executed_query_ids]
        if dropped:
            problems.append(f"Finding {f.id} cites unknown queries: {', '.join(dropped)}")
            limitations.append(f"Finding {f.id} cited a query that was not run; the reference was removed.")
        if not valid:
            problems.append(f"Finding {f.id} has no supporting query")
            if not dropped:
                limitations.append(f"Finding {f.id} has no supporting query result; treat it with caution.")
        findings.append(f.model_copy(update={"query_ids": valid}))

    finding_ids = {f.id for f in findings}
    actions = []
    for item in out.action_items:
        if item.finding_id in finding_ids:
            actions.append(item)
        else:
            problems.append(f"Action item references unknown finding {item.finding_id}")
            limitations.append("An action item that did not follow from a finding was removed.")

    outcome = out.outcome
    question = out.clarification_question
    if outcome == "clarification" and not (question and question.strip()):
        problems.append("Clarification without a question")
        outcome = "cannot_answer"
    if report and outcome == "answer" and not actions:
        problems.append("A report needs at least one action item")
        limitations.append("No action items could be derived from the findings.")

    fixed = out.model_copy(update={
        "outcome": outcome, "findings": findings, "action_items": actions,
        "limitations": list(dict.fromkeys(limitations)),
        "title": _clip(out.title, TITLE_MAX), "description": _clip(out.description, DESCRIPTION_MAX),
        "clarification_question": question if outcome == "clarification" else None,
    })
    return fixed, problems


def render_markdown(out: AnalysisOutput) -> str:
    """Summary first, then findings, action items, definitions and limitations."""
    if out.outcome == "clarification" and out.clarification_question:
        return out.clarification_question
    parts = [out.summary.strip()]
    if out.findings:
        lines = []
        for f in out.findings:
            refs = f" ({', '.join(f.query_ids)})" if f.query_ids else ""
            lines.append(f"- **{f.id}.** {f.statement}{refs}")
        parts.append("**Findings**\n" + "\n".join(lines))
    if out.action_items:
        parts.append("**Recommended actions**\n" + "\n".join(
            f"- {a.action} (based on {a.finding_id})" for a in out.action_items))
    if out.definitions_used:
        parts.append("**Definitions used**\n" + "\n".join(f"- {d}" for d in out.definitions_used))
    if out.limitations:
        parts.append("**Limitations**\n" + "\n".join(f"- {x}" for x in out.limitations))
    return "\n\n".join(parts)


@dataclass(frozen=True)
class QueryRecord:
    query_id: str
    sql: str
    outcome: str
    row_count: int | None
    bytes_processed: int | None


@dataclass(frozen=True)
class AnalysisResult:
    output: AnalysisOutput | None
    user_message: str | None
    stopped_by: Literal["budget", "deadline", "out_of_scope_brand", "bytes_cap",
                        "unavailable", "pii_repeated"] | None
    queries: tuple[QueryRecord, ...]
