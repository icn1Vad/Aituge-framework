---
name: target-company-extraction
description: Extract investor, target company, equity structure, and valuation fields.
tags: [smart-fill, feasibility, company, equity]
---

# Target Company Extraction

Extract only the company-scoped fields listed in the current item. Distinguish the investor, target company, shareholders, and superior management entities. Preserve every equity row and do not merge different shareholders. Keep pre-investment and post-investment capital separate. Manual attachment fields must remain empty. Follow the shared Smart Fill, citation, and structured-output skills.

For investor metrics, distinguish parent-company/current-level figures from consolidated figures and prefer the figure whose scope exactly matches the field. In equity rows, populate country, source, shareholder nature, and unified social credit code only when the supplied document explicitly discloses them; never infer them from a company name or ownership context.
