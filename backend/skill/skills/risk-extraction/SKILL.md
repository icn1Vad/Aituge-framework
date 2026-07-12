---
name: risk-extraction
description: Extract all risk and non-financial indicator rows without collapsing them.
tags: [smart-fill, feasibility, risk, indicators]
---

# Risk And Non-Financial Indicator Extraction

Extract only the risk and non-financial fields listed in the current item. Return every distinct disclosed risk with its own name, level when explicit, control measure, and evidence. Do not keep only the first risk. Return non-financial indicators as separate rows with their description, completion time, and quantity when disclosed. Follow the shared Smart Fill, citation, and structured-output skills.

Treat operational quantities, production or supply volumes, project counts, commissioning dates, capacity, safety performance, and other measurable operating achievements as candidate non-financial indicators. Do not leave the table empty when the document explicitly discloses such indicators.
