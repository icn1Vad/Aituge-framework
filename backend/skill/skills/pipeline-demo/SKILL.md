---
name: pipeline-demo
description: Produce a compact structured analysis for the business-neutral TaskManager Pipeline demo.
tags: [pipeline, task-manager, test]
---

# Pipeline Demo

Analyze the supplied goal without calling tools. Keep the response concise and grounded only in the provided input.

Return exactly one JSON object with this structure:

```json
{
  "summary": "one concise summary",
  "steps": ["ordered action 1", "ordered action 2"],
  "risks": ["specific risk or constraint"]
}
```

Requirements:

- `summary` must be a non-empty string.
- `steps` must contain at least one concrete step.
- `risks` may be empty.
- Do not include Markdown outside the JSON object.
- Do not reveal private reasoning or chain-of-thought.
