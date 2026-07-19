---
name: contract-liability-termination
description: Review breach, liability, termination, confidentiality, IP, and dispute terms.
tags: [contract, liability, termination]
---

# Liability and termination review

The stage input contains task identity only. First call `contract_get_ir` with the exact `review_id` and
`document_id`. Fetch Blocks or clause context before relying on wording that is not complete in the IR.

Review breach triggers and remedies, liquidated damages, indemnities, direct and indirect loss, liability
caps and exclusions, unilateral termination, cure periods, post-termination duties, confidentiality,
intellectual property ownership and licences, governing law, venue, arbitration, and dispute procedure.
Classify with `BREACH`, `LIABILITY`, `TERMINATION`, `CONFIDENTIALITY`, `INTELLECTUAL_PROPERTY`, or
`DISPUTE_RESOLUTION`. Report only a material risk to the selected party and propose a balanced, performable
change under the fixed `NEUTRAL` attitude.

Every Finding needs real evidence. Copy an existing IR source anchor whenever possible. A quoted candidate
must use the real Block ID and exact `[char_start,char_end)` Unicode code-point range. Prefer the full anchor
range to a guessed substring. The model may omit `quoted_text` and `quoted_text_hash`; Contract Python reads
the Block and generates both. Never fabricate a hash, page, clause, or quote. Keep all IDs unique and all
Finding–Evidence references consistent. Return empty arrays when no material issue exists.
