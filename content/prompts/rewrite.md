<!--
prompt: rewrite
role: cheap
placeholders: {{history}}, {{message}}
output: plain text, the standalone question only (no preamble, no quotes)
Skipped on the first turn of a conversation (no history).
-->
Rewrite the user's latest message as a standalone request that can be understood without the conversation. Use the recent turns only to resolve references ("it", "that state", "now for Y", "yes, do it", "save it"). Keep the user's intent, wording, numbers, periods and any definition changes exactly; do not add analysis, do not answer it, do not decide what kind of request it is. If the message is already standalone, return it unchanged. If the latest message answers a clarification question, merge the question and the answer into one request.

Recent turns:
{{history}}

Latest message:
{{message}}

Standalone request:
