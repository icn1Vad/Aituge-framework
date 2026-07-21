---
name: contract-ir-definitions-basics
description: Extract contract definitions, dates, and amounts as one validated Contract IR fragment.
tags: [contract, ir, definitions, dates, amounts]
---

# Contract IR definitions and basic facts

Read every current-contract block with `contract_get_blocks` before producing the result. Use the
`review_id` and `document_id` from `task_input`, request a limit large enough to cover the complete
document, and use only those blocks. Do not stop after finding representative examples.

Extract every explicitly supported:

- defined term and its contractual meaning;
- material date, period, deadline, start date, end date, renewal date, and notice period;
- amount, price, percentage, rate, cap, threshold, deposit, penalty amount, and calculation base.

Return only the three arrays in the registered fragment schema. Do not return any other Contract IR
category. Every entry must contain one or more exact source anchors from the returned blocks. Offsets
use Python Unicode code points and the left-closed, right-open interval `[char_start, char_end)`.

Use stable, category-prefixed `item_id` values for dates and amounts. Do not omit facts to make the
response shorter, but do not repeat the same fact with equivalent wording. Return one JSON object only,
without Markdown fences, reasoning, progress narration, or commentary.
