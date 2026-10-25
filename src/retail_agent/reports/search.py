"""Report selection by entity or thread (contracts §5.2, DD §11.2, R3)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from retail_agent.agent.prompt_assembly import load_prompt, render
from retail_agent.llm.wrapper import LLMClient, TurnContext
from retail_agent.reports.store import ReportStore

_MIN_TERM_CHARS = 2


class ReportSelectionOutput(BaseModel):
    search_terms: list[str]
    report_ids: list[str]


@dataclass(frozen=True)
class Selection:
    report_ids: list[str]
    search_terms: list[str]
    model_ids_dropped: list[str]


def _catalog_lines(rows: list[dict]) -> str:
    return "\n".join(f"{r['id']} | {r['title']} | {r['description']} | {', '.join(r['entities'])}"
                     for r in rows)


def select_reports_by_entity(request: str, owner_id: str, store: ReportStore,
                             llm: LLMClient, ctx: TurnContext, content_dir: Path) -> Selection:
    """Union of the model's IDs (intersected with the owner's) and substring matches of its terms."""
    rows = store.catalog_for_selection(owner_id)
    if not rows:
        return Selection([], [], [])
    prompt = render(load_prompt("report_selection", content_dir), request=request,
                    reports=_catalog_lines(rows))
    result = llm.call_llm("cheap", [SystemMessage(load_prompt("safety", content_dir)), HumanMessage(prompt)],
                          ctx, node="report_library", schema=ReportSelectionOutput)
    out: ReportSelectionOutput = result.parsed

    terms = [t.strip().lower() for t in [*out.search_terms, request]
             if t and len(t.strip()) >= _MIN_TERM_CHARS]
    terms = list(dict.fromkeys(terms))
    model_ids = list(dict.fromkeys(out.report_ids))
    owned_model_ids = store.filter_owned(owner_id, model_ids)
    selected = store.filter_owned(owner_id, [*owned_model_ids, *store.substring_ids(owner_id, terms)])
    return Selection(report_ids=selected, search_terms=terms,
                     model_ids_dropped=[i for i in model_ids if i not in set(owned_model_ids)])


def select_reports_by_thread(owner_id: str, thread_id: str, store: ReportStore) -> Selection:
    return Selection(report_ids=store.ids_by_thread(owner_id, thread_id), search_terms=[],
                     model_ids_dropped=[])
