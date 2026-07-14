---
name: douyin-content-analysis
description: Analyze one pre-ranked Douyin content item using only supplied evidence.
---

# Douyin content analysis

Analyze exactly one item. The upstream service owns date filtering, deduplication, percentile scoring, ranking, and the best/worst label. Never change its rank.

Return one JSON object with exactly these fields:

- `content_id`
- `evidence_based_reason` (non-empty string array)
- `copy_and_angle_analysis` (string array)
- `comment_feedback_analysis` (string array)
- `trend_analysis` (string array)
- `reusable_lessons` (string array)
- `improvement_suggestions` (string array)
- `data_limitations` (string array)

Use only supplied evidence. Missing, negative, unavailable, or permission-blocked fields are limitations, not zeroes. Do not invent comments, trends, retention, copy, audience attributes, or causal claims.
