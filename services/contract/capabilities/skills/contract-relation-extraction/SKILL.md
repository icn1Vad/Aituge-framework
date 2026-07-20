---
name: contract-relation-extraction
description: Extract typed internal relationships between real contract clauses.
tags: [contract, relation, conflict]
---

# Contract relationship extraction

The stage input contains task identity only. Call `contract_get_ir` with the exact `review_id` and
`document_id`. Use only `clause_id` values present in that IR; use clause context when the relationship is
not clear from the clause alone.

Create only these internal relationship types:

- `SUPPORTS`: one clause supplies a condition or detail for another;
- `CONFLICTS`: clauses prescribe incompatible outcomes;
- `DEPENDS_ON`: performance or effect depends on another clause;
- `OVERRIDES`: the text expressly gives one clause priority over another.

Do not infer a relationship merely because clauses share a topic. Explain the textual basis. These
`internal_relationships` are Python-internal aids and never populate the schema 1.0 public
`relationships` array.

Return a Finding only when the relationship creates a material risk for the selected party, normally
`INTERNAL_CONFLICT` or `AMBIGUITY`. Such a Finding must cite real candidates for every clause needed to
support it. Copy exact IR anchors and their Block-relative `[char_start,char_end)` ranges; prefer full
anchors over guessed substrings. The model may omit `quoted_text` and `quoted_text_hash`, which Contract
Python derives deterministically. Keep IDs unique and Finding–Evidence references complete. Stay neutral.

Return at most four highest-materiality findings and merge findings with the same cause. Keep each
free-text field to one or two concise sentences. If an `ABSENCE` candidate is necessary, set `block_id`,
`page_number`, `char_start`, `char_end`, `quoted_text`, and `quoted_text_hash` to null. Start the final
answer immediately with `{`; do not narrate analysis or use a Markdown fence.
