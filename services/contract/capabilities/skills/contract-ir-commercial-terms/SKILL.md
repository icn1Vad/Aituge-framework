---
name: contract-ir-commercial-terms
description: Extract payment, delivery, and acceptance terms as one validated Contract IR fragment.
tags: [contract, ir, payment, delivery, acceptance]
---

# Contract IR commercial terms

Read every current-contract block with `contract_get_blocks` before producing the result. Use the
`review_id` and `document_id` from `task_input`, request a limit large enough to cover the complete
document, and use only those blocks. Combine related blocks when they jointly define one commercial
mechanism.

Extract comprehensively:

- price, invoicing, payment timing, payment conditions, settlement, deductions, and tax treatment;
- deliverables, service scope, delivery milestones, transfer method, and delivery dependencies;
- acceptance subject, standard, procedure, deadline, deemed acceptance, rejection, and correction.

Return only `payment_terms`, `delivery_terms`, and `acceptance_terms`. Do not return general obligations
or liabilities merely because they mention money or performance. Every item must contain one or more
exact source anchors from the returned blocks. Offsets use Python Unicode code points and the
left-closed, right-open interval `[char_start, char_end)`.

Use stable category-prefixed `item_id` values. Do not impose a top-N limit. Return one JSON object only,
without Markdown fences, reasoning, progress narration, or commentary.
