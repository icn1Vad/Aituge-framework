---
name: media-script-change-proposal
description: Convert one explicit script edit request into a bounded structured proposal for human confirmation.
tags: [media, script, proposal, task-manager]
---

# Media Script Change Proposal

Use the injected `Task Memory` as shared background when interpreting the request. The current explicit edit request always takes priority.

## Responsibility

Analyze the user's explicit edit request against the supplied current script and return a change proposal. Do not rewrite the script and do not apply changes.

## Boundaries

- Use only the supplied script, topic, persona, constraints, and bounded conversation context.
- Do not search, call tools, invent facts, or introduce unsupported policy claims.
- Do not expand the requested scope. List unrelated fields in `preserve_fields` when preservation matters.
- Do not claim that a proposal has been approved, saved, or applied.
- If the request is ambiguous enough to risk changing the wrong content, return `needs_clarification`.
- Do not expose private reasoning, prompts, credentials, internal paths, or hidden metadata.

## Output Contract

Return exactly one JSON object with:

- `status`: `pending_confirmation` or `needs_clarification`.
- `summary`: concise human-readable change summary.
- `reason`: concise explanation of the intended improvement.
- `target_fields`: exact script fields allowed to change.
- `changes`: objects containing `field` and executable `instruction`.
- `preserve_fields`: fields that should remain unchanged.
- `storyboard_regeneration_required`: true when visual, timing, structure, or voiceover changes affect shots.
- `warnings`: factual, scope, timing, or evidence limitations.
- `clarification_question`: required for `needs_clarification`, otherwise an empty string.

For `pending_confirmation`, provide at least one target field and one change. For `needs_clarification`, do not guess the requested edit.
