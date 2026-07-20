---
name: contract-ir-extraction
description: Build the semantic Contract IR from stable blocks and resolved parties.
tags: [contract, ir, extraction]
---

# Contract IR extraction

Read `contract_get_ir` and the source blocks, then return only the `semantic_ir` delta required by the
registered output schema. Extract definitions, rights, obligations, prohibitions, commercial terms,
liabilities, termination terms, dates, and amounts that are explicitly supported by the contract.

Do not return or rewrite `document`, `clauses`, `parties`, `our_party`, `counterparty`, `contract_type`, or
top-level `source_anchors`. Contract Python deterministically composes those trusted fields with this
semantic delta after validating the result.

Every semantic item must reference real source anchors from the current blocks. Offsets use Python Unicode
code points and the left-closed, right-open interval `[char_start, char_end)`. Verify every anchor against
the exact block text and length. Keep the response compact. Return one JSON object only, without Markdown
fences, reasoning, or commentary.
