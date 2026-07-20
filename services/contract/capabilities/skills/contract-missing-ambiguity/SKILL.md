---
name: contract-missing-ambiguity
description: Detect material missing clauses, ambiguity, and internal contradictions.
tags: [contract, missing-clause, ambiguity]
---

# Missing and ambiguous clause review

The stage input contains task identity only. Call `contract_get_ir` for the exact `review_id` and
`document_id`; use `contract_get_blocks` and `contract_get_clause_context` to check the whole relevant
scope. Never infer absence from a partial excerpt.

Separate three cases:

- `MISSING_CLAUSE`: a material protection or obligation is absent from the checked scope;
- `AMBIGUITY`: real wording permits materially different interpretations;
- `INTERNAL_CONFLICT`: two or more real clauses impose inconsistent outcomes.

Suppress grammar, style, formatting, and harmless incompleteness. Apply the selected party perspective and
fixed `NEUTRAL` attitude to explain the concrete effect and balanced correction.

For `MISSING_CLAUSE`, use an `ABSENCE` Evidence Candidate. Do not provide Block, page, character range,
quote, or hash. Set `checked_scope` to `ENTIRE_CONTRACT` or a precise clause category and make
`verification_note` state what categories and IR fields were checked and what was not found. Never
manufacture a quote for missing text.

For ambiguity or conflict, use real `TEXT_QUOTE` or `CONTEXT` candidates. Copy existing IR anchors and use
their exact Block-relative `[char_start,char_end)` ranges. The model may omit `quoted_text` and
`quoted_text_hash`; Contract Python derives them. Keep IDs unique and references complete. Return empty
arrays if no material issue is supported.

Return at most four highest-materiality findings and merge missing protections that have the same cause.
Keep each free-text field to one or two concise sentences. In every `ABSENCE` candidate, explicitly set
`block_id`, `page_number`, `char_start`, `char_end`, `quoted_text`, and `quoted_text_hash` to null. Start
the final answer immediately with `{`; do not narrate analysis or use a Markdown fence.
