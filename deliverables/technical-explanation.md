# Retail Data Analysis Agent — Technical Explanation

This document explains how the production system described in the [High-Level Design](hld.md) works, why it is built this way, and what the working prototype in this repository implements.

- Production design and diagrams: [hld.md](hld.md)
- Prototype: [README](../README.md) (setup and commands), [docs/design-decisions.md](../docs/design-decisions.md) (prototype specification), [docs/example-run.md](../docs/example-run.md) (real session transcript)

---

## 1. Choices and reasoning

### 1.1 Cloud services

The component table in [HLD §2.2](hld.md#22-components) lists every service. The decisions below are the ones that shape the system; each states what was chosen, why, and what was rejected.

**Google Cloud as the only cloud.** The data already lives in BigQuery, and the client confirmed a GCP-native stack. Keeping the models (Vertex AI), the database and the warehouse in one project lets one VPC Service Controls perimeter and one IAM model cover everything. A second cloud would add a network path and a second identity system for no gain.

**Queries run as the executive, not as a service account.** The web app holds the executive's OAuth token and the agent uses it for every BigQuery query. BigQuery then knows who is asking (`SESSION_USER()`), so the views can filter to that person's brands and the audit log names the person. The alternative, a single service account plus scoping in agent code, is what the prototype does, because the public dataset cannot hold views; in production it would mean that one bug in the agent exposes every brand. With per-user identity, the agent cannot read more than the executive could read directly.

**Brand scope inside the views, read from one entitlements table.** A table `executive → brands` (plus an all-brands flag for the CEO) is easier to maintain than Google Groups for overlapping brand portfolios, and a view filter `brand IN (entitlements of SESSION_USER())` keeps all access logic in one reviewed place. Row-level access policies were considered and not used: executives have no access to the raw tables, so a second mechanism would add no guarantee.

**Cloud Run for every service, not GKE or a managed agent runtime.** Fifty executives and a few background jobs do not justify operating a Kubernetes cluster. Cloud Run supports long streamed responses, scales per service and runs in several regions behind one load balancer. Vertex AI Agent Engine would host the LangGraph agent for us, but it gives less control over passing the executive's token to BigQuery and over streaming, which are both central here.

**A separate agent service.** The agent has a different load profile (long model calls) and a different release process (the evaluation gate) from the web app, and it is the only component that may touch BigQuery, Vertex AI and the agent's database. Keeping it behind internal ingress means the agent cannot be called from the internet at all.

**One Postgres for state, reports, memory and the Golden Bucket index.** Cloud SQL for PostgreSQL with pgvector covers the LangGraph checkpointer, relational data, full-text search and vector search. A deletion and its audit row commit in one transaction. Two schemas with separate database users keep ownership clear: the agent owns its schema, the admin app and Golden Bucket jobs own theirs, and the agent reads the admin schema read-only. Firestore plus a separate vector store would mean two more services and no transactions across them. Redis was considered as a cache for retrieval and dropped: a semantic cache still needs the embedding call (the slow, paid part), saves only a few milliseconds of database time, and a stale cache of report searches would be dangerous in the deletion flow.

**Pub/Sub and Cloud Run Jobs for background work.** Memory extraction, candidate mining and Golden Bucket ingestion must never slow down an answer. The agent publishes small events; jobs pick them up and retry on failure without affecting users.

**LangSmith for LLM traces and evaluation, Datadog for service health.** LangSmith shows every prompt, model response and tool call of a conversation and runs offline and online evaluation; it integrates with LangGraph directly. GCP writes all service logs and metrics to Cloud Logging by default; they are forwarded to Datadog for dashboards, alerts and on-call. I use both tools in production today, which matters for operating the system, not only for building it. LangSmith runs as SaaS: traces contain aggregated business figures but no PII.

### 1.2 Models

Models are configured by **role**, never named in code, so a model can be replaced by changing configuration.

| Role | Model (Vertex AI) | Used for | Why |
|---|---|---|---|
| Routing | Gemini 3.5 Flash-Lite | rewriting follow-ups, intent classification, schema answers, selecting reports for deletion, memory extraction, persona check | short structured tasks; fast and about 5× cheaper than the analysis model |
| Analysis | Gemini 3.8 Flash | writing SQL in the analysis loop, the final structured answer, Golden Bucket enrichment | generally available, strong at tool use and SQL, fits the 30-second target |
| Fallback | Gemini in the other region → another Gemini Flash model → Claude via Vertex AI | provider or quota failures | a second model family under the same IAM and billing |
| Embeddings | Gemini Embedding | Golden Bucket search | same platform; one call per question |
| Evaluation judge | a different model family (Claude via Vertex AI) | LLM-as-judge in offline and online evaluation | a judge from another family reduces the bias of a model grading its own output |

- **Why two models.** Most turns need only routing; the expensive model runs only for analysis and reports. In the prototype, routing costs about $0.001 per turn and an analysis turn about $0.06.
- **Why not Gemini Pro.** At the time of writing, Gemini 3.1 Pro on Vertex AI is a Preview model and costs more; Gemini 3.8 Flash with the glossary and Golden Bucket examples is enough for this kind of SQL. Pro can be switched in for reports once it is generally available, by configuration only.
- **Settings.** Temperature 0 for routing and SQL. Structured output (a schema) for classification and for the final answer, so the code can check the answer instead of parsing text.

### 1.3 Framework: LangGraph

**Why LangGraph.** The conversation is a graph, not a chain: it branches by intent, loops back for SQL self-correction, and pauses for human confirmation before a deletion. LangGraph gives exactly these pieces:

- explicit nodes and conditional edges, so routing and safety steps are code that can be tested without a model;
- `interrupt` for the delete confirmation, with the pending state stored in the checkpointer;
- a Postgres checkpointer, so any agent instance can continue any conversation and a restart loses nothing;
- streaming of node events, which drives the progress messages in the UI;
- tracing in LangSmith out of the box.

**Alternatives considered.**

| Alternative | Why not |
|---|---|
| A single tool-calling agent loop (for example LangChain's prebuilt agent) | no explicit routing; refusals, deletion and confirmation would depend on the model choosing correctly |
| Multi-agent frameworks (CrewAI, LangGraph supervisor) | one agent with tools is enough; several agents add model calls, latency and cost per turn |
| Google Agent Development Kit | GCP-native and a reasonable option, but I have no production experience with it |
| Plain Python | possible (the prototype's analysis loop is plain Python), but checkpointing, interrupts and streaming would have to be rebuilt |

**My experience with LangGraph.** Two years, three LLM agents in production at Similarweb, all built on LangGraph:

- **Activation Insights Agent** (LangGraph + MCP): two-model routing, a lightweight model for tool selection and parallel tool execution; cost per query down from $0.50 to about $0.06 (−88 %), latency from 50 s to about 14 s.
- **Multilingual Competitors Agent**: a parallel fan-out / fan-in graph for a latency-critical use case, with a single structured-output call.
- **RAG onboarding agent**: hybrid retrieval over PostgreSQL + pgvector (dense + full-text, fused with weighted Reciprocal Rank Fusion), semantic routing, Redis semantic cache; cost per request down about 10×, latency from 11–12 s to 3 s.

The same patterns appear here: two-model routing, hybrid retrieval with relevance floors, structured final output, and evaluation in LangSmith.

---

## 2. Data flow between components

[HLD §2](hld.md#2-system-overview) shows the components and [HLD §4](hld.md#4-key-flows) the main flows. This section follows the data itself: what moves, where it is stored, and what a model gets to see.

### 2.1 Sign-in

1. The executive signs in to the web app with Google Workspace. The web app keeps the ID token and an OAuth access token with BigQuery scope in its server-side session, and refreshes the access token before it expires (about one hour).
2. The web app calls the agent's `start session` endpoint. The agent reads the executive's brands from `secure.my_scope` (a view that returns only the caller's own entitlements) with the executive's token, loads their memory from Postgres, and stores both as the **session snapshot**.

### 2.2 One turn

| Step | Data | Where it goes |
|---|---|---|
| Question arrives | message text, thread ID; the executive's token travels in a header | web app → agent; the token is never written to state, traces or logs |
| Masking | e-mails, phone numbers and card numbers replaced by placeholders | only the masked text reaches state, models and traces |
| Routing | masked question, last 6 turns (question and final answer only), a compact schema | routing model on Vertex AI |
| Golden retrieval | the rewritten question; its embedding | Vertex AI (embedding), Postgres (search); up to 3 anonymised trios go into the prompt |
| Analysis | prompt (safety rules, preferences, persona, schema, glossary, examples, history); SQL written by the model | analysis model on Vertex AI; SQL to BigQuery as the executive |
| Query results | at most 200 rows per query, long text cells cut, scanned for PII patterns | back to the model **for this turn only**; never stored in history |
| Final answer | structured: definitions, findings, limitations, action items, summary, entities, title | checked in code, stored in the checkpoint as the last answer, streamed to the browser |
| Events | thread ID, turn ID, executive ID, intent, outcome, latency, cost; no message content | Pub/Sub |
| Traces | every prompt, response, SQL and tool result of the turn | LangSmith (14 days) |

Every model call stays inside Vertex AI in the perimeter; query results are sent only to the model that needs them in that turn.

### 2.3 After the conversation

A conversation counts as ended when the executive starts a new one, signs out, or has been idle for 30 minutes (a scheduled sweep publishes the event).

- **Memory extraction.** The memory job reads the conversation from the agent schema and the user's current memory, and asks the routing model which preferences to add, change or remove (for example "prefers tables", "wants short summaries", "usually compares with the previous quarter"). Memory holds preferences only, never figures, and is capped at a fixed number of short entries, so the oldest or least used entries are replaced. The executive sees and deletes entries on a settings page.
- **Candidate mining.** Every night the miner asks LangSmith for conversations with an online-evaluation score above the threshold and positive feedback, and sends them through the Golden Bucket pipeline ([HLD §4.3](hld.md#43-golden-bucket)).

### 2.4 Admin changes

- **Persona.** An admin saves new text; the routing model checks that it changes tone and style only (not safety rules, not data access, not the requirement to state definitions and limitations); a passing version becomes active. Conversations that are already open keep the version they started with; new ones get the new version.
- **Entitlements.** An admin changes a row in the entitlements table. The next BigQuery query of that executive already sees the new scope; the brand list in the prompt follows at the next sign-in.
- **Golden Bucket review.** An admin approves, edits or rejects candidates; approved trios are embedded and indexed immediately.

---

## 3. Requirements one by one

Each requirement has three parts: the production design, what the prototype implements (the prototype covers requirements 2, 3, 5 and 7 end to end; the others are designed, with seams in the code), and how it is verified.

### 3.1 Hybrid intelligence (Golden Bucket)

**Production design.**

*What a trio is.* A trio is stored in the admin schema with: the anonymised question, the anonymised SQL (written against the `secure` views), an interpretation note (which metric definitions were chosen, the steps, the caveats), tags, its source (analyst file or conversation), its status (`pending`, `approved`, `rejected`, `stale`), the review fields (which admin approved it, when), a link to a near-duplicate if one exists, an embedding and a full-text vector. The analyst's report text with figures is never stored in the index.

*How it is updated over time* ([HLD §4.3](hld.md#43-golden-bucket)):

1. Analysts drop trio files into Cloud Storage; each new file triggers ingestion through Pub/Sub.
2. Every night, the candidate miner takes conversations from LangSmith whose online-evaluation score is above the threshold and that received positive feedback from the executive.
3. Both go through the same pipeline: the SQL is validated (dry-run and run on the `secure` views), brands and PII are anonymised, near-duplicates are linked, and the analysis model writes the interpretation note and tags.
4. An admin approves, edits or rejects each candidate in the admin app. Only approved trios are embedded and indexed.
5. A nightly job dry-runs every approved trio against the current views; broken trios become `stale` and leave retrieval until fixed.

*How relevant data reaches the agent at question time:*

1. The `retrieve_golden` node runs before every analysis, on the rewritten (standalone) question.
2. Dense search (embedding, cosine similarity) and full-text search run in one Postgres query; each list keeps only results above its own floor.
3. The survivors are fused with weighted Reciprocal Rank Fusion; the top 1 (analysis) or 2–3 (report) trios go into the prompt as "how analysts approached a similar question": question, SQL, interpretation note.
4. If nothing passes the floors, no example is added; the glossary and its analysis patterns still guide the model.

The floors and fusion weights are tuned on the evaluation set, which is kept out of the index. Tracked metrics: share of questions with an example, evaluation score with and without an example, trios never retrieved (candidates for retirement).

**Why anonymised brands.** An analyst's trio about one brand could otherwise show figures or brand names to a brand manager who has no access to that brand. With placeholders, the example carries the method, not the data.

**Prototype.** The Golden Bucket is a seam: `retrieve_golden_examples()` returns nothing and has a slot in the analysis prompt. The analysts' logic is carried by the metric glossary (`content/glossary.md`): definitions, "why" decompositions, and analysis patterns such as RFM segmentation and period-over-period growth. The evaluation cases (`eval/golden_set.yaml`) already use the trio format, which is also the production storage format.

### 3.2 Safety and PII

**Production design.** Five layers ([HLD §4.2](hld.md#42-secure-data-access)). The first three make the agent behave well; the fourth makes the data safe even if they fail.

1. **Input.** Structured identifiers in the message are masked before any model call or log write. The intent classifier sends anything that is not an analysis question to a fixed refusal, with no further model call, and records a security event for malicious requests.
2. **Prompt.** Safety rules come first and cannot be changed by the persona or preferences. The model sees only the schema of the `secure` views; PII columns and the raw tables do not exist from its point of view.
3. **Query guard (code).** An AST validator (sqlglot) accepts exactly one `SELECT` over the `secure` views; rejects DML, DDL, scripting, system tables and table functions; checks columns against an allowlist; and rejects a customer ID returned together with demographics (age, gender, city, state, country, traffic source), also when hidden behind aggregates or `UNNEST`. A dry-run with a bytes cap runs before every query.
4. **BigQuery, as the executive.** IAM gives executives the `secure` views only. The views contain no PII columns and filter to the brands in the caller's entitlements. Customer and order data is reached only through purchases of in-scope brands.
5. **Output.** Query results are scanned for e-mail, phone and address patterns before they reach the model; the final answer is scanned again. Threads and reports are always filtered by owner.

*Indirect prompt injection* (instructions hidden in product names, saved report text or trios) is handled the same way: such text reaches the model only as data, and every control that matters (scoping, PII columns, deletion ownership) is enforced in code or in the database, not by the model obeying instructions. When the cheap model selects reports for deletion, it can only return IDs, which code intersects with the owner's own reports.

**Prototype.** All five layers exist, with one substitution: the public dataset cannot hold views, so the prototype builds the same scoped, PII-free tables in code as `WITH` clauses that are put in front of every model query through the sqlglot syntax tree, and binds the brand list as a query parameter. The validator, input masking, result and answer scans, thread ownership (`/resume` of another executive's thread answers "not found") and report ownership are implemented.

**Verified.**
- 69+ deterministic SQL attack cases in `eval/security_cases.yaml` (aliases, nested `WITH`, subqueries, `UNNEST`, quoted identifiers, `SELECT *`), plus masking and scan cases, all run offline.
- BigQuery tests: a brand manager never receives another brand's rows; `SELECT *` on users returns no PII column; the same question gives different totals for the CEO and a brand manager.
- An independent review session red-teamed the CLI (18 live attempts) and the validator (38 probes). No attempt reached a PII column, another brand's rows, a base table, another executive's thread or report. Two quasi-identifier bypasses it found were fixed in the next stage.
- Routing evaluation: 58 labelled messages, 0 % of legitimate questions refused.

**Known limitation.** A very narrow demographic filter can still return an aggregate over one customer ([HLD §9](hld.md#9-known-limitations-and-next-steps)).

### 3.3 High-stakes oversight (deleting reports)

**Production design** ([HLD §4.4](hld.md#44-deleting-reports)).

- Deletion is a deterministic graph branch, never a tool the model can call.
- Reports are found by thread ("from this conversation", no model) or by topic ("mentioning Diesel"): a cheap-model selection plus a full-text match over the full report text, combined, so a brand mentioned only inside a finding is still found.
- The exact IDs are frozen in the conversation state and listed with titles and dates. The executive types `confirm`.
- `confirm` deletes exactly the frozen IDs, filtered again by owner, and writes the audit row in the same transaction. Any other message cancels the deletion, says so, and is answered as a normal message. Starting a new conversation or signing out also cancels it.
- Hard delete: the report is gone immediately; backups (7 days) and traces (14 days) expire by retention; the audit log keeps metadata only.

**Why typed `confirm` and not a button.** Typing a word is a deliberate act, the same pattern as tools that make you type a resource's name before deleting it; and it keeps the chat as the single interface.

**Prototype.** Implemented as designed, with SQLite for reports and the audit log and LangGraph `interrupt` for the confirmation.

**Verified.** 14 offline tests: ownership on every operation, hallucinated IDs never reach the confirmation list, a brand mentioned only in a finding is found, `confirm` deletes exactly the snapshot even when a matching report was saved after the search, a non-`confirm` message is answered as a new turn, the audit log holds no content. Live review: `yes` cancelled the deletion; a report whose description contained an injected instruction did not change the selection.

### 3.4 Learning loop

**User level: preferences memory.**

- **What is stored.** Short preference statements per executive: output format (tables or bullets), depth, charts or text, usual comparison baseline, recurring focus (for example "usually asks about the women's department"). Never figures, never query results.
- **How it is learned.** Only in the background, never during a turn, so learning adds no latency to answers. After a conversation ends, the memory job gives the routing model the conversation and the current memory and asks for operations: add, update or remove, each with the evidence from the conversation. An explicit statement ("I prefer tables") is stored on the next run of the job; an inferred preference is stored only after it appears in more than one conversation, so a single request is not over-learned. New entries are used from the next sign-in.
- **Bounded.** A fixed maximum of short entries per executive; when full, the least used entry is replaced.
- **How it is used.** Loaded into the session snapshot at sign-in and placed in the prompt above the persona, as agreed with the client: preferences decide format and depth, the persona decides tone.
- **Control.** The executive sees every entry on a settings page and can delete any of them.

**System level.**

- Well-rated conversations become Golden Bucket candidates (§3.1); after admin approval, every executive benefits from the way a good answer was built.
- The per-user memory improves each executive's experience without changing the system for others.
- Failures surface through online scores, feedback and alerts; fixes to prompts or the glossary go through a pull request and the evaluation gate (§6), never through automatic prompt rewriting, because each such change needs a full regression run to be safe.

**Prototype.** Seams only: `load_user_preferences()` returns nothing and has its slot in prompt assembly, in the agreed order (safety, preferences, persona). `/feedback good|bad [comment]` is recorded in the traces as the input to the learning loop.

### 3.5 Resilience and graceful error handling

**Production design.** SQL errors and empty results are corrected by the model inside a fixed tool budget; infrastructure failures are retried and failed over without using that budget; every limit ends in a partial answer rather than an error. Section 4 lists every failure and its reaction.

**Prototype.** Implemented: the self-correction loop with the shared tool budget, repeated-query detection, empty-result hints, the row limit, the bytes cap, per-intent deadlines with a synthesis reserve, the model wrapper with retries, backoff and a fallback chain, a dedicated message for exhausted daily quota, and a turn wrapper that never crashes the CLI.

**Verified.** Offline tests with a fake model and a fake BigQuery: retry on per-minute rate limits, no retry on client errors, fallback and quota exhaustion, deadline capping, budget exhaustion leading to a partial answer, a failed turn leaving the conversation usable. Live: per-minute rate-limit responses during the review were retried with a visible "waiting" note and every turn completed.

### 3.6 Quality assurance

**Production design.** Deterministic tests, offline evaluation sets as a release gate, online scoring of live traffic, user feedback, and UX testing with executives. Section 6 describes each.

**Prototype.** pytest suites (offline, BigQuery, model), a routing evaluation, scripted multi-turn dialogues and a golden set run live against reference SQL, all in the repository.

### 3.7 Observability

**Production design.** LangSmith traces every model call and tool call, grouped by conversation and turn; Datadog holds service metrics, agent metrics and alerts. Section 5 lists the metrics and the debugging path.

**Prototype.** Structured JSONL traces (`logs/traces.jsonl`) with the full message correspondence, `scripts/inspect_trace.py` to replay one conversation, and `scripts/metrics.py` to aggregate failure rates, latency and cost over a time window.

### 3.8 Persona management

**Production design.**

- The persona is text stored in the admin schema with full version history. The CEO or an admin edits it in the admin app; no approval step, as agreed with the client.
- On save, the routing model checks the new text against fixed rules: it may change tone and style only; it may not touch safety rules, data access, or the requirement to state metric definitions and limitations. A failing text is rejected with the reason. Data security does not depend on this check (it is enforced in the database), but answer quality does.
- New conversations use the newest version; an open conversation keeps the version it started with, so the tone never changes mid-conversation. Rollback is "activate an older version".
- The persona version (hash) is recorded on every trace, so a change in online scores can be traced to a persona change.

**Prototype.** `content/persona.md` is read at the start of each conversation and pinned in its state; editing the file changes the tone of new conversations without a restart or redeploy. The persona hash is written to every trace record.

---

## 4. Error handling and fallback strategies

Two principles run through every row below:

1. **Two kinds of retries, two budgets.** Mistakes the model can fix (bad SQL, empty results) use the turn's **tool budget** (6 calls for analysis, 10 for reports). Infrastructure failures (timeouts, rate limits, outages) are retried by code and **do not** use the tool budget, but every retry must fit in the turn deadline. Neither kind can loop forever, so a failure cannot multiply cost.
2. **Degrade, don't crash.** When a limit is reached, the agent answers with what it has and names the gaps. A turn fails only when nothing at all succeeded, and even then the user gets a clear message with the turn ID and the conversation continues.

### 4.1 Data and SQL

| Failure | Detected by | Reaction |
|---|---|---|
| SQL syntax error, unknown column | BigQuery dry-run (free) | error text goes back to the model, which rewrites the query; counts against the tool budget |
| Query rejected by the validator | AST validator | a neutral explanation goes back to the model, which re-plans (for example, aggregates instead of a customer ID with demographics); a repeated attempt to reach a PII column stops the turn and records a security event |
| Brand outside the executive's scope named in the SQL | brand check in the validator | the query is not run; the user is told which brand they have no access to (the views would return no rows anyway) |
| Empty result | code | a hint goes back to the model (check the spelling of values and the date range) together with a tool to look up real column values; if still empty, the answer says honestly that there is no data |
| More than 200 rows | row limit in code | if the user asked for that level of detail, the answer says so and offers an aggregated version; if the model chose it, the model aggregates by itself |
| Query would scan more than the bytes cap | dry-run + `maximum_bytes_billed` | not run, no retry; the answer says the request is too large and suggests narrowing it |
| The same SQL proposed twice in one turn | normalised-SQL check | not run again; the earlier result or error is returned with "change the approach"; counts against the budget |
| BigQuery timeout or 5xx | BigQuery client | retried with backoff within the deadline; then "the data source is unavailable right now", and the turn ends gracefully |

### 4.2 Models

| Failure | Detected by | Reaction |
|---|---|---|
| Per-minute rate limit (429), 5xx, timeout | model wrapper | up to 2 retries with exponential backoff and jitter, honouring `retry-after`; a "waiting" note in the progress stream; no retry if the remaining time is too short |
| Daily quota exhausted, region outage | model wrapper (error type) | no retry; straight to the next model in the fallback chain: same model in the other region → another Gemini Flash model → Claude via Vertex AI |
| Every model in the chain unavailable | model wrapper | a clear message naming the problem; logged separately from outages so quota and provider failures are distinguishable |
| A model keeps failing | circuit breaker in the wrapper | after a burst of failures the model is skipped for a few minutes, so users do not wait through timeouts on every call |
| Invalid structured output | schema validation | one repair round with the problems fed back; then a forced final call; then a "cannot answer" response that still lists what was found |
| Final answer contains a PII pattern | output guard | the answer is replaced by a short refusal and a security event is recorded |
| Embedding call fails | retrieval node | Golden Bucket search continues with full-text only |

### 4.3 Turn limits

| Situation | Reaction |
|---|---|
| Tool budget used up | no more queries; the model writes the final answer from the results it has and lists the gaps under limitations |
| Turn deadline approaching | at 20 s before the deadline no new query starts and the final answer is written; a timeout produces a partial answer, not an error |

### 4.4 Infrastructure

| Failure | Reaction |
|---|---|
| A region fails | the load balancer sends traffic to the other region; conversations continue there because their state is in Postgres |
| Database zone fails | Cloud SQL fails over to the standby automatically (about a minute); a turn in progress may fail with a clear message and is retried by the user; the conversation resumes from its last checkpoint |
| Database region fails | the cross-region replica is promoted (runbook); a few minutes of downtime |
| LangSmith unavailable | tracing is asynchronous and never blocks a turn; traces may be lost for that period, service logs are not |
| Pub/Sub or a background job fails | automatic retries; a final failure is logged with the event and the error and shows up in Datadog; users are never affected |
| Executive's OAuth token expires | the web app refreshes it; if refreshing fails, the user is asked to sign in again |
| Any unexpected exception | the turn wrapper returns a message with the turn ID; the conversation state stays consistent |

### 4.5 Keeping cost under control

- Routing, refusals, schema answers and report-library actions use the cheap model or no model at all.
- One shared tool budget per turn, which includes retries after errors.
- Dry-run before every query; a bytes cap on every query.
- Repeated identical queries are not executed again.
- History in prompts is limited to the last 6 questions and final answers; raw query results never carry over.
- Cloud Armor rate limits per user stop a script or a stuck client from generating cost.

**Prototype.** Every row of 4.1–4.3 is implemented, except the circuit breaker and region fallback (the prototype's chain is model → other Gemini model → Claude if a key is present). Section 4.4 is production design.

---

## 5. Observability

### 5.1 What is recorded

Every model call, tool call and graph node writes a trace record, and every turn ends with a summary record. All records carry the conversation (thread) ID, turn ID and executive ID, so one conversation can be reassembled end to end.

| Record | Contents |
|---|---|
| Model call | node, model, full prompt and response, tokens in and out, latency, attempt number, retries, whether a fallback model answered, persona version |
| Tool call | the SQL the model wrote and the SQL actually executed, bytes scanned, rows returned, outcome (success, validator rejection with its rule, empty, error type) |
| Routing | rewritten question, intent, the classifier's one-line reason, metric overrides |
| Retrieval | Golden Bucket trio IDs and their scores, or "no example" |
| Security event | kind (validator rule, PII in result or answer, malicious intent, access to another executive's thread), never the PII value itself |
| Turn summary | intent, outcome (answered, partial, refused, error), total latency, model and tool calls, tokens, estimated cost, whether the deadline or budget was hit |
| Feedback | rating and comment, linked to the turn |

BigQuery jobs carry the thread and turn IDs as labels, so their cost can be joined to the trace.

### 5.2 Agent metrics

Computed from the turn summaries and sent to Datadog as custom metrics; quality metrics come from LangSmith.

| Group | Metrics |
|---|---|
| Health | error rate by error type; partial-answer rate (budget or deadline); fallback rate; quota exhaustion events; p50 and p95 latency per intent; time to first progress message |
| Quality | online evaluation score; share of positive feedback; clarification rate; refusal rate; rephrase rate (the same question asked again); SQL corrections per answered turn; empty-result rate; share of questions that used a Golden Bucket example |
| Security | validator rejections by rule; PII scan hits; malicious-intent rate; attempts to open another executive's thread |
| Cost | tokens and dollars per turn and per executive; BigQuery bytes per turn |

### 5.3 Alerts

| Alert | Example threshold | Why |
|---|---|---|
| Error rate | above 5 % of turns over 15 minutes | something is broken |
| p95 latency of analysis turns | above 30 s over 30 minutes | the agreed target is missed |
| Fallback rate | above 20 % over 15 minutes | the primary model or region is degrading |
| Any PII hit in a result or an answer | every event, paged | a data-layer control failed; must be investigated the same day |
| Online evaluation score | 10 % below its 7-day average | quality regression, often after a persona, prompt or model change |
| Daily model cost | above budget | runaway usage |

### 5.4 Debugging one conversation

1. Start from the turn ID: every error message shows it, and every alert links to example turns.
2. Open the conversation in LangSmith: the rewritten question, the classifier's reason, the retrieved trios, every SQL query with its result or error, every retry and fallback, and the final answer, in order.
3. Follow the same IDs to the BigQuery job (labels) and to service logs in Datadog (shared trace ID).
4. To test a fix, replay the turn in staging from its checkpoint with the changed prompt or model, then add it to the evaluation set so the problem cannot come back unnoticed.

Traces contain business figures, so access is limited to the engineering group and retention is 14 days.

**Prototype.** All records above are written as JSON lines to `logs/traces.jsonl` (no external account needed). `python scripts/inspect_trace.py <thread_id>` prints one conversation in order; `python scripts/metrics.py --since <date>` prints the health, quality, security and cost metrics for a period. The [example run](../docs/example-run.md) includes real trace excerpts.

---

## 6. Quality assurance

### 6.1 Before deployment

| Layer | What it checks | How |
|---|---|---|
| Deterministic tests | the validator against attack queries, scoping, masking, ownership, the deletion flow, budgets and deadlines | pytest with fake models and a fake BigQuery; no cost; run on every pull request |
| Routing evaluation | that each message reaches the right branch, with special attention to legitimate questions refused by mistake | labelled messages incl. blunt wording, typos and follow-ups; precision and recall per intent, false-refusal rate |
| Security set | natural-language attacks: PII extraction, other brands, prompt injection, other executives' reports, deletion without confirmation | deterministic pass/fail checks; must be 100 % |
| Golden set (business) | the client's questions and the expected capabilities, for the CEO and for brand managers | reference SQL run live through the same scoping; key numbers compared with a tolerance; deterministic checks (definitions stated, date range, "no spike" when there is none); LLM judge |
| Multi-turn dialogues | follow-ups, clarifications, metric overrides kept across turns, reports built from the conversation, deletion | scripted conversations with assertions per turn |

**Does the report answer what the user meant?** Three checks together:

1. *Execution accuracy*: the numbers in the answer match the reference query, run live for the same executive, because data and relative dates ("last month") move.
2. *Structure*: every action item points to a finding, definitions and the date range are stated (checked in code).
3. *LLM judge* from another model family, with a rubric: answers the question that was asked, numbers match the query results, definitions stated, action items follow from the findings. Before the judge is trusted as a gate, it is calibrated against a sample labelled by people, and its agreement with them is measured.

### 6.2 Release gate

A pull request that changes code, prompts or the glossary is deployed only if:

- deterministic tests pass;
- the security set passes 100 %;
- the golden set scores no lower than the current production version, within a tolerance band (each case runs several times, because model output varies);
- the false-refusal rate does not increase.

### 6.3 After deployment

- LangSmith's online evaluator scores a sample of live turns with the same rubric; a drop raises an alert (§5.3).
- Executives rate answers (thumbs up or down with an optional comment).
- Well-rated conversations feed the Golden Bucket candidates (§3.1).

### 6.4 User experience

- **Before launch:** scripted multi-turn scenarios scored by a conversation-level judge (were follow-ups understood, were clarifications necessary), and think-aloud sessions with 3–5 executives.
- **In production:** implicit signals (rephrase rate, clarification rate, turns until an answer, sessions abandoned after an error, deletions cancelled, time to the first progress message, p95 latency) and explicit feedback.

### 6.5 Prototype results

All on the `free` profile (Gemini 3.5 Flash-Lite for every role), so answer quality is below what the production model would give.

| Check | Result |
|---|---|
| Offline tests | 395 passing |
| BigQuery tests (no model) | 107 passing |
| Routing evaluation | 38 labelled messages: 97 % correct, 0 false refusals; 20 further messages in plain language and as follow-ups: all routed correctly, 11/11 follow-ups rewritten with their context |
| Multi-turn dialogues | 12 scripted conversations, all passing (some after a re-run because of free-tier rate limits) |
| Golden set, 26 business cases per brand manager | 23/26 and 19/26 before a prompt fix for totals and shares; 8 of the 10 misses pass after it; 2 remain open |
| Independent review | no critical findings; safety, deletion, resilience and observability passed after the fixes |

---

## 7. Setup and example run

The prototype is a command-line chat agent. Full instructions are in the [README](../README.md); in short:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
gcloud auth application-default login
cp .env.example .env        # set GOOGLE_API_KEY, GOOGLE_CLOUD_PROJECT, LLM_PROFILE=free
python scripts/seed_executives.py          # CEO + two brand managers with non-overlapping brands
python -m retail_agent --exec brand_manager_2 --profile free
```

You need Python 3.11+, a Google Cloud project with BigQuery enabled (a scoped query bills about 40 MiB, inside the free 1 TiB per month) and a Gemini API key from Google AI Studio. The `free` profile runs every role on Flash-Lite so a full demo fits in a free key's daily quota.

**Example run.** An excerpt from a real session of a brand manager (full transcript with trace excerpts: [docs/example-run.md](../docs/example-run.md)):

```text
[brand_manager_2] > What was our total revenue last month, by product category?
... Understanding the question
... Querying data (1/6)
... Querying data (2/6)
Last month's revenue totaled $14,439.10 across 18 categories, dominated by
Outerwear & Coats ($4,530.16) and Jeans ($4,507.80).
...
Definitions used
 • Analysis period: last full calendar month (2026-09-01 to 2026-09-30)
 • Revenue: sum of sale_price over counted items (status NOT IN ('Cancelled', 'Returned'))

[brand_manager_2] > Delete all reports that mention Tommy Hilfiger.
... Searching your reports
1 report will be permanently deleted. Type `confirm` to proceed.
- Tommy Hilfiger Revenue by Category (Year-to-Date 2026) (2026-10-25)
[brand_manager_2] > confirm
Deleted 1 report.

[brand_manager_2] > Show me the email addresses and phone numbers of our top 5 customers.
I can't help with that request.

[brand_manager_2] > What was Calvin Klein's revenue this year?
You do not have access to Calvin Klein data; your assigned brands are Carhartt,
Diesel, and Tommy Hilfiger.
```

The CEO asking the same first question gets $348,843.10 instead of $14,439.10: the same agent, scoped to all brands. In the first turn, BigQuery rejected the model's first query and the model corrected it; the trace shows both attempts.
