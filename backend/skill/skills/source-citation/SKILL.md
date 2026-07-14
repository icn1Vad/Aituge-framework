---
name: source-citation
description: Return exact, field-scoped source evidence for SmartAutoFill results.
tags: [smart-fill, evidence, citation]
---

# Source Citation

For every filled field, return the strongest exact quote available. Include the source file id and any available file name, section, page, paragraph index, and character offsets. The quote must support the returned value directly. Do not cite a broad section merely because it discusses the same topic. If no supporting quote is available, leave the field missing or mark it for review.

Return evidence as a flat JSON array following the `structured-output` skill. Use `file_id` and `file_name`; do not use `source_file_id`, `source_file_name`, a nested `source` object, or a `quotes` collection.
