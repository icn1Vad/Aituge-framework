---
name: smart-fill-common
description: Shared guardrails for evidence-grounded SmartAutoFill extraction.
tags: [smart-fill, feasibility, shared]
---

# Smart Fill Common Rules

- Work only on the `group_id` and `field_ids` in the current item.
- Read `field_specs` for field type, options, table columns, and business meaning.
- Use the SmartAutoFill knowledgebase tools before filling fields: catalog the files, search or grep for relevant terms, then fetch surrounding text when needed.
- Only use files whose `document_id` appears in the global task input.
- Never return a field outside the current item whitelist.
- Preserve dynamic tables as arrays; never collapse multiple rows into one summary.
- Leave a value empty when the supplied materials do not support it.
- Distinguish an explicit negative value from a missing disclosure.
- Preserve numeric sign, unit, currency, percentage, and forecast year exactly.
- Do not infer confidence scores in this phase.
