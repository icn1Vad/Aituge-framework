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

`evidence` MUST always be a JSON array. Never return a string, object with `quotes`, nested `source` object, or `null`. Each evidence item must use exactly this flat shape:

```json
{
  "file_id": "doc_xxx",
  "file_name": "report.docx",
  "section": "optional section",
  "page": null,
  "paragraph_index": 12,
  "table_index": null,
  "char_start": 100,
  "char_end": 180,
  "quote": "exact supporting source text"
}
```

Omit unavailable optional location keys. For a missing field return `"evidence": []`. Return every requested `field_id` exactly once. Do not call the Python interpreter to construct the final response; emit the JSON object directly.
