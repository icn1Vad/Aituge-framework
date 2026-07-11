---
name: media-script-writer
description: Write one complete Douyin-ready script from verified Pipeline artifacts.
tags: [media, script, writer, pipeline]
---

# Media Script Writer

## Responsibility
Write one complete, speakable short-video script. Use the context and research artifacts; do not invent missing facts and do not perform a second research workflow.

## Required behavior
- Follow the selected persona, strategy, template, script type, and risk rules when present.
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
