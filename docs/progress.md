# Progress

Stage log for `docs/implementation-plan.md`. Every stage updates this file (plan §3, rule 11).

| Stage | Title | Status | Date | Live LLM calls |
|---|---|---|---|---|
| 0 | Foundations | done | 2026-10-25 | 0 |
| 1 | Scaffold and infrastructure | done | 2026-10-25 | 0 |
| 2 | Security core | done | 2026-10-25 | 0 |
| 3 | LLM wrapper and resilience | done | 2026-10-25 | 1 |
| 4 | Analysis subgraph | done | 2026-10-25 | 6 |
| 5 | Conversation graph | not started | | |
| 6 | Saved reports and deletion | not started | | |
| 7 | CLI | not started | | |
| 8 | Observability and eval tooling | not started | | |
| 9 | README and example run | not started | | |
| 10 | Independent review | not started | | |
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
