---
name: contract-grounded-answer
description: Generate a contract report or answer with deterministic source citations.
tags: [contract, report, chat, grounded, citation]
---

# Contract grounded report and chat

Generate Chinese content from the completed contract review. This task is document-grounded content
generation, not a new legal review.

## Required workflow

1. Call `contract_get_review_result` with the task's `review_id` and `document_id`.
2. Use the returned contract profile, summary, findings and evidences as the authoritative source.
3. Do not introduce a risk, party, amount, date, clause or conclusion that is absent from that result.

## Mode behavior

### REPORT

Generate a concise report. Use Markdown headings and compact prose. Cover:

- transaction and party overview;
- important rights and obligations;
- high and medium risks, ordered by materiality;
- actionable revision priorities;
- a short conclusion.

Do not cite every sentence. Cite the most useful source phrase for claims that should support document
navigation.

### CHAT

Answer only the current `question`. Use `conversation_history` only to understand references in the
question; it is not an authoritative source. Prefer a direct answer followed by short supporting
points. If the completed review result does not contain enough information, say that it cannot be
determined from the current review result. Do not invent or perform a new review.

## Citation contract

For locatable `TEXT_QUOTE` or `CONTEXT` evidence, render the link exactly as:

```text
[short label](#docref-EVIDENCE_ID)
```

Add the same `evidence_id` and exact link label to `citations`. A source may be cited more than once.
Never cite `ABSENCE` evidence as a clickable link. You may describe a verified absence in ordinary
text.

The model must not output block IDs, offsets, hashes or quoted source text in `citations`; the finalizer
materializes those fields from the authoritative result.

Return only the registered JSON object. Copy the input `mode` exactly.
