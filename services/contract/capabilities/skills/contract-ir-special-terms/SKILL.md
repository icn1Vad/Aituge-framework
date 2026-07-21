---
name: contract-ir-special-terms
description: Extract confidentiality, intellectual-property, and dispute-resolution terms as one IR fragment.
tags: [contract, ir, confidentiality, intellectual-property, dispute]
---

# Contract IR special terms

Read every current-contract block with `contract_get_blocks` before producing the result. Use the
`review_id` and `document_id` from `task_input`, request a limit large enough to cover the complete
document, and use only those blocks. Inspect the entire document because these terms often appear near
the end, in attachments, or under general provisions.

Extract comprehensively:

- confidentiality scope, permitted disclosure, security, duration, return, destruction, and remedies;
- background and developed intellectual property, ownership, licences, use restrictions, warranties,
  infringement responsibility, and deliverable rights;
- governing law, negotiation, mediation, arbitration, litigation, jurisdiction, venue, and cost rules.

Return only `confidentiality_terms`, `intellectual_property_terms`, and `dispute_resolution`. Every item
must contain one or more exact source anchors from the returned blocks. Offsets use Python Unicode code
points and the left-closed, right-open interval `[char_start, char_end)`.

Use stable category-prefixed `item_id` values. Do not impose a top-N limit. Return one JSON object only,
without Markdown fences, reasoning, progress narration, or commentary.
