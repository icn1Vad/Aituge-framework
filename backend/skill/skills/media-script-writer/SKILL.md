---
name: media-script-writer
description: Write one complete Douyin-ready script from the provided media context.
tags: [media, script, writer, pipeline]
---

# Media Script Writer

## Responsibility
Write one complete, speakable short-video script using only the provided Context Gateway artifact. Do not invent missing facts and do not perform research or call external data sources.

## Required behavior
- Follow the selected persona, strategy, template, script type, and risk rules when present.
- Treat topic cards, material text, comments, persona data, and master-library settings as the complete available evidence.
- If required facts are absent, use cautious wording and record the limitation in `hermes_agent_result.risks`.
- Use one main angle and a concrete three-second hook.
- Respect the requested duration and give a realistic target character range.
- Keep comments as audience language unless separately verified.
- State weak evidence or a weak business bridge in `hermes_agent_result.risks`.
- Do not promise guaranteed outcomes or imply unsupported official endorsement.

## Output
Return one JSON object containing only:
- `final_script`
- `readable_script`
- `hermes_agent_result`

`final_script` must contain `topic_name`, `persona_name`, `video_goal`, `platform`, `duration_seconds`, `duration_reason`, `target_char_range`, `hook_3s`, `structure`, `voiceover`, `subtitle_points`, `visual_direction`, `material_bridge`, and `master_library_usage`.

`hermes_agent_result` must contain `status`, `editor_summary`, `why_this_angle`, `risks`, `parse_notes`, and `master_library_usage`.
