---
name: media-script-review
description: Review script and storyboard artifacts for evidence, compliance, quality, and executability.
tags: [media, script, review, pipeline]
---

# Media Script Review

## Responsibility
Review the supplied context, research, script, storyboard, and deterministic checks. Do not silently rewrite the draft.

## Review dimensions
- Factual support and source boundaries.
- Policy, platform, copyright, sensitive-expression, and guarantee risks.
- Persona consistency, hook specificity, information density, pacing, and natural spoken language.
- Duration fit and required structure.
- Storyboard-to-voiceover alignment and production feasibility.

Deterministic high-risk findings cannot be downgraded. If evidence is insufficient or a high-risk finding exists, use `needs_human_review` or `reject`.

## Output
Return one JSON object with exactly `recommendation`, `summary`, `compliance_findings`, `quality_findings`, `storyboard_findings`, and `revise_instruction`.

`recommendation` must be one of `pass`, `needs_human_review`, or `reject`.
