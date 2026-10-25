# Progress

Stage log for `docs/implementation-plan.md`. Every stage updates this file (plan §3, rule 11).

| Stage | Title | Status | Date | Live LLM calls |
|---|---|---|---|---|
| 0 | Foundations | done | 2026-10-25 | 0 |
| 1 | Scaffold and infrastructure | not started | | |
| 2 | Security core | not started | | |
| 3 | LLM wrapper and resilience | not started | | |
| 4 | Analysis subgraph | not started | | |
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
1. **Future-dated rows (facts §1.2; DD §8.1, §9).** `order_items` contains items up to 4 days in the future. This is handled as a glossary invariant (`created_at <= CURRENT_TIMESTAMP()`) and in all reference SQL. *Question for the author:* should the scoped `order_items` CTE apply this filter deterministically instead? It would be a DD §8.1 change: simpler for the model, but item-level `shipped_at`/`delivered_at` would still need it.
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
- `.env` (git-ignored) should hold `GOOGLE_CLOUD_PROJECT=project-cc255935-5e46-4e75-960` and later `GOOGLE_API_KEY`. No Gemini key exists yet.
- Add a `.gitignore` covering `.env`, `.venv/`, `data/`, `logs/`, `.DS_Store` and `__pycache__/`. The repository root currently has an untracked `.DS_Store`.
- Verified with sqlglot 30.24.0 and google-cloud-bigquery 3.46.2. `db-dtypes` is needed for `to_dataframe()`.
- `bq` catalog drift test: compare the `content/catalog.yaml` columns and types with `get_table_schema()` of each base table. All catalog columns exist with matching types on 2026-10-25.
- BigQuery bills at least 10 MiB per referenced table, and a scoped query bills ~40 MiB (facts §2). Keep `bq` tests few.
