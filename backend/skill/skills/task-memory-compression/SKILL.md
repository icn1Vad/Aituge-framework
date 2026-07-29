---
name: task-memory-compression
description: Merge user-confirmed information into one concise shared Task Memory.
tags: [task, memory, compression]
---

# Task Memory Compression

## Responsibility

Produce the complete next version of one business Task's shared memory. The memory is used by all Skills in that Task and must remain concise, stable, and directly useful.

## Rules

- Read the previous memory before applying new information.
- Keep durable user preferences, confirmed decisions, relevant background, and unresolved constraints.
- Merge duplicates and rewrite conversational wording as clear instructions.
- Replace an older rule when the new confirmed information conflicts with it.
- Add independent new information without removing unrelated valid memory.
- Preserve the force and scope of the user's wording. Do not turn suggestions such as "slightly", "prefer", "when possible", or "more" into "must", "always", or "forbidden" unless the user explicitly made the rule mandatory.
- Do not invent preferences, facts, decisions, or user intent.
- Do not include implementation details, database fields, Agent reasoning, or temporary execution status.
- Keep the memory concise enough to inject into every later Skill call.
- The current request only updates memory; do not execute the business task.

## Output

Return exactly one JSON object with one string field:

```json
{"memory":"complete updated Task Memory"}
```
