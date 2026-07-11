---
name: media-script-research
description: Build the evidence and angle package used by the media script Pipeline.
tags: [media, script, research, pipeline]
---

# Media Script Research

## Responsibility
Research one script task. Do not write the final voiceover or storyboard.

The input contains a topic card, source brief, material and comments, persona context, master-library recommendations, and user constraints. Treat comments as audience language, not verified facts. Use web search only when current policy, dates, institutions, public events, rankings, or other time-sensitive claims need verification.

## Rules
- Preserve the topic card's concrete entities and source boundaries.
- Separate verified facts, material claims, audience questions, and unresolved risks.
- Prefer official or authoritative sources for policy and public-service claims.
- Do not force a weak topic into enrollment, certificates, courses, purchases, or private-message conversion.
- Recommend one main angle that the writer can execute in a short video.
- Use at most two web-search calls. Each call must use one focused query; stop searching once the key current claim is verified.

## Output
Return one JSON object with exactly these fields:
`topic_summary`, `key_facts`, `usable_materials`, `audience_questions`, `controversies`, `source_evidence`, `recommended_angle`, `risks`.

Each `key_facts` item should contain `claim`, `source`, and `confidence`. Each `source_evidence` item should contain `title`, `url`, `source_name`, and `supports` when available.
Each `controversies` item may be a string or an object containing only `issue`, `detail`, and `severity`.
`recommended_angle` may be a string or an object containing only `main_angle`, `rationale`, `suggested_structure`, `persona_fit`, and `formula_suggestion`.
Each `risks` item may be a string or an object containing only `risk`, `detail`, and `mitigation`.
Return raw JSON only. Do not wrap the object in Markdown or add prose outside it.
