# AGENTS.md

Rules for coding agents working on this repository. The prototype is built in stages by separate agent sessions, following `docs/implementation-plan.md`.

## Sources of truth
- `docs/design-decisions.md` (DD) is the specification. Do not add features it does not describe; design-only items stay seams.
- `docs/contracts.md` is binding for interfaces. If you change a signature, model or error type, update it in the same commit.
- `docs/implementation-plan.md` says what each stage builds. `docs/facts.md` holds verified facts about the data and models. Do not re-guess them; re-measure when needed.
- Gaps or conflicts: take the simplest option consistent with DD §1, implement it, and record it in `docs/progress.md` under "Deviations and questions" with the DD section. Do not stop the stage.

## Scope and style
- Change only the files of your stage. The exceptions are a clear bug from an earlier stage (record it) and new tests.
- Repository layout (plan §4) is fixed: package `src/retail_agent/`, editable content in `content/`, eval data in `eval/`, scripts in `scripts/`.
- Python 3.11+. Plain functions and small modules. Use no frameworks beyond those in DD: LangGraph, langchain-google-genai, google-cloud-bigquery, sqlglot, pydantic, PyYAML, rich, python-dotenv, pytest.
- Code, comments and docs in English.
- Model IDs, budgets, limits, timeouts and paths come from `config.yaml` via `retail_agent.config`. Never hard-code them.
- Security is deterministic. Scoping, PII removal, validation and ownership live in code, never only in prompts.

## Secrets and data
- Never log, print or commit keys. `.env` is git-ignored; `.env.example` lists variable names only.
- `content/catalog.yaml` `blocked_columns` is never rendered into any prompt.
- `data/` and `logs/` are runtime directories, git-ignored.

## Tests
- `pytest` (default) is offline: no LLM, no BigQuery. It must always pass. LLM behaviour in tests uses fakes, through `LLMClient(factory=...)`.
- Markers: `bq` needs GCP credentials and `GOOGLE_CLOUD_PROJECT`; `llm` needs `GOOGLE_API_KEY`.
- Commands:
  ```bash
  pytest                 # offline
  pytest -m bq           # BigQuery integration
  pytest -m llm          # live LLM (counts against the free-tier quota)
  ```
- Deterministic security cases live in `eval/security_cases.yaml`, and their tests load that file. Do not copy the cases into test code.

## Quota hygiene
- The author's Gemini key is on the free tier. Use `LLM_PROFILE=free` for manual checks, prefer fakes, and stay under the stage's live-call cap. Record the count in `docs/progress.md`.
- BigQuery: dry-run first and stay within `bigquery.bytes_cap`. A scoped query bills about 40 MiB (`docs/facts.md` §2).

## Done means
- Every acceptance criterion of the stage passes, and the offline tests pass.
- `docs/progress.md` is updated: what was done, deviations, known issues, notes for the next stage, live LLM calls used.
- One commit named `Stage N: <title>`.
