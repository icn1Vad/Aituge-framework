---
name: media-script-task-orchestration
description: Execute one formal media script Task through the required MainAgent Workspace flow.
tags: [main-agent, media, script, task-manager]
---

# Media Script Task Orchestration

You are executing the formal TaskManager Task supplied in `Formal Task Context`. The caller already created this Task. Do not create or track another Task.

## Shared rules

- Treat the Workspace as the source of truth.
- Treat Task Memory as persistent background. The current user instruction always takes priority.
- Read an available auxiliary skill before applying its domain rules.
- Every Workspace change must save a complete replacement value through the matching write tool.
- Do not claim success until all writes required by the current operation have succeeded.

## Generate operation

When the validated Task input has `operation: generate`, execute this order without skipping a stage:

1. Delegate to `media-writer-agent` with a complete instruction grounded in the validated Task input and Task Memory.
2. Require Writer to read the latest Workspace and save the complete script with `write_script_workspace`.
3. After Writer succeeds, delegate to `media-storyboard-agent`.
4. Require Storyboard to read the newly saved script and save the complete storyboard with `write_storyboard_workspace`.
5. Finish only after both Workspace values have been saved.

Do not replace these two specialist stages with a direct MainAgent write during initial generation.

## Interact operation

When the validated Task input has `operation: interact`, read `main-agent-orchestration` and choose its smallest useful path:

- answer directly for explanation only;
- directly save a small, explicit Workspace edit;
- consult a specialist for advice;
- delegate a substantial script or storyboard edit.

Preserve unaffected Workspace content and do not force both specialists to run for every interaction.
