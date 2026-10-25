# Requirements Clarifications

Questions sent to the client (OpsFleet) before design and implementation, with the client's answers and the resulting design decisions.

- **Format:** written Q&A with the client
- **Date:** 2026-10-22
- **Answered by:** the client's project manager (questions 1–4 and the assumptions review) and AI Team Lead (question 5)

Each question lists what I proposed, the client's answer, and the resulting design decision. The decisions describe the final design of the prototype; details are in [design-decisions.md](design-decisions.md) (DD). Where the design changed after the answers, this is noted.

---

## 1. Data access model

**Question.** By "users" I mean the executives using the system, not customers in the `users` table. How is an executive "related" to products: by brand, category, department? Are there global roles, e.g. does the CEO see everything? Does the scope also restrict customer-side data such as regions?

**Proposed.** Role + brand/category entitlements, enforced deterministically in the query layer, never by the LLM.

**Answer.**
- Each executive manages one or more **brands** and can analyze data only for those brands.
- Customer and order data is also scoped: only purchases of in-scope brands.
- The **CEO sees everything**.
- There is no pre-built mapping in the dataset; the structure (config table, access manifest) is up to me.
- The proposed approach is confirmed as the right direction.

**Design decisions.**
- Access manifest: `executive → [brands]`, with an explicit "sees everything" flag for the CEO instead of a magic brand value (DD §7).
- The base fact table is `order_items` filtered by brand. `orders` and `users` are reached only through scoped items, because a single order can contain items from several brands and order-level fields (`num_of_item`, order status) would leak other brands' data.
- "Total spend" and "top customers" for an executive mean spend on **their brands only**; the same applies to every customer and order metric (DD §9).
- Scoping is injected in code: the model writes SQL against logical table names, and code prepends brand-scoped, PII-free CTEs. The model is never the control that enforces access (DD §8.1).

---

## 2. PII

**Question.** I'd classify first_name, last_name, email, street_address, postal_code and lat/long as PII, and keep state/city/country, age and gender as analytical dimensions. Is it acceptable for raw PII to reach the LLM provider at all? For "top customers", is a `user_id` fine?

**Proposed.** PII columns are blocked at the data layer, so the model never sees them; customers are shown by ID only.

**Answer.**
- The classification is correct.
- **Never in output and never sent to the LLM:** first and last name, email, street address, postal code, exact coordinates.
- **Allowed as analytical dimensions, aggregates only:** city, state, country, age, gender, traffic source.
- **PII must be blocked before data is sent to the model.** Masking only in the final answer is not sufficient.
- Referencing customers by `user_id` or a stable pseudonymous ID is fine, so multi-turn follow-ups keep working.

**Design decisions.**
- The schema exposed to the model contains no PII columns; `user_geom` is treated as exact coordinates (DD §8.1).
- An AST-based SQL validator rejects any reference to PII columns, including in `WHERE`, `JOIN` or functions, since filtering on a column can also leak it (DD §8.2).
- Query results are scanned for PII patterns before reaching the LLM, as defense in depth (DD §8.3).
- Demographic dimensions are used in aggregates only: a deterministic rule blocks returning demographics together with a customer ID. A minimum group size is part of the production design (DD §8.4).
- Emails, phone numbers and card numbers typed by the user are masked before any LLM call and before logging (DD §8.5).

---

## 3. Metric definitions

**Question.** Is there a source of truth for business KPIs (revenue, churn, top customers), or should I define and document them?

**Proposed.** A small glossary; the agent always states which definition it used.

**Answer.**
- There is no company-wide source of truth: define and document them myself.
- Expected defaults:
  - **Revenue** excludes cancelled and returned items.
  - **Churn** = no purchase in 90 days.
- The agent must always state which definition it used.
- **Users must be able to override a definition** (e.g. "use a 60-day churn window").

**Design decisions.**
- A glossary, versioned with the code, forms the semantic layer (DD §9).
- Metrics are relative to the executive's brands, e.g. churn means no purchase of in-scope brands in 90 days (DD §9).
- Session-level overrides apply on top of glossary defaults and are echoed back in the answer.

---

## 4. Saved Reports

**Question.** Are reports private to their author or visible to others with the same data scope? Is a report saved only on explicit request? In "reports mentioning Client X", is "client" a customer, a brand, or any free-text mention?

**Proposed.** Private; saved on explicit request; matching on entities plus text.

**Answer.**
- Reports are **private to their author**.
- Reports are saved **only on explicit user request**.
- thelook has no B2B clients, so "client" means **any entity named in a report**: brand, category, or a free-text mention.

**Design decisions.**
- Each report stores its owner, its conversation, the named entities and searchable full text (DD §11.1).
- "Delete reports mentioning X" combines a model selection with a deterministic text match, so a brand mentioned anywhere in a report is found. The exact list is shown before anything is deleted (DD §11.2–11.3).
- Ownership checks happen in code on every read and delete.

---

## 5. Persona vs. user preferences

**Question.** When the CEO changes the weekly tone and a user has learned preferences (tables vs. bullets, depth), which one wins? Who edits the persona, and is an approval or preview step needed?

**Proposed.** Persona controls tone and style, user preferences control format and depth; persona changes are versioned with preview and rollback.

**Answer.**
- The project manager confirmed the split (persona = tone and style, preferences = format and depth) and passed the remaining questions to the AI Team Lead.
- AI Team Lead: **learned user preferences always win** on conflict.
- **The CEO changes the tone directly.** No preview or approval is needed.

**Design decisions.**
- Prompt precedence: **safety rules → user preferences → persona**. Safety rules cannot be overridden by either layer.
- Prototype: the persona is a text file read at the start of each conversation and kept for that conversation, so the tone does not change mid-conversation and no redeployment is needed (DD §13). An earlier version reloaded it on every request.
- Production design: external versioned storage, automatic validation that persona text changes only tone and style (never safety rules or data access), history and rollback. No approval flow, as agreed.

---

## Assumptions review

| Assumption | Status |
|---|---|
| **Deletion:** strict confirmation listing the exact reports, plus soft delete with a restore window and audit log | **Corrected.** Confirmation listing exact reports is expected, but deletion must be a **hard delete with no restore path**. |
| **User memory:** preferences only, never query results; visible and deletable by the user | Confirmed. Design only in the prototype (DD §13). |
| **Golden Bucket:** promotion to "golden" only after analyst review; small synthetic seed set in the prototype | Confirmed. The prototype later kept only a seam for retrieval; the full design is documented separately (DD §12). |
| **Scale:** ~50–200 executives, low concurrency, <30s per analytical answer with progress shown, English | Confirmed |
| **Stack:** GCP-native; prototype runs with the reviewer's own Gemini key and GCP project | Confirmed |

**Deletion, final design.** The agent resolves the matching reports, shows the exact list, and executes only after explicit confirmation. Deletion is permanent. The audit log keeps metadata only (who, when, how many, report IDs) and never report content, so it cannot become a recovery path (DD §11).

---

## Client-provided example questions

The client shared these for testing. They form the core of the evaluation set.

1. List the top 10 best sellers by revenue and by number of customers.
2. Which regions or countries generate the most revenue?
3. Which product categories have seen the fastest growth in sales this year?
4. Why are the users in state X underspending? How does it compare to state Y?
5. Compare the performance of product X and Y. Why do they perform differently?
6. Why did our churn rate spike last month?
7. Create a report for Q1 including insights and action items for Q2.
8. What data is available, and what can we do with it?
9. Conduct RFM segmentation, provide summary statistics per group, and suggest action items for each.

**Notes for evaluation.**
- Every question runs within the executive's brand scope. The same question from two executives must give different answers, and the CEO gets the full picture.
- Time-relative questions ("this year", "last month", "Q1") are computed relative to the current date.
- On synthetic data, "why did churn spike" may have no spike. The agent must say so rather than invent causes.
- RFM segmentation was not in the original brief and needs multi-step analysis support.
