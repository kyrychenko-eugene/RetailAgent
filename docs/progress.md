# Progress

Stage log for `docs/implementation-plan.md`. Every stage updates this file (plan §3, rule 11).

| Stage | Title | Status | Date | Live LLM calls |
|---|---|---|---|---|
| 0 | Foundations | done | 2026-10-25 | 0 |
| 1 | Scaffold and infrastructure | done | 2026-10-25 | 0 |
| 2 | Security core | done | 2026-10-25 | 0 |
| 3 | LLM wrapper and resilience | done | 2026-10-25 | 1 |
| 4 | Analysis subgraph | done | 2026-10-25 | 6 |
| 5 | Conversation graph | done | 2026-10-25 | 5 |
| 6 | Saved reports and deletion | done | 2026-10-25 | 0 |
| 7 | CLI | done | 2026-10-25 | 10 |
| 8 | Observability and eval tooling | done | 2026-10-25 | about 190 |
| 9 | README and example run | done | 2026-10-25 | 43 |
| 10 | Independent review | done | 2026-10-26 | about 330 |
| 11 | Fixes | not started | | |

---

## Stage 0 — Foundations

### What was done
- **`docs/facts.md`**: every DD §20 item answered with its query or source. It covers table sizes, partitioning (none), date ranges, future-dated rows, the daily refresh, categorical cardinalities, the top brands, bytes per scoped query and the proposed cap (1 GB), Gemini model IDs, prices and free-tier status, 429 classification, the seed rule, the live run of the golden reference SQL, and calibration of the PII patterns on real values.
- **`content/catalog.yaml`**: exactly the DD §8.1 CTE columns, checked by script against DD and against the live schema. Flags: `categorical`, `high_cardinality`, `customer_identifier`, `demographic`. `blocked_columns` holds the 8 PII columns, and none of them is in the catalog.
- **`content/glossary.md`**: invariants, overridable parameters, scoped metric definitions with SQL for the easy-to-get-wrong ones (counted item, churn rate), date conventions and the partial-period rule, "why" decompositions, and analysis patterns (growth, comparisons, RFM, top customers, demographics, reports). Its SQL snippets were executed live.
- **`docs/contracts.md`**: interfaces for every module of plan §4, the config keys and env overrides, the trace schema and event types, and the CLI–graph event stream and interrupt.
- **`config.yaml`**: both profiles, every DD §18 value, a price table and eval settings.
- **`AGENTS.md`**, and this file.
- **`content/prompts/`**: `safety`, `rewrite`, `classify`, `schema_answer`, `analysis`, `report`, `report_selection`, plus `content/persona.md`. Each has a header with its placeholders and output, and the placeholders were checked by script.
- **`eval/golden_set.yaml`**: 15 business cases (the 9 client questions plus 6 capability cases), 4 placeholder resolvers and 12 security cases. All 14 reference queries and 3 query resolvers ran live for `ceo`, `brand_manager_1` and `brand_manager_2` (facts §6).
- **`eval/routing_set.yaml`**: 38 labelled messages covering all 8 intents, 18 of them borderline. Each has a rationale, and the library cases carry `library_action` / `delete_scope`.
- **`eval/security_cases.yaml`**: 69 SQL cases covering all 6 rule groups, with at least 2 rejected and at least 1 accepted per group. Cases include aliases, nested CTEs, subqueries, `UNNEST`, quoted identifiers, case variations and `SELECT *`. The file also has 15 input-masking cases and 8 result-scan cases. All SQL parses with sqlglot 30.24 (bigquery dialect). All 24 accepted queries run on BigQuery inside the scoped CTEs. A throwaway reference implementation passes all masking and scan cases; its patterns are recorded as informative in the file.
- **`eval/smoke.yaml`**: 7 dialogues. They cover a follow-up, CEO vs manager, injection and PII, an out-of-scope brand, save → delete by entity → confirm, a non-confirm continuation, and the schema / "no spike" case.
- **Environment.** Python 3.12 via a standalone `uv`, and the Google Cloud SDK in `~/google-cloud-sdk`, run with `CLOUDSDK_PYTHON` set to that Python. Application-default credentials are set up for project `project-cc255935-5e46-4e75-960`. Nothing was added to shell rc files.
- **Usage.** BigQuery: 124 query jobs, 4.3 GB billed in total (from `INFORMATION_SCHEMA.JOBS_BY_USER`; free tier). Live LLM calls: 0.

### Deviations and questions
1. **Future-dated rows (facts §1.2; DD §8.1). Resolved, at the author's decision.** `order_items` contains items up to 4 days in the future. The scoped `order_items` CTE now filters `oi.created_at <= CURRENT_TIMESTAMP()` for every scope, and this is updated in DD §8.1 (and §19), `catalog.yaml` (`filter`), `contracts.md` §2.2, the glossary and `facts.md`. The glossary and golden reference SQL no longer repeat the filter. Duration metrics keep a glossary rule for future end timestamps (`delivered_at` etc.). The golden set and the accepted security cases were re-run with the new CTE for all personas. All passed, and the only differences were small CEO deltas from items crossing "now" between the runs. Bytes were unchanged.
2. **`AnalysisOutput.outcome` and `clarification_question` (DD §10.4).** These two fields are not in DD. Plan §5 Stage 0 item 4 requires early exit and clarification to be representable, and they do that. `outcome` is generated first.
3. **`ClassifyOutput.overrides` and `delete_query` (DD §4.2, §9, §11.2).** Added so that session overrides and the entity to delete come out of the existing classify call, with no extra call.
4. **Default turn deadline of 30 s** for intents other than analysis and report (DD §10.7 defines only those two): `turn.deadline_s.default`.
5. **`pii_attempts_before_stop: 2` (DD §10.2).** DD says "repeated PII attempt → stop" without giving a number.
6. **`SELECT * FROM users` is rejected** by the quasi-identifier rule, because it outputs `id` together with demographics (DD §8.2 R12 applied literally; case QI-06). The Stage 2 `bq` acceptance test "`SELECT *` from `users` returns no PII column" must therefore run `build_scoped_sql()` output directly (bypassing the validator, as trusted test code), or use `SELECT * EXCEPT (id)` (case QI-12).
7. **Brand literal comparison is case-insensitive (DD §8.2 says "exact match").** `brand = 'calvin klein'` passes for a Calvin Klein manager and returns 0 rows, which triggers the empty-result hint. Out-of-scope literals are detected regardless of case, including inside `LOWER()`/`UPPER()`/`TRIM()` (BL-04, BL-13). Alias-renamed brand columns (`SELECT b FROM (SELECT brand AS b ...) WHERE b = 'Nike'`) are not required to be detected: the CTEs already return no rows, and the check is a UX rule, not the security control.
8. **Leaving a thread cancels its pending delete (DD §11.3 step 5).** DD names `/new` and `/login`. `/resume` of another thread also leaves the current thread, so it cancels too, via `CANCEL_SENTINEL` (contracts §6.3).
9. **RFM frequency uses fixed buckets (1 / 2 / 3+) instead of NTILE (DD §9, R14).** 69 % of the CEO's customers and 99 % of a brand manager's customers have exactly one order (facts §1.4), so frequency quantiles would split ties arbitrarily. R and M still use NTILE(5).
10. **Optional non-Google fallback (DD §14.1).** `config.yaml` lists `anthropic/claude-haiku-5-5`, used only if `ANTHROPIC_API_KEY` is set and `langchain-anthropic` is installed. The package is not in plan Stage 1's requirements list. Stage 1 may leave it out, and Stage 3 then skips the entry with a warning.
11. **Unverified values.** Prices for `gemini-3.6-flash`, `gemini-3.1-flash-lite` and `claude-haiku-5-5` are assumptions. Free-tier RPM/RPD numbers are not published by Google (facts §3). The detailed 429 body (QuotaFailure / RetryInfo) is not shown by any official page (facts §4). **Stage 3 must capture one real 429 body** and adjust `classify_rate_limit` if needed.
12. **`docs/assignment.md` arrived during the session.** It was read before the commit and cross-checked: each expected capability maps to a golden case (top customers B10, product X vs Y B05, monthly revenue B11, up-to-date revenue by product B12, database structure B08, multi-step B04/B06/B09). The "Delete all reports mentioning Client X" and "this conversation" flows map to S08 and SM-05/SM-06. No change was needed.
13. **Default analysis period** when the question names none: "all data to date", always stated (glossary §2). DD does not specify one.
14. **First commit of the author's files.** `docs/design-decisions.md`, `docs/implementation-plan.md`, `docs/clarifications.md` and `bq_client.py` were untracked and are committed together with Stage 0. Plan §2 leaves open whether `clarifications.md` (names, Slack details) stays in the public repository.

### Known issues
- The dataset is regenerated daily, so ranks, brands of the seed and every key number change. The golden runner must compare against the live reference, never against `facts.md` numbers.
- The address pattern catches only ~29 % of real thelook addresses (facts §7). That is acceptable as defense in depth, since the CTEs are the control.
- The B06 "spike" threshold (~3 pp above the previous 5 months' maximum) is a judgment call, stated in the glossary and judge notes. brand_manager_1 is close to the line (33.83 % vs a previous maximum of 33.24 %).

### Notes for Stage 1
- **Python.** No Python ≥ 3.11 is installed system-wide (`/usr/bin/python3` is 3.9). Use the uv-managed interpreter:
  ```bash
  ~/.local/uv-dl/uv-aarch64-apple-darwin/uv venv -p 3.12 .venv
  ```
- **gcloud** needs `CLOUDSDK_PYTHON=$(~/.local/uv-dl/uv-aarch64-apple-darwin/uv python find 3.12)`.
- `.gitignore` (`.DS_Store`, `.env`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `data/`, `logs/`) and `.env.example` are committed. A local `.env` exists with the project ID and a placeholder `GOOGLE_API_KEY`, which must be replaced with a real key before any `llm` run. Stage 1's `config.py` must treat the placeholder value `your-gemini-api-key` as missing.
- Verified with sqlglot 30.24.0 and google-cloud-bigquery 3.46.2. `db-dtypes` is needed for `to_dataframe()`.
- `bq` catalog drift test: compare the `content/catalog.yaml` columns and types with `get_table_schema()` of each base table. All catalog columns exist with matching types on 2026-10-25.
- BigQuery bills at least 10 MiB per referenced table, and a scoped query bills ~40 MiB (facts §2). Keep `bq` tests few.

---

## Stage 1 — Scaffold and infrastructure

### What was done
- **Package skeleton** `src/retail_agent/` per plan §4 (`pyproject.toml`, setuptools, editable install via `-e .` in `requirements.txt`). Stub modules carry the contract signatures and raise `NotImplementedError`. Pure data (dataclasses, enums, pydantic models, error classes, `UiEvent`s, `ClassifyOutput`, `AnalysisOutput`, `AgentState`) is already real, so later stages import it instead of redefining it. Seams (`load_user_preferences`, `retrieve_golden_examples`) return `[]`. Stub scripts in `scripts/`.
- **`requirements.txt`** pinned to the versions installed on 2026-10-25 (Python 3.12). A fresh venv installs it and `import retail_agent` works. `langchain-anthropic` is left out (deviation 10 of Stage 0).
- **`config.py`**: `load_settings` (profile from `LLM_PROFILE`, env overrides of contracts §1.2, `.env` loaded only when `env is None`, readable `ConfigError` naming key and file), `require_api_key` (placeholders count as missing), `load_catalog` with `Catalog.schema_prompt()` / `compact_prompt()` / `allowed_columns()`, `load_text`. Extra fallbacks are appended to a role chain only when the provider key is present.
- **`tracing.py`**: `Tracer` / `NullTracer` / `read_traces`, the helpers `security_event`, `pii_in_input`, `feedback`, `turn_summary`, secret redaction (values ≥ 8 chars), truncation with the `truncated` flag, and `validate_record` for the contract schema.
- **`bq_client.py`** moved into the package with `execute_query` / `get_table_schema` untouched. Added `dry_run`, `run_query` (parameters, `maximum_bytes_billed`, client timeout plus server `job_timeout_ms`, labels), the typed errors, `to_query_parameters`, `sanitize_labels`, `create_runner`.
- **Tests**: 63 offline tests (config, catalog, tracing, bq client with a fake google client, scaffold imports) and 3 `bq` tests (`dry_run`, `run_query` with cap and cache-proof cap breach, catalog drift against `get_table_schema()`). `conftest.py` has the fixtures `settings`, `catalog`, `fake_tracer`, `tmp_data_dir` and skips `bq` / `llm` tests when credentials are missing.

### Deviations and questions
1. **`create_runner(settings)` and `validate_record()`** are additions to contracts §1.3 / §1.4 (documented there). The first is where the "missing `GOOGLE_CLOUD_PROJECT` becomes a `ConfigError`" rule lives.
2. **`UiEvent` classes live in `agent/state.py`** (contracts §6.1 does not name a module); `llm/wrapper.py` imports them only under `TYPE_CHECKING`.
3. **Tracer validation.** `write()` rejects unknown event names, security kinds and turn outcomes with `ValueError`, so a typo fails in tests rather than producing an off-schema trace.
4. **Catalog drift test maps legacy type names** (`INTEGER`→`INT64`, `FLOAT`→`FLOAT64`, `BOOLEAN`→`BOOL`), because `get_table_schema()` returns the REST names.
5. **`.gitignore`** gained `*.egg-info/`.

### Known issues
- `BigQueryRunner._map_error` classifies a bytes-cap breach by message text (`bytesBilledLimitExceeded` / "bytes billed"). The real error was confirmed with a live breach in the `bq` test, but a change of BigQuery's wording would turn it into `BigQueryBadRequestError`.
- A cached BigQuery result bills 0 bytes and ignores `maximum_bytes_billed`; `COUNT(*)` without a filter is also free. Both only matter for tests that try to breach the cap.
- ADC prints a "no quota project" warning during `bq` tests; it is harmless.

### Notes for Stage 2
- Use `create_runner(settings)` to get a runner; `dry_run()` returns `ok=False` for bad SQL instead of raising.
- `Catalog.table.filter` holds the predicate text of each CTE (including the future-date filter on `order_items`); the CTE builder should read it from there.
- `Tracer.security_event(kind, reason_code, **detail)` accepts only the kinds in contracts §1.3.
- Run `bq` tests with `set -a; . ./.env; set +a; .venv/bin/python -m pytest -m bq`. gcloud needs `CLOUDSDK_PYTHON` as in the Stage 0 notes.

### Usage
- Live LLM calls: 0. BigQuery: a handful of dry runs and small queries from the `bq` tests (each bills ~10 MiB per referenced table; the cap-breach query bills nothing).

---

## Stage 2 — Security core

### What was done
- **`security/identity.py`**: `Scope` (`everything()`, `of()`, case-insensitive `allows()`), `IdentityStore` on the DD §7 SQLite tables (created if missing), `upsert_executive` in one transaction that replaces the brand rows, `get_executive` / `get_scope` / `list_executives`.
- **`scripts/seed_executives.py [--dry-run]`**: the facts §5 rule, with brands ranked live through `BigQueryRunner.run_query`. Run live twice on 2026-10-25: the same brands as facts §5 both times (bm1: Calvin Klein, 7 For All Mankind, True Religion; bm2: Diesel, Carhartt, Tommy Hilfiger).
- **`security/scoped_sql.py`**: the four CTEs are generated from `catalog.yaml` (columns, `scope_filter`, `join`, `filter`) as sqlglot nodes and put in front of the model's own `WITH`. Nested `WITH` and set operations work. `@brands` is a query parameter, and there is no brand filter for the CEO. `wrap_with_limit` caps rows at N+1.
- **`security/validator.py`**: every DD §8.2 rule, in the contract order. Columns are resolved with sqlglot `qualify()` (schema from the catalog, stars expanded) plus scope traversal, so aliases, model CTEs, subqueries, `UNNEST` and output aliases in `GROUP BY` / `ORDER BY` are handled. The brand literal check covers `=`, `IN`, `IN UNNEST`, `LIKE`, literals on either side and `LOWER`/`UPPER`/`TRIM`/`CAST` wrappers. The quasi-identifier rule follows column lineage through CTEs and derived tables.
- **`security/guards.py`**: `run_scoped_query` runs the repeated-query check (R11), the deadline check, the validator, the scoped CTEs, the row cap, a dry-run against the bytes cap, execution (timeout = min(query timeout, remaining − reserve), job labels), the row-limit rule (user-chosen vs agent-chosen), the PII scan of rows and cell truncation. It also writes the `tool_call` / `security_event` traces. Plus `column_values` (R10), `normalize_sql`, `scan_rows_for_pii` and `scan_text_for_pii`.
- **`security/input_masking.py`**: card (Luhn + network prefix + card-shaped grouping), then email, then phone (DD §8.5).
- **Tests**: 186 new offline tests. `test_security_cases.py` loads every case of `eval/security_cases.yaml` (69 SQL, 15 masking, 8 scan) and checks that each accepted case's scoped SQL references the dataset path only inside the generated CTEs, for both scopes. There are also `test_scoped_sql.py`, `test_guards.py` (fake runner and deadline) and `test_identity.py`. 82 new `bq` tests in `test_security_bq.py`: a manager never sees an out-of-scope brand, scoped `SELECT * FROM users` has no PII column, CEO vs manager revenue differ, live `column_values`, and free dry-runs of every accepted case and every golden query without placeholders after CTE injection, for both scopes.

### Deviations and questions
1. **`wrap_with_limit` tightens a literal top-level LIMIT in place** instead of always wrapping in `SELECT * FROM (...)` (DD §8.3). BigQuery does not guarantee the order of a subquery, so wrapping could reorder "top N" answers. A query without LIMIT gets `LIMIT N+1`, and other shapes are still wrapped. `contracts.md` §2.2 is updated.
2. **`column_values` inlines its limit** as a literal (a validated int ≤ `column_values_limit`) instead of `@limit`, so the row cap stays a literal top-level LIMIT (deviation 1). `search` stays a query parameter. The repeated-query key includes the parameters, so different searches are not "the same query". `contracts.md` §2.4 is updated.
3. **Quasi-identifier rule, closing bypasses of the literal DD §8.2 wording (R12).** An identifier also counts as output when it passes through value-returning aggregates (`MIN`, `MAX`, `ANY_VALUE`, `ARRAY_AGG`, `STRING_AGG`, `FIRST_VALUE`, …), a scalar subquery, or a whole-row reference (`TO_JSON_STRING(u)`, `SELECT u`). It also counts when it is grouped via `ROLLUP` / `CUBE` / `GROUPING SETS`, or filtered with `BETWEEN` constants or through `CAST`. Identifiers used only in `COUNT`/`SUM`-style aggregates, in window `PARTITION BY` / `ORDER BY`, or in `IN (subquery)` do not count. Derived demographics (`IF(age < 30, …)`) count as demographic output.
4. **Brand literal check** also fires for positive predicates outside `WHERE` (e.g. `CASE WHEN brand = 'Nike'`). The user message lists every out-of-scope literal, comma-separated. A `LIKE` pattern is named without its `%` wildcards.
5. **Forbidden functions** (DD §8.2 names only `EXTERNAL_QUERY`): any table-valued function in `FROM`, plus `EXTERNAL_QUERY`, `VECTOR_SEARCH`, `APPENDS`, `CHANGES`, `SESSION_USER` and the `ML.` / `AI.` / `KEYS.` / `NET.` families.
6. **Column check fallback.** When sqlglot `qualify()` gives up (an alias-qualified column missing from its source, e.g. `u.first_name`), the validator falls back to raw column names: a blocked name → `pii_column`, otherwise `unknown_column`.
7. **Identity fails closed.** An executive with `scope_all = 0` and no brand rows raises `UnknownExecutiveError`; it never becomes "all brands".
8. **Unspecified outcomes in guards (DD §10.2, §10.7):** `DEADLINE` is terminal with no user message, so the loop goes to synthesis. `TIMEOUT` and `BAD_REQUEST` are non-terminal. `PII_IN_RESULT` is non-terminal and writes `pii_in_result` with kinds and columns only. A repeated failure is non-terminal. Transient failures (timeout, unavailable, deadline) are not stored for R11. The user-facing wording for bytes cap, unavailable and repeated PII was written here (DD gives no text).
9. **`tool_call.node`** defaults to `analysis` when the tracer is not bound to a node, because the trace schema requires `node`.
10. **`tests/test_scaffold.py`** (Stage 1) asserted that two security functions are stubs. It now uses two later-stage stubs.

### Known issues
- `build_scoped_sql` regenerates the model's SQL through sqlglot. All 24 accepted security cases and the 12 golden queries without placeholders dry-run cleanly for both scopes, but an unusual BigQuery construct could still be transpiled differently. The dry-run error then goes back to the model as a normal `bad_request`.
- The quasi-identifier rule does not see identifiers hidden in arithmetic filters (`id + 0 = 5`), and it does not cover narrow demographic filters (the known gap of DD §8.4, k-anonymity in the HLD).
- The brand check is a UX rule. `REGEXP_CONTAINS`, `STARTS_WITH` and alias-renamed brand columns are not checked; the CTEs return no out-of-scope rows anyway (Stage 0 deviation 7).
- `llm/wrapper.Deadline` was a stub in this stage; it is real since Stage 3.

### Notes for Stage 4
- Build one `QueryContext` per turn (it holds the R11 store and the PII attempt counter). `run_scoped_query(sql, qctx, user_requested_granularity=...)` and `column_values(...)` never raise for model-caused problems.
- Stop the loop on `outcome.terminal` and surface `outcome.user_message` (out-of-scope brand, repeated PII, bytes cap, unavailable). `DEADLINE` is terminal with no user message: go straight to synthesis.
- Send the model `model_message` / `hint` only; `QuerySuccess.sql` is the model's own SQL. The scoped SQL is only in the `sql_executed` trace field.
- Guards already write the `tool_call` record, so the analysis tools must not write a second one. Bind the tracer with `node="analysis"` (or `report_generation`) to get the right `node`.
- Run `scan_text_for_pii()` on the final answer (DD §8.3) and write `pii_in_answer` if it finds anything.

### Usage
- Live LLM calls: 0.
- BigQuery: two `pytest -m bq` runs (5 executed queries each, ~40 MiB billed per query), three seed runs (one query each over the base tables), and about 160 dry runs (free).

---

## Stage 3 — LLM wrapper and resilience

### What was done
- **`llm/errors.py`**: `classify_rate_limit` (docs/facts.md §4 rule; also detects `RESOURCE_EXHAUSTED` in the text), `retry_after_seconds` (`Retry-After` header, `RetryInfo.retryDelay`, or "retry in Ns" in the message), plus helpers `http_status`, `is_timeout`, `is_transport_error`. They walk `__cause__` / `__context__`, so errors wrapped by `langchain-google-genai` are still recognised.
- **`llm/wrapper.py`**: `Deadline`, `TurnBudget`, `TurnContext`, and `LLMClient.call_llm` as in contracts §3.2. The default factory builds `ChatGoogleGenerativeAI` / `ChatAnthropic` with `max_retries=0`, so the SDK does not retry behind our accounting. A model is built per attempt, because its timeout depends on the remaining deadline.
- **Tests**: `tests/test_llm_wrapper.py` (20 offline tests with a fake chat model) and one `llm` test.

### Deviations and questions
1. **`LLMClient.__init__` has two extra keyword-only arguments**, `sleep` and `rng`, so tests need no real waits. `contracts.md` §3.2 is updated.
2. **Structured output uses `with_structured_output(schema, include_raw=True)`**, to get token usage and a parse error without an exception. A parse failure retries once on the same model, then raises `LLMOutputError`. `schema` and `tools` together raise `ValueError`.
3. **Failure classification not spelled out in the contract:** a per-minute 429 and a 5xx both map to `LLMUnavailableError` / trace `error_type=llm_unavailable` (the trace has no rate-limit type; the record also carries `rate_limit="per_minute"`). When every model fails only on timeouts, `LLMTimeoutError` is raised. Mixed daily-quota and outage failures raise `LLMUnavailableError`. Unknown exceptions raise `LLMError` (`internal`) without retry.
4. **Key check.** With the default factory (or when `env` is passed), a model whose provider key is missing is skipped. With an injected factory and no `env`, nothing is skipped. A missing `langchain-anthropic` skips that model silently (logged).
5. **`retry_count`** on a trace record is the number of retries on that model before the attempt; `LLMResult.retry_count` is the total for the call. `fallback_used` means the answering model is not first in the configured chain.
6. **Output-parse retry** also needs room before the deadline; it re-checks the timeout rule and raises `DeadlineExceededError` if there is none.

### Known issues
- **No real 429 body was captured** (docs/progress.md deviation 11 asked for one). A deliberate 429 needs burning the quota, so I did not trigger it. The classifier works on the documented `QuotaFailure` / `RetryInfo` shape and on the AIP-193 / `quota_exceeded` forms. Capture one body when a real 429 shows up and add it to `test_classify_helpers`.
- `langchain-google-genai` warns that `gemini-3.5-flash-lite` ignores `temperature`; harmless.

### Notes for Stage 4
- Call `call_llm(..., is_synthesis=True)` for the forced final answer, so the synthesis reserve is not subtracted from its timeout.
- Catch `DeadlineExceededError` in the loop and go to synthesis. `QuotaExhaustedError.models` and `.primary_model` name the models for the user message (DD §14.1.1).
- The wrapper writes the `llm_call` records and counts `ctx.budget.llm_calls` / tokens; nodes must not do it again.

### Usage
- Live LLM calls: 1 (one `llm` test run, free profile).

---

## Stage 4 — Analysis subgraph

### What was done
- **`analysis/subgraph.py`**: `run_analysis` as a plain ReAct loop with the expensive model, tools `run_sql` and `get_column_values`, and `final_answer` bound as a third tool, so a normal finish costs no extra call. Every executed tool call consumes the shared budget (also several in one step); a call over budget is answered "Not run" and the loop goes to synthesis. A terminal failure (out-of-scope brand, bytes cap, unavailable, repeated PII) returns `AnalysisResult(output=None, user_message=...)` at once, so an out-of-scope brand costs one expensive call. At the synthesis reserve, or when the budget is used up, one call with `tool_choice="final_answer"` and `is_synthesis=True` forces the answer; a deterministic limitation line records the stop.
- **`analysis/output.py`**: `check_output` (drops unknown query IDs and action items without a finding, trims `title`/`description`, turns a question-less clarification into `cannot_answer`, adds a limitation per fix) and `render_markdown`.
- **`analysis/tools.py`**: `make_tools`, `format_outcome_for_model` (CSV-like rows, hint, error text; never the scoped SQL), `retrieve_golden_examples` seam.
- **`agent/prompt_assembly.py`**: implemented the helpers the prompt needs (`load_prompt`, `render`, `load_persona`, `build_system_prompt`, `history_block`, `scope_description`).
- **Tests**: `tests/test_analysis.py` (21 offline tests, scripted fake model and fake runner) and `tests/test_analysis_live.py` (`llm` + `bq`).

### Deviations and questions
1. **Plain loop instead of a compiled LangGraph `StateGraph`.** Contracts §4.8 allows it. A loop makes the budget, deadline and terminal-failure rules explicit and testable; LangGraph is used for the conversation graph in Stage 5.
2. **`prompt_assembly.py` is a Stage 5 file.** It was a stub, and the analysis prompt cannot be built without it, so the small helpers were implemented here. `render` substitutes in one pass (substituted text is never rescanned, so `{{` in a question is safe). Stage 5 still owns the persona pinning and wiring.
3. **A final answer with problems gets one repair round.** When `outcome == "answer"` and `check_output` finds problems (unknown query IDs, no supporting query, an action item without a finding, a report without action items), the problems go back as the `final_answer` tool result once; the second attempt is accepted and fixed deterministically. A finding may cite only queries that succeeded.
4. **Invalid `final_answer` arguments** are fed back too; after two such attempts the forced call runs. If the forced call fails to parse or times out, a `cannot_answer` output is returned. `QuotaExhaustedError` and `LLMUnavailableError` still propagate to the turn wrapper.
5. **Final-answer PII scan** (DD §8.3): on a hit the answer is replaced by a short refusal and `pii_in_answer` is written.
6. **`stopped_by` for a `DEADLINE` failure** inside a query is `"deadline"`. A plain-text reply without a tool call also goes to the forced call.

### Known issues
- The model sometimes writes lower-case finding IDs (`f1`); they are accepted as long as action items match.
- The free profile uses Flash-Lite for the analysis, so answers can be thin (for example, the monthly-revenue answer used a 13-month window).

### Notes for Stage 5
- Build the `QueryContext` once per turn and bind the tracer with `node="analysis"` or `"report_generation"`.
- Set `ctx.budget = TurnBudget(tool_calls_limit=settings.turn.budget_for(intent))`.
- `ProgressEvent` is sent through `ctx.emit` before each tool call and before the forced answer.
- Store `render_markdown(output)` as the history answer; `AnalysisResult.output is None` means show `user_message`.

### Usage
- Live LLM calls: 6 (two golden questions for `brand_manager_1`, free profile, 3 calls each).
- BigQuery: 3 queries (about 40 MiB each).


---

## Stage 5 — Conversation graph

### What was done
- **`agent/graph.py`**: `Deps`, `build_graph` (LangGraph `StateGraph`: `mask_input` → `rewrite` (only when history exists) → `classify` → one of `analysis`, `report_generation`, `report_library`, `schema_answer`, `clarify`, `unanswerable`, `refuse` → `finalize`), `make_checkpointer` (`SqliteSaver` on `paths.checkpoint_db`) and the `AgentApp` facade with the turn wrapper (`send`), `login`, `new_thread`, `resume`, `feedback`, `trace_info`.
- **`agent/nodes.py`**: all nodes, `validate_classification` (contracts §4.1 post-validation), canned texts. Per-turn objects (deadline, `TurnBudget`, tracer, progress writer) are rebuilt from state in every node. Classify sets the per-intent deadline and tool budget (R5). `finalize` appends the turn to history (question + final answer text only), updates the clarification counter and sets the thread title.
- **`agent/threads.py`**: `ThreadRegistry` on `paths.app_db` (R1). A foreign thread writes `thread_access_denied`; an unknown one writes nothing; both answer `False`.
- **Persona**: loaded at the first turn of a thread and pinned in state; its hash is bound to every trace record of the turn.
- **Tests**: `tests/test_agent_graph.py` (22 offline, fake chat models and a fake BigQuery runner) and one `llm` test.

### Deviations and questions
1. **Input masking runs in `AgentApp.send()` before the graph**, so the raw text never reaches the checkpointer (the plan has it as the first node). The `mask_input` node still runs and re-masks. Contracts §4.10 updated.
2. **`AgentState` has two new turn fields**, `outcome` and `stopped_by`, so the facade can write `turn_summary` without re-deriving them. Contracts §4.4 updated.
3. **`rewrite` and `classify` get only the safety rules as system message**; their prompt files have no persona or user slot. `schema_answer` and analysis get the full system prompt.
4. **`llm.wrapper._text` is now public as `message_text`** (also used by nodes).
5. **Refusals and errors are not added to history.** Everything else with text is (clarifications, no-access, unanswerable, notices), so a short follow-up can be rewritten.
6. **`est_cost_usd` is not written in `turn_summary`**; the budget has no per-model split. `scripts/metrics.py` (Stage 8) can price `llm_call` records per model.
7. **No `confirm_delete` node and no pending-delete cancel on `login`/`new_thread`** (Stage 6). `report_library` is a stub returning a notice, with outcome `library`.
8. **Failed turn:** the facade closes the run with `update_state(as_node="finalize")` and `response.kind="error"`; the failed turn adds nothing to history. The exception is logged with `log.exception` (no secrets: only the exception text of the provider call).
9. **Turn id** is `<first 8 chars of thread_id>-<turn_no:04d>`.

### Known issues
- The turn deadline for the intent starts when classification ends, so rewrite and classify time (a few seconds) comes on top of the 60 s / 150 s.
- Out-of-scope brand answers cost one expensive call (Stage 4); the final text comes from the guard message, not the model.

### Notes for Stage 6
- Replace the body of `report_library_node` and add `confirm_delete` (interrupt) plus the cancel path in `AgentApp.login/new_thread/resume` (`CANCEL_SENTINEL`). `Deps.reports` is already carried; tests pass `None`.
- `state["last_answer"]` holds `AnalysisOutput.model_dump()` of the last `answer` outcome, ready for "save it".
- `send()` must resume with `Command(resume=text)` when the thread has a pending interrupt.

### Notes for Stage 7
- `AgentApp.send()` yields `ProgressEvent`, `RateLimitWait`, then one `FinalResponse` or `ErrorEvent`. `resume()` returning `False` means "Thread not found."; `login()` raises `UnknownExecutiveError`.
- The CLI writes the `session` trace records.

### Usage
- Live LLM calls: 5 (one two-turn dialogue, free profile).

---

## Stage 6 — Saved reports and deletion

### What was done
- **`reports/store.py`**: `ReportStore` on `paths.app_db` (`reports`, `audit_log`), `build_search_text`, `ReportRecord`. Every query filters by `owner_id` in SQL. `delete` removes the owned subset and writes one audit row (IDs and count only) in one transaction.
- **`reports/search.py`**: `select_reports_by_entity` (no LLM call when the owner has no reports; union of the model's owned IDs and substring matches of its terms plus the request), `select_reports_by_thread` (no LLM).
- **`reports/deletion.py`**: `make_pending_delete` (snapshot, re-checked against the owner), `confirmation_prompt`, `is_confirmation`, `execute_delete` (deletes exactly the snapshot, writes the `deletion` trace event).
- **`agent/nodes.py`**: `report_library_node` (save / list / delete), `confirm_delete_node` (`interrupt()`), `route_after_library`; `finalize` copies `notes` into `response.notes`.
- **`agent/graph.py`**: `confirm_delete` node; `send()` resumes with `Command(resume=...)` when the thread has an open interrupt and yields `ConfirmRequest`; `login`, `new_thread` and `resume` of another thread cancel a pending delete.
- **Tests**: `tests/test_reports.py` (14 offline); the Stage 5 library stub test now expects the real list answer; the scaffold test for the `NotImplementedError` stub was removed.

### Deviations and questions
1. **The reply to a confirmation is its own turn** (new `turn_no` and `turn_id`, fresh budget and deadline), not a continuation of the asking turn as contracts §6.3 said. Reason: one `turn_summary` per user message, and the old monotonic deadline may have long expired. Contracts §6.3 updated.
2. **Cancel on leaving a thread writes a `session` record** (`detail.action = "pending_delete_cancelled"`) instead of a `turn_summary`, since it is not a user turn. The `delete_cancelled` outcome stays unused.
3. **The request text is also used as a substring term** (with the model's `search_terms`), as contracts §5.2 says for `delete_query`; terms shorter than 2 characters are dropped.
4. **Hallucinated or foreign model IDs** are counted in the `report_library` node record (`model_ids_dropped`), with no `security_event`: owner-filtered SQL cannot tell the two apart.
5. **Save is not deduplicated**: asking twice saves two reports.
6. **A confirmation shown after a process restart stays valid** for the next message (the interrupt is in the checkpoint).

### Known issues
- The confirm reply is rewritten only when the thread has history, like any turn.

### Notes for Stage 7
- `send()` ends with `ConfirmRequest` when a delete awaits confirmation; the next `send(text)` answers it, so the CLI needs no special input mode. `response.notes` holds "Deletion cancelled." for the follow-up turn.
- `login`, `new_thread` and `resume` already cancel the pending delete; the CLI must only call them.
- Progress steps added: "Searching your reports", "Deleting reports".

### Usage
- Live LLM calls: 0 (all behaviour tested with fakes).

---

## Stage 7 — CLI

### What was done
- **`cli/app.py`**: `parse_command`, `Command`, `Session` (the loop, testable with an injected reader and console), `build_app` (wires `Deps` from `config.yaml` and `content/`), `main` (`--exec`, `--profile`). Rendering with `rich`: summary first, findings and actions as tables, definitions and limitations as lists, report tables for lists and confirmations.
- **Commands** as in DD §6. An unknown command prints help. `/login` and `/new` go through `AgentApp`, which clears a pending delete. `/resume` of a foreign or unknown thread prints "Thread not found." The CLI writes `session` trace records (login, new, resume, quit).
- **No login, no request:** a question typed before `/login` never reaches the agent; the CLI asks for `/login <exec_id>`. `AgentApp.send` refuses too (defence in depth).
- **Confirmation:** the prompt and the report table are printed, and the next line is the answer (the facade resumes the interrupt). Only `confirm` deletes.
- **Errors:** `ErrorEvent` messages (quota, data source, generic, each with `thread_id`) are printed in red and the loop continues. Any exception escaping `send` is mapped with `error_for`.
- **No TTY:** plain output, input lines echoed, one `... step` line per progress event, no spinner.
- **Tests:** `tests/test_cli.py` (offline): command parsing, a piped session against fakes (login, question, foreign `/resume`, save, delete with `confirm`, trace, feedback), cancelled delete, login clears a pending delete, quota error and continued loop.

### Deviations and questions
None.

### Known issues
- Third-party `FutureWarning`/`UserWarning` lines (google-auth, langchain-google-genai) appear on stderr at start and during calls.
- Ctrl-C during a turn prints "Turn interrupted." and the loop continues; the thread state is whatever the last completed node committed.

### Notes for Stage 8
- Traces from a real session are in `logs/traces.jsonl` (CEO and brand_manager_1 asking the same question, thread ids in the file).

### Usage
- Live LLM calls: 10 (manual free-profile session: CEO and `brand_manager_1`, the same monthly sales question; totals $1,233,212 vs $62,107, so scoping differs).

---

## Stage 8 — Observability and eval tooling

### What was done
- **`scripts/inspect_trace.py`**: `<thread_id|prefix> [--traces] [--full]`, turns in order, nodes, LLM calls (model, tokens, attempt, retries, fallback, prompt/response cut to 300 chars unless `--full`, "TRUNCATED when written" flag), SQL, security events, deletions, feedback, turn summary.
- **`scripts/metrics.py`**: all DD §15 metrics over `--since/--until`: error rate and errors by `error_type` (turns and failed attempts), fallback, budget, deadline and clarification rates, security events by kind, p50/p95 latency per intent, LLM/tool calls and tokens per turn, estimated cost from `config.yaml` prices, `/feedback` counts.
- **`scripts/run_routing_eval.py`**: classify with the graph's prompt and post-validation; precision/recall per intent, false refusal rate, library_action/delete_scope accuracy, misroutes.
- **`scripts/run_smoke.py`**: runs `eval/smoke.yaml` through `AgentApp` on a temp data dir (executives copied from the real app DB), checks every assertion, p50/p95 per intent.
- **`scripts/run_golden.py`**: placeholders per persona, agent run, reference SQL live through the scoped CTEs, key numbers with tolerance, deterministic security checks, optional `--judge`; `--case/--persona/--limit`.
- **`scripts/_common.py`**: shared loaders, percentile, table, throwaway app, placeholder resolution, `--rpm` throttle.
- **Tests**: `tests/test_eval_tools.py` (loaders, metrics over a fixture, inspector, routing/smoke/golden scoring, throttle).

### Deviations and questions
1. **`scripts/_common.py` added** (layout §4 lists only the five scripts) to avoid duplicating helpers.
2. **`--rpm N`** added to the three live runners (user note: Gemini 3.5 Flash-Lite allows 15 requests/minute). Contracts §8 updated.
3. **`results_brands_in_scope`** (smoke) is checked on executed SQL (every brand-scoped query has `UNNEST(@brands)`), because trace records do not hold result rows. Golden key numbers are searched in the answer text only, for the same reason.
4. The smoke `compare` note (SM-02) is printed, not checked. `eval.judge_model` is honoured by swapping the judge role's chain.
5. Golden security checks are heuristics for the free-text `pass_if` lists (blocked columns in executed SQL, email pattern, bytes cap, security events, report ids). Not run live (golden set not run: quota).

### Known issues
- **Earlier-stage bug (fixed in this stage, at the user's request):** the per-call timeout was `min(call_timeout_s, remaining - synthesis_reserve_s)`, so classify/rewrite got about 10 s or less, and Gemini rejects request deadlines under 10 s (`400 deadline too short`). Now the `cheap` role takes no reserve (it never precedes a synthesis) and `llm.min_call_timeout_s: 10` (new config key) is a floor. Contracts §1.1 and §3.2 updated; test added in `tests/test_llm_wrapper.py`.
- Free tier: 429 per minute and 503 "high demand" are frequent; they dominated the smoke failures.

### Results (free profile)
- **Routing eval:** 38 cases, accuracy 97 %, false refusal rate 0 % (0/25), library_action 6/6, delete_scope 3/3. One misroute: R27 "Delete it." (borderline) routed to `report_library` instead of `clarify`. Weakest recall: `clarify` 50 %.
- **Smoke:** first full run 2/7 dialogues passed (most failures were `llm_unavailable`/`llm_request` from 429/503 and the timeout bug above, not assertion mismatches). Reruns of single dialogues: SM-03, SM-04, SM-07 pass. After the timeout fix, SM-05 passes (14/14, `--rpm 14`) and SM-06 passes (13/13, `--rpm 10`; one earlier attempt failed on Gemini 429/503). SM-01 and SM-02 were not rerun after the first full run.
- `metrics.py` and `inspect_trace.py` verified on the fixture and on `logs/traces.jsonl`.

### Notes for Stage 9
- Use the `--rpm 14` flag for live runs. Document the routing and smoke numbers above in the README with the caveat.

### Usage
- Live LLM calls: about 190 (38 routing; about 150 smoke across the full run and reruns). Over the stated cap for smoke (≤ 40) because of reruns after rate-limit failures.

---

## Stage 9 — README and example run

### What was done
- **`README.md`**: purpose, short architecture (Mermaid) linking to DD, setup, profiles and free-tier limits, seeding, CLI and commands, persona editing, tests and markers, eval and observability scripts with the Stage 8 results and caveats, known limitations (DD §8.4, §8.5 and observed ones), framework choice with a placeholder for the author's LangGraph experience. The HLD link is a "to be added" line.
- **`docs/example-run.md`**: two real piped sessions on the `free` profile (CEO; `brand_manager_2`) with `inspect_trace.py` excerpts. Covers the schema question, CEO vs brand manager on the same question, a follow-up "why", a report with action items, save then delete by entity with `confirm`, a refused PII request and an out-of-scope brand. Only the local repo path is replaced with `<repo>`, and trace excerpts omit prompt, response and SQL lines.

### Deviations and questions
1. **Verification in a fresh venv.** Created from the real interpreter path (`python3.12 -m venv` through the uv symlink fails in `ensurepip` on this machine), then `pip install -r requirements.txt`, `pytest` (362 passed), `pytest -m bq` (87 passed), `seed_executives.py`, the CLI (piped), `inspect_trace.py`, `metrics.py`, and `--help` of the three eval runners. **Not run**: `cp .env.example .env` (it would overwrite the author's `.env`), `pytest -m llm`, and the three live eval runners (quota; Stage 8 recorded their results). `gcloud auth application-default login` was not run because credentials already exist (`gcloud` is not installed on this machine).
2. **Example run shows "delete by entity" with a brand name** (`Tommy Hilfiger`), the case covered by SM-05. The report was written from a brand-specific question for that purpose.
3. **Example run does not show a security event for the out-of-scope brand.** The brand lookup runs inside the manager's scope, finds nothing, and the model says so; no `out_of_scope_brand` event was written. The PII turn did write a `malicious_intent` event.

### Known issues
- Over the stage cap of 25 live calls: 43 (CEO session 23, brand manager session 20), because the report turn alone used 9 and a "why" turn 6. No retries or failures occurred.
- On `free`, the CEO's answers say "the executive's brands" in Limitations (model wording; scope was all brands).
- `/trace` prints the absolute path of the trace file.

### Notes for Stage 10
- Follow README from a clean venv; `python3 -m venv` works on a normal Python 3.11+, but not through uv's symlinked interpreter.
- Example session thread ids: `8b7a7e1b…` (CEO), `8131d941…` (brand_manager_2), in `logs/traces.jsonl`.
- The seeded `data/` now holds the stage's sessions; the saved report was deleted in the run.

### Usage
- Live LLM calls: 43.

---

## Stage 10 — Independent review

### What was done
- **`docs/review.md`**: findings with severity, evidence and suggested fixes; verdicts per mandatory requirement; red-team log; automated checks; coverage of DD §17 and of the Stage 1–9 acceptance criteria. No code was changed.
- **Setup check** from `README.md` in a clean venv (Python 3.12): install, `import retail_agent`, `seed_executives.py --dry-run`, offline `pytest` (362 passed), `pytest -m bq` (87 passed).
- **Automated checks:** routing eval (97 %, 0 % false refusals; same as Stage 8), smoke SM-01 and SM-02 (both pass; the two dialogues Stage 8 never re-ran).
- **Red-team:**
  - 18 live CLI attempts on the `free` profile: PII, other brands via `=` and `LIKE`, injection directly and through saved report text, other executives' threads and reports, deletion without `confirm`, cost;
  - 38 deterministic probes of the validator and guards, offline plus free dry-runs, 3 executed through `run_scoped_query`.
- **Security core reviewed line by line:** `security/validator.py`, `scoped_sql.py`, `guards.py`, `identity.py`, `input_masking.py`.

### Results
- **Verdicts:** Safety & PII **pass with fixes required**; High-Stakes Oversight, Resilience, Observability **pass**.
- **No critical findings.** No attempt reached a PII column, another brand's rows, a base table outside the CTEs, another executive's thread or report, or a deletion without `confirm`.
- **High:**
  - H1: the quasi-identifier rule (R12) is bypassed by `AVG`/`SUM`/`COUNTIF` per customer ID;
  - H2: the rule is also bypassed by passing the ID through `UNNEST([id])`.
  - Both were executed on real data through `run_scoped_query`.
- **Medium:**
  - M1: demographics linked through `order_id`, item ID or `ORDER BY id` alignment;
  - M2: identifier filters other than `=`/`IN`/`BETWEEN`;
  - M3: HLD and LangGraph experience still placeholders.
- **Low:** 10 findings (L1–L10), listed in `docs/review.md` §3.

### Deviations and questions
1. **Live-call cap exceeded (plan Stage 10: ≤ 40).** The plan asks for the routing eval (~38 calls) and the smoke run on top of a ≥ 15-attempt red-team, so the cap cannot hold. Only SM-01 and SM-02 were run (SM-03 to SM-07 passed in Stage 8, and their security content was red-teamed live). About 14 of the attempts were per-minute 429 retries.
2. **`cp .env.example .env` not run**, because it would overwrite the author's `.env` (as in Stage 9).
3. **Test reports were planted directly in the report store** (no LLM) for the injection-through-report-text test, because the classifier refused a question that carried the injected title. All planted reports were deleted afterwards. The CEO's red-team report was deleted through the CLI with `confirm`.
4. **The red-team sessions ran against the real `data/` and `logs/`** (runtime, git-ignored), so the traces stay available for `inspect_trace.py`. The thread IDs are in `docs/review.md` §4.

### Known issues
- See `docs/review.md`. `seed_executives.py` would assign different brands today than on 2026-10-25 (L7). It was run only with `--dry-run`, so `data/` still holds the Stage 2 assignment.

### Notes for Stage 11
- Fix H1 and H2 in `security/validator.py`, with new rejected cases in `eval/security_cases.yaml`, then run the focused Opus re-check the plan requires for security-core changes. M1/M2, L1, L2, L3 and L8 are cheap. `docs/review.md` §7 has the order.
- Use `pytest -m "bq and not llm"` to keep BigQuery checks off the Gemini quota (L4).

### Usage
- Live LLM calls: about 142 attempts, of which about 14 were per-minute 429 retries:
  - `pytest -m bq` (includes `test_analysis_live`): about 6;
  - red-team CLI sessions: 78;
  - routing eval: 38;
  - smoke SM-01: 9; SM-02: 11.
- BigQuery: `pytest -m bq`, about 30 MB from the red-team sessions, 3 probe executions (~10 MB each), the smoke runs, and ~25 free dry-runs.

### Follow-up: coverage of the core purpose (same day, at the user's request)
- The evals were extended to cover conversation and plain-language questions (`docs/review.md` §8):
  - routing 38 → 58 cases, with rewrite and override scoring;
  - smoke 7 → 12 dialogues, with five new assertion types;
  - golden 15 → 26 business cases, with deterministic `checks` and a definitions check;
  - `run_golden.py --category` added, and its crash on B08 fixed.
- **Deviation:** the reviewer changed eval files, eval scripts and `tests/test_eval_tools.py` (not agent code), at the user's request. AGENTS.md allows new tests and fixes of clear bugs.
- **New findings:** N1 (list answers cut short), N2 (short follow-ups refused after an unchanged rewrite), N3 (eval throttle eats the turn deadline).
- **Live LLM calls in the follow-up:** about 190.
  - CEO golden B01–B15: 73;
  - routing R39–R58: 32;
  - SM-08: 32; SM-10: 11; SM-08b: 22;
  - golden B06, B17, B22, B23, B26: 18.
  - Stage 10 total: about 330.
