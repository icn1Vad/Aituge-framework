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

Before delegating a script or storyboard revision, call `list_active_agents`. Reuse a matching specialist by passing only its real `instance_id`. If no matching instance exists, create one by passing only its `agent_id`. Never invent an instance identifier or intentionally pass both fields.

When the current operation requires both the script and structured storyboard to change, always execute the two revisions serially:

1. Delegate only to `media-writer-agent` first. Do not request a Storyboard revision or issue either Workspace write in the same tool-call batch.
2. Wait for the Writer delegation result to confirm that `write_script_workspace` succeeded.
3. Only after that success, delegate to `media-storyboard-agent`.
4. Require Storyboard to read the newly saved script before it saves all four structured storyboard fields as one complete replacement.
5. Finish only after both delegations and both Workspace writes have succeeded.

Do not use direct MainAgent Workspace writes for a revision that changes both values, and never schedule the Writer and Storyboard delegations in parallel.

Preserve unaffected Workspace content and do not force both specialists to run for every interaction.
