---
name: media-storyboard
description: Convert a media script draft into an executable short-video storyboard.
tags: [media, script, storyboard, pipeline]
---

# Media Storyboard

## Responsibility
Convert the supplied voiceover into executable shots. Do not rewrite factual claims or change the script's main angle.

## Rules
- Cover the full narration timeline without unexplained gaps.
- Keep each shot practical for filming or AI-assisted generation.
- Keep persona, character appearance, location, and visual style continuous.
- Bind every shot to a narration or subtitle segment.
- Avoid impossible camera movement, excessive scene changes, or visuals that imply unverified facts.

## Output
Return one JSON object with exactly `storyboard`, `storyboard_plan`, `visual_direction`, and `warnings`.

Each `storyboard` item should contain `time`, `scene`, `shot`, `action`, `voiceover`, `subtitle_focus`, and `visual_prompt`.
