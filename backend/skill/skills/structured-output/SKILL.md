---
name: structured-output
description: Enforce the shared JSON envelope for SmartAutoFill parallel items.
tags: [smart-fill, json, schema]
---

# Structured Output

Return exactly one JSON object and no Markdown. The object must contain:

```json
{
  "group_id": "project | company | financial | risk | analysis",
  "fields": [],
  "missing_field_ids": [],
  "warnings": []
}
```

Each field result contains `field_id`, `status`, `value`, `evidence`, and `warnings`. Allowed status values are `filled`, `missing`, `conflict`, `invalid_format`, and `needs_review`.

