"""ReAct analysis loop (contracts §4.8, DD §10).

The expensive model calls run_sql / get_column_values until it calls final_answer. One shared
tool-call budget and the turn deadline bound the loop; when either runs out, or a terminal
failure occurs, the loop stops and either surfaces the user message or forces final_answer.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from retail_agent.agent.prompt_assembly import (
    build_system_prompt, history_block, load_persona, load_prompt, load_user_preferences, render,
    scope_description,
)
from retail_agent.agent.state import AgentState, ProgressEvent
from retail_agent.analysis.output import (
    AnalysisOutput, AnalysisResult, QueryRecord, check_output, render_markdown,
)
from retail_agent.analysis.tools import (
    FINAL_ANSWER, FinalAnswer, format_outcome_for_model, make_tools, retrieve_golden_examples,
)
from retail_agent.config import Catalog, load_text
from retail_agent.llm.errors import DeadlineExceededError, LLMOutputError, LLMTimeoutError
from retail_agent.llm.wrapper import LLMClient, TurnContext
from retail_agent.security.guards import (
    FailureKind, QueryContext, QueryFailure, QueryOutcome, QuerySuccess, column_values,
    run_scoped_query, scan_text_for_pii,
)
from retail_agent.security.identity import Executive

NODE = {"analysis": "analysis", "report_generation": "report_generation"}
_TERMINAL_STOP = {
    FailureKind.OUT_OF_SCOPE_BRAND: "out_of_scope_brand",
    FailureKind.BYTES_CAP: "bytes_cap",
    FailureKind.UNAVAILABLE: "unavailable",
    FailureKind.PII_REPEATED: "pii_repeated",
}
_STOP_LIMITATION = {
    "budget": "The tool-call budget was used up before every check could be made, so this answer is partial.",
    "deadline": "The time limit for this answer was reached before every check could be made, "
                "so this answer is partial.",
}
_FORCE_PROMPT = {
    "budget": "The tool-call budget is used up. Call final_answer now with what the queries returned. "
              "Name every gap in `limitations`.",
    "deadline": "Time is nearly up. Call final_answer now with what the queries returned. "
                "Name every gap in `limitations`.",
    None: "Call final_answer now with what the queries returned. Name every gap in `limitations`.",
}
_PII_ANSWER_MESSAGE = "I can't show that answer because it contained personal contact data."
_MAX_BAD_FINALS = 2


def _overrides_block(state: AgentState) -> str:
    overrides = state.get("metric_overrides") or {}
    return "\n".join(f"- {k}: {v}" for k, v in overrides.items()) or "(none)"


def _golden_block(question: str) -> str:
    examples = retrieve_golden_examples(question)
    if not examples:
        return ""
    return "# Worked examples\n" + "\n\n".join(
        f"Question: {e.get('question', '')}\nSQL: {e.get('sql', '')}\nApproach: {e.get('note', '')}"
        for e in examples)


def build_prompt(question: str, *, mode: str, state: AgentState, executive: Executive, ctx: TurnContext,
                 qctx: QueryContext, catalog: Catalog, glossary: str, content_dir: Path, today: date) -> str:
    """The rendered analysis or report prompt (DD §13, §9, §12.1)."""
    persona = state.get("persona_text") or load_persona(content_dir)[0]
    system = build_system_prompt(
        safety=load_prompt("safety", content_dir), preferences=load_user_preferences(executive.exec_id),
        persona=persona, executive=executive, today=today)
    template = load_prompt("analysis" if mode == "analysis" else "report", content_dir)
    return render(
        template, system=system, today=today.isoformat(), scope=scope_description(executive.scope),
        schema=catalog.schema_prompt(), glossary=glossary, overrides=_overrides_block(state),
        golden_examples=_golden_block(question),
        history=history_block(state.get("history", []), qctx.settings.turn.history_turns),
        tool_budget=str(ctx.budget.tool_calls_limit), max_rows=str(qctx.settings.bigquery.max_rows_to_model),
        question=question)


def _record(outcome: QueryOutcome) -> QueryRecord:
    if isinstance(outcome, QuerySuccess):
        status = "empty" if outcome.row_count == 0 else "ok"
        return QueryRecord(outcome.query_id, outcome.sql, status, outcome.row_count, outcome.bytes_processed)
    return QueryRecord(outcome.query_id, outcome.sql, outcome.kind.value, None, None)


def _fallback_output(reason: str) -> AnalysisOutput:
    return AnalysisOutput(
        outcome="cannot_answer", definitions_used=[], findings=[], limitations=[reason], action_items=[],
        summary="I wasn't able to complete this analysis. " + reason, entities=[],
        title="Analysis not completed", description="The analysis could not be completed.")


def _parse_final(args: dict) -> tuple[AnalysisOutput | None, str | None]:
    try:
        return AnalysisOutput.model_validate(FinalAnswer.model_validate(args).model_dump()), None
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
        return None, f"final_answer arguments are invalid ({problems}). Call final_answer again."


def run_analysis(question: str, *, mode: Literal["analysis", "report_generation"],
                 state: AgentState, executive: Executive, ctx: TurnContext,
                 qctx: QueryContext, llm: LLMClient, catalog: Catalog, glossary: str,
                 content_dir: Path, today: date) -> AnalysisResult:
    node = NODE[mode]
    report = mode == "report_generation"
    budget = ctx.budget
    reserve = ctx.synthesis_reserve_s
    tools = make_tools(qctx)
    final_tool = [t for t in tools if t.name == FINAL_ANSWER]
    messages: list[BaseMessage] = [HumanMessage(build_prompt(
        question, mode=mode, state=state, executive=executive, ctx=ctx, qctx=qctx, catalog=catalog,
        glossary=glossary, content_dir=content_dir, today=today))]
    records: list[QueryRecord] = []
    ok_ids: set[str] = set()
    stopped_by: str | None = None
    output: AnalysisOutput | None = None
    bad_finals = 0
    repaired = False

    def finish_with_message(outcome: QueryFailure) -> AnalysisResult:
        return AnalysisResult(output=None, user_message=outcome.user_message,
                              stopped_by=_TERMINAL_STOP[outcome.kind], queries=tuple(records))

    while output is None:
        if budget.tool_calls_remaining == 0 and records:
            stopped_by = "budget"
            break
        if ctx.deadline.remaining() - reserve <= 0:
            stopped_by = "deadline"
            break
        try:
            result = llm.call_llm("expensive", messages, ctx, node=node, tools=tools)
        except DeadlineExceededError:
            stopped_by = "deadline"
            break
        message = result.message
        messages.append(message)
        calls = list(message.tool_calls or [])
        if not calls:
            break  # plain text without a tool call: force the structured answer

        final_call = next((c for c in calls if c["name"] == FINAL_ANSWER), None)
        if final_call is not None:
            parsed, error = _parse_final(final_call.get("args") or {})
            feedback = error
            if parsed is not None:
                fixed, problems = check_output(parsed, executed_query_ids=ok_ids, report=report)
                if problems and parsed.outcome == "answer" and not repaired:
                    repaired = True
                    feedback = "Fix these problems and call final_answer again: " + "; ".join(problems) + "."
                else:
                    output = fixed
            if output is None:
                bad_finals += 1 if error else 0
                messages.append(ToolMessage(feedback, tool_call_id=final_call["id"], name=FINAL_ANSWER))
                for c in calls:
                    if c is not final_call:
                        messages.append(ToolMessage("Not run: call final_answer again.",
                                                    tool_call_id=c["id"], name=c["name"]))
                if bad_finals >= _MAX_BAD_FINALS:
                    break
                continue
            break

        for call in calls:
            name, args = call["name"], call.get("args") or {}
            if stopped_by is not None or not budget.try_consume_tool_call():
                stopped_by = stopped_by or "budget"
                messages.append(ToolMessage("Not run: no more tool calls are allowed. Call final_answer.",
                                            tool_call_id=call["id"], name=name))
                continue
            ctx.emit(ProgressEvent(f"Querying data ({budget.tool_calls_used}/{budget.tool_calls_limit})"))
            if name == "run_sql":
                outcome = run_scoped_query(str(args.get("sql", "")), qctx,
                                           user_requested_granularity=bool(args.get("user_requested_granularity")))
            elif name == "get_column_values":
                outcome = column_values(str(args.get("column", "")), qctx, search=args.get("search"),
                                        limit=args.get("limit"))
            else:
                messages.append(ToolMessage(f"Unknown tool '{name}'.", tool_call_id=call["id"], name=name))
                continue
            records.append(_record(outcome))
            if isinstance(outcome, QuerySuccess):
                ok_ids.add(outcome.query_id)
            messages.append(ToolMessage(format_outcome_for_model(outcome), tool_call_id=call["id"], name=name))
            if isinstance(outcome, QueryFailure) and outcome.terminal:
                if outcome.kind in _TERMINAL_STOP:
                    return finish_with_message(outcome)
                stopped_by = "deadline"  # DEADLINE: no message, go to synthesis
        if stopped_by is not None:
            break

    if output is None:
        output = _synthesize(messages, final_tool, ctx, llm, node, stopped_by, ok_ids, report)

    output = _finalize(output, stopped_by, qctx)
    return AnalysisResult(output=output, user_message=None, stopped_by=stopped_by, queries=tuple(records))


def _synthesize(messages: list[BaseMessage], final_tool, ctx: TurnContext, llm: LLMClient, node: str,
                stopped_by: str | None, ok_ids: set[str], report: bool) -> AnalysisOutput:
    """One call with final_answer required (DD §10.6). Never raises for model-output problems."""
    messages = messages + [HumanMessage(_FORCE_PROMPT[stopped_by])]
    ctx.emit(ProgressEvent("Writing the report" if report else "Writing the answer"))
    try:
        result = llm.call_llm("expensive", messages, ctx, node=node, tools=final_tool,
                              tool_choice=FINAL_ANSWER, is_synthesis=True)
    except (DeadlineExceededError, LLMTimeoutError, LLMOutputError):
        return _fallback_output("The time limit was reached before an answer could be written.")
    call = next((c for c in result.message.tool_calls or [] if c["name"] == FINAL_ANSWER), None)
    parsed, _ = _parse_final(call.get("args") or {}) if call else (None, None)
    if parsed is None:
        return _fallback_output("The answer could not be put in the expected form.")
    fixed, _problems = check_output(parsed, executed_query_ids=ok_ids, report=report)
    return fixed


def _finalize(output: AnalysisOutput, stopped_by: str | None, qctx: QueryContext) -> AnalysisOutput:
    if stopped_by in _STOP_LIMITATION and output.outcome == "answer":
        output = output.model_copy(update={"limitations": [*output.limitations, _STOP_LIMITATION[stopped_by]]})
    hits = scan_text_for_pii(render_markdown(output))
    if hits:
        qctx.tracer.security_event("pii_in_answer", None, kinds=sorted({h.kind for h in hits}))
        return _fallback_output(_PII_ANSWER_MESSAGE).model_copy(update={"summary": _PII_ANSWER_MESSAGE})
    return output
