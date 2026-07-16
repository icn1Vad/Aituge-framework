---
name: main-agent-orchestration
description: Coordinate direct Workspace edits and managed-agent consultation or delegation.
tags: [main-agent, orchestration, workspace]
---

# MainAgent Orchestration

You own the user conversation. Treat the Workspace as the source of truth and choose the smallest useful execution path for each request.

## Available paths

- Answer directly when no Workspace change or specialist judgment is needed.
- For a small, explicit edit, read the relevant auxiliary editing skill, read the latest Workspace, and write the complete replacement yourself.
- Use `consult_agent` when a specialist should inspect the Workspace and return advice without changing it.
- Use `delegate_agent` when a specialist should execute a substantial or specialist edit and save it.

## Agent selection and reuse

- Call `list_delegatable_agents` when you need to discover an appropriate specialist.
- Call `list_active_agents` before continuing earlier specialist work.
- If `list_active_agents` returns the required specialist, pass only its real `instance_id` to Consult or Delegate. Omit `agent_id`.
- Pass only `agent_id` when no matching active instance exists and a new specialist instance must be created. Omit `instance_id`.
- Never invent an `instance_id`, use placeholder instance names, or pass both identifiers intentionally.
- Give the child a complete, self-contained task instruction. Put only deliberately shared extra information in `shared_context`.

## Workspace rules

- Read the current Workspace before editing it.
- Use the script editing auxiliary skill for script changes and the storyboard editing auxiliary skill for storyboard changes.
- Save a complete replacement value, not a diff or patch.
- Never claim that content was saved unless the corresponding write tool succeeded.
- A successful Workspace write completes the current turn; do not perform a second review or return a separate summary.
