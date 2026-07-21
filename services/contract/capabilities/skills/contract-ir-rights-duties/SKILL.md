---
name: contract-ir-rights-duties
description: Extract contract rights, obligations, and prohibitions as one validated Contract IR fragment.
tags: [contract, ir, rights, obligations, prohibitions]
---

# Contract IR rights, obligations, and prohibitions

Read every current-contract block with `contract_get_blocks` before producing the result. Use the
`review_id` and `document_id` from `task_input`, request a limit large enough to cover the complete
document, and use only those blocks. Preserve multi-block rules as one semantic item when the blocks
together express one right, duty, or prohibition.

Extract comprehensively:

- each party's powers, entitlements, options, approvals, remedies, and discretions;
- each party's affirmative duties, conditions, cooperation duties, and continuing obligations;
- each expressly forbidden act, restriction, and negative covenant.

Return only `rights`, `obligations`, and `prohibitions`. A clause may legitimately have more than one
legal role, but do not duplicate equivalent wording inside the same category. Every item must contain
one or more exact source anchors from the returned blocks. Offsets use Python Unicode code points and
the left-closed, right-open interval `[char_start, char_end)`.

Use stable category-prefixed `item_id` values. Do not impose a top-N limit. Return one JSON object only,
without Markdown fences, reasoning, progress narration, or commentary.
