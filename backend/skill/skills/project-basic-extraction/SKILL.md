---
name: project-basic-extraction
description: Extract SmartAutoFill project basics and project classification fields.
tags: [smart-fill, feasibility, project]
---

# Project And Classification Extraction

Extract only the project and classification fields listed in the current item. Prefer cover-page facts, investment plan facts, explicit project classification statements, and direct yes/no disclosures. Do not turn absence of a statement into `false`; use `missing` or `needs_review`. Keep monetary values and currency separate when the field contract requires it. Follow the shared Smart Fill, citation, and structured-output skills.
