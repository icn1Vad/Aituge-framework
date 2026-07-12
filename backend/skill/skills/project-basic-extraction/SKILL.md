---
name: project-basic-extraction
description: Extract SmartAutoFill project basics and project classification fields.
tags: [smart-fill, feasibility, project]
---

# Project And Classification Extraction

Extract only the project and classification fields listed in the current item. Prefer cover-page facts, investment plan facts, explicit project classification statements, and direct yes/no disclosures. Do not turn absence of a statement into `false`; use `missing` or `needs_review`. Keep monetary values and currency separate when the field contract requires it. Follow the shared Smart Fill, citation, and structured-output skills.

Do not infer `parent_project` from the existence of a feasibility report. Do not infer `filing_type=不涉及` merely because the document describes an internal approval path; both require an explicit disclosure.

For `has_industry_market_analysis`, count substantive industry, market, demand, customer, competition, or business-environment analysis wherever it appears. Do not return `否` merely because there is no standalone chapter titled 行业市场分析. For `managing_unit`, return the immediate supervising unit rather than the ultimate group when the document discloses an affiliation chain.
