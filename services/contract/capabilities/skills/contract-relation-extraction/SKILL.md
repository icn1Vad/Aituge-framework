---
name: contract-relation-extraction
description: Extract typed internal relationships between real contract clauses.
tags: [contract, relation, conflict]
---

# Contract relationship extraction

Relate only existing clause IDs using SUPPORTS, CONFLICTS, DEPENDS_ON, or OVERRIDES. Relationships are an
internal aid for detecting contradictions and do not enter schema version 1.0's public relationships array.
If a relationship creates a material risk, return a source-grounded finding and evidence as well.
