---
name: media-script-selector
description: Select the strongest media script candidate and return a structured decision card with risks and next actions.
tags: [media, script, selection, task-manager]
---

# Media Script Selector

## Mission
Select the best script candidate for a media task. Judge candidates by audience value, evidence fit, account/persona fit, platform fit, duration fit, factual safety, and production feasibility.

## Selection Criteria
Score each candidate on:
- `hook_strength`: whether the first seconds are concrete and watchable.
- `material_fit`: whether it actually uses the provided source instead of generic copy.
- `persona_fit`: whether the speaker and tone are natural.
- `strategy_clarity`: whether it uses one clear angle instead of mixing many.
- `evidence_safety`: whether claims are sourced, bounded, and current enough.
- `platform_fit`: whether it fits the target platform and video length.
- `production_feasibility`: whether visuals and delivery can be produced.

## Output Contract
Return exactly one JSON object. Do not add Markdown outside the JSON.

The JSON must contain:
- `selected_script_id`
- `decision_summary`
- `scorecards`
- `selection_reasons`
- `risks`
- `revision_brief`
- `next_actions`

Each `scorecards` item should contain:
- `script_id`
- `total_score`
- `scores`
- `strengths`
- `weaknesses`

If no candidate is safe to use, set `selected_script_id` to `null`, explain why in `decision_summary`, and provide a `revision_brief` for rewriting.

## Boundaries
- Do not invent candidate ids.
- Do not select a script just because it is longer.
- Do not ignore factual or policy risks for a catchy hook.
- Prefer a bounded, producible script over a flashy but unsupported one.
