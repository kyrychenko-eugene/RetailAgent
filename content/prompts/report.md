<!--
prompt: report
role: expensive
placeholders: {{system}}, {{today}}, {{scope}}, {{schema}}, {{glossary}}, {{overrides}},
              {{golden_examples}}, {{history}}, {{tool_budget}}, {{max_rows}}, {{question}}
output: tool calls (run_sql, get_column_values), then exactly one final_answer call
        (AnalysisOutput with at least one action item; docs/contracts.md §4.2)
-->
{{system}}

Today is {{today}} (UTC). The data covers {{scope}}.

# Task
Write a management report answering the request below: headline KPIs for the period against the comparison period, the main drivers, and concrete action items. Follow the report pattern in glossary §6.6.

# How to work
- Query BigQuery with `run_sql` over `products`, `order_items`, `orders` and `users` exactly as named in the schema (already limited to this executive's data); use only the listed columns. Use `get_column_values` for exact spellings.
- You have at most {{tool_budget}} tool calls, including retries. Start with one query for the headline KPIs of both periods, then drill into the biggest movements.
- Results return at most {{max_rows}} rows; aggregate in SQL.
- If a tool says a brand is outside this user's data, stop and tell the user they have no access to it.
- If some queries fail or the budget runs out, write the report from what succeeded and list the gaps in `limitations`.
- When the user asks for a list, ranking, breakdown or series (top N, by month, by category, per segment), write one query whose rows are that list and add it to `tables` with a short `title` and its `query_id`. The system shows that query's rows to the user, so findings can state the key points instead of repeating every row.
- Finish by calling `final_answer` once:
  - every number comes from a query result of this turn, with query IDs in each finding;
  - at least one action item, and every action item references the finding it follows from (`finding_id`); action items are specific and measurable where possible;
  - `definitions_used` states definitions, overrides, both date ranges and granularity;
  - `title` names the period and topic (e.g. "Q1 2026 performance review").

# Schema
{{schema}}

# Glossary
{{glossary}}

# Session definition overrides
{{overrides}}

{{golden_examples}}

# Recent conversation (questions and final answers only)
{{history}}

# Request
{{question}}
