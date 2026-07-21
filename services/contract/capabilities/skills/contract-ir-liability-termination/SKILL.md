---
name: contract-ir-liability-termination
description: Extract liability, breach, termination, and post-termination terms as one Contract IR fragment.
tags: [contract, ir, liability, breach, termination]
---

# Contract IR liability and termination

Read every current-contract block with `contract_get_blocks` before producing the result. Use the
`review_id` and `document_id` from `task_input`, request a limit large enough to cover the complete
document, and use only those blocks. A termination trigger list that spans multiple blocks must remain
one coherent semantic item with all supporting anchors.

Extract comprehensively:

- breach consequences, damages, indemnities, penalties, liability scope, exclusions, caps, and set-off;
- ordinary termination, immediate termination, rescission, expiration, renewal, notice, cure periods,
  triggering conditions, and post-termination duties.

Return only `liabilities` and `termination_terms`. Every item must contain one or more exact source
anchors from the returned blocks. Offsets use Python Unicode code points and the left-closed,
right-open interval `[char_start, char_end)`.

Use stable category-prefixed `item_id` values. Do not impose a top-N limit and do not split one trigger
list into artificial independent rights. Return one JSON object only, without Markdown fences,
reasoning, progress narration, or commentary.
