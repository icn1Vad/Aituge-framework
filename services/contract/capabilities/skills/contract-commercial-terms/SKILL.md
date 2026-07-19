---
name: contract-commercial-terms
description: Review payment, delivery, acceptance, and other commercial terms neutrally.
tags: [contract, payment, delivery, acceptance]
---

# Commercial terms review

The stage input contains task identity only. Call `contract_get_ir` with its exact `review_id` and
`document_id`, then use `contract_get_blocks` or `contract_get_clause_context` for text that needs more
context. Do not review any other document.

Review payment amount and triggers, due dates, invoicing prerequisites, tax allocation, delivery scope and
standards, transfer of risk, acceptance procedure, deemed acceptance, rejection and cure, and dependencies
between these terms. Identify a Finding only when the selected side faces a concrete commercial risk,
uncertain trigger, unilateral control, infeasible dependency, or material absence. Use `PAYMENT`,
`DELIVERY`, or `ACCEPTANCE` when applicable. Stay on the selected side and apply the `NEUTRAL` attitude.

For quoted or contextual support, use a real IR source anchor or verified Block range. Prefer a complete
existing anchor over a guessed narrow range. `char_start` and `char_end` are Python Unicode code-point
indices relative to that Block and use `[start,end)`. The model may omit `quoted_text` and
`quoted_text_hash`; Contract Python creates them deterministically. Never invent a hash or source text.

Use `ABSENCE` only for a genuine missing commercial protection after checking the entire Contract IR. Set
`checked_scope` to `ENTIRE_CONTRACT` or a precise commercial-term scope and explain the deterministic search
in `verification_note`. Keep IDs unique and keep Finding–Evidence references complete. Return empty arrays
when there is no material issue.

Return at most four highest-materiality findings and merge findings with the same cause. Keep each
free-text field to one or two concise sentences. For every `ABSENCE` candidate, explicitly set
`block_id`, `page_number`, `char_start`, `char_end`, `quoted_text`, and `quoted_text_hash` to null. Start
the final answer immediately with `{`; do not narrate analysis or use a Markdown fence.
