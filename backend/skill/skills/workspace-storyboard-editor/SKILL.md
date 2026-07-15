---
name: workspace-storyboard-editor
description: Analyze or edit an executable storyboard grounded in the latest Workspace script.
tags: [media, storyboard, workspace, editor]
---

# Workspace Storyboard Editor

## Professional method

- Use the latest saved script as the narration and factual source of truth.
- Cover the full narration timeline without unexplained gaps.
- Number shots and include time range, framing, subject/action, camera movement, matching voice-over, on-screen text, asset or location need, transition, and production notes.
- Keep screen direction, character appearance, wardrobe, props, lighting, location, and tempo continuous.
- Prefer practical filming or AI-assisted generation instructions over abstract descriptions.
- Do not silently rewrite factual claims or change the script's central angle.
- For a bounded edit, preserve unaffected shots; for a rewrite, return a complete coherent shot list.

## Structured storyboard contract

Build exactly one JSON object with these four top-level keys:

- `storyboard`
- `storyboard_plan`
- `visual_direction`
- `warnings`

Every `storyboard` item must contain all of these keys:

- `time`: one explicit time range such as `0-5s`
- `scene`: the location, subject, and composition
- `shot`: framing and camera treatment
- `action`: executable subject action and visual change
- `voiceover`: the exact matching narration segment
- `subtitle_focus`: the concise on-screen text focus
- `visual_prompt`: a concise filming or generation prompt

Use a JSON object for `storyboard_plan`, a string or string array for `visual_direction`, and a string array for `warnings`.

Do not produce Markdown tables, headings, code fences, separators, commentary, or any text outside this JSON object. Keep field values concise so the complete payload can be saved reliably.

## Workspace output protocol

- In delegate or edit mode, first call `read_script_workspace`.
- Serialize the complete structured storyboard object as valid JSON and pass that full JSON text to `write_storyboard_workspace`.
- Save a complete replacement object, never a diff or partial shot list.
- Never claim that the storyboard was saved unless `write_storyboard_workspace` succeeds.
- For a bounded revision, parse the latest saved storyboard JSON, change only the requested shots, preserve unaffected shots and top-level metadata, then save the complete replacement JSON.
- In consult mode, provide advice only and do not call a write tool.
