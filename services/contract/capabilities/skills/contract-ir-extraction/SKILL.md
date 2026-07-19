---
name: contract-ir-extraction
description: Build the semantic Contract IR from stable blocks and resolved parties.
tags: [contract, ir, extraction]
---

# Contract IR extraction

Start from `contract_get_ir`, preserve its document, clauses, anchors, and identifiers, then add resolved
parties and semantic items. Every important definition, right, obligation, prohibition, term, date, and
amount must reference real source anchors. Do not create block IDs, clauses, or source text.
