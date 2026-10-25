<!--
prompt: schema_answer
role: cheap
placeholders: {{system}}, {{catalog}}, {{derived_metrics}}, {{scope}}, {{question}}
output: markdown text shown to the user
-->
{{system}}

Answer the executive's question about what data is available, using only the catalog below. Explain in business terms: what each table holds and its main fields, the metrics that can be derived, and three to five example questions they could ask. Say that the data covers {{scope}}. Mention that personal customer details are not available by design and that customers are identified by ID only. Do not mention column names that are not in the catalog. Keep it under 250 words.

Catalog:
{{catalog}}

Derived metrics:
{{derived_metrics}}

Question:
{{question}}
