---
name: contract-rights-obligations
description: Review rights, obligations, prohibitions, and imbalance from the selected party perspective.
tags: [contract, rights, obligations]
---

# Rights and obligations review

The stage input contains task identity, not the contract body. Call `contract_get_ir` with the exact
`review_id` and `document_id` before reviewing. Use `contract_get_blocks` or
`contract_get_clause_context` when an IR item needs surrounding text. Never use another document.

Review only these concerns:

- each side's express rights, obligations, and prohibitions;
- one-sided discretion, control, suspension, or approval rights;
- obligations that are unperformable, unbounded, or missing a reciprocal right;
- material imbalance from the selected `PARTY_A` or `PARTY_B` perspective.

Apply `NEUTRAL`: explain concrete impact on `our_party`, but do not assume strong or weak bargaining
leverage. Copy `perspective`, `our_party`, and `counterparty` from the resolved IR. Use category
`RIGHTS_OBLIGATIONS_IMBALANCE` unless another registered category is clearly more precise. Return no
Finding for harmless style, ordinary balanced obligations, or unsupported speculation.

Every Finding must have at least one Evidence Candidate. Prefer copying an existing IR source anchor's
`block_id`, `page_number`, `char_start`, and `char_end`. If a narrower range cannot be counted exactly as
Unicode code points, cite the complete real anchor range instead of guessing. For `TEXT_QUOTE` or
`CONTEXT`, the model may omit `quoted_text` and `quoted_text_hash`; Contract Python derives both from the
validated Block. Never invent a hash. Keep Finding and Evidence IDs unique and make every
`finding.evidence_ids` entry point to a returned candidate.
