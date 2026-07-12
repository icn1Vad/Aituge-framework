# Media Script Lite Pipeline V1

## Scope

`media.script.pipeline.generate` runs `media-script-lite-pipeline-v1`. It generates a
script and storyboard from operator-selected context without Agent research or review.

The Pipeline uses `media_military` only through two internal Gateway endpoints. It
does not read the legacy SQLite database directly and does not modify Scheduler,
Single Agent, or ReactAgent behavior.

## Runtime Flow

```text
media.script.pipeline.generate
  -> context (gateway)
  -> writer (media-writer-agent)
  -> storyboard (media-storyboard-agent)
  -> deterministic_checks (gateway)
  -> finalize (finalizer)
```

Each successful stage creates one immutable Artifact. The final Artifact type is
`media_script_output` and matches the existing `media_script_output` task contract.

## Stage Contracts

| Stage | Input | Output | Responsibility |
| --- | --- | --- | --- |
| `context` | `media_script_generate_input` | `media_script_context_bundle` | Load the topic card, source material, comments, persona, selected master-library cards, and risk rules through the legacy Gateway. |
| `writer` | Context Artifact | `media_script_writer_draft` | Generate the complete voiceover draft using only supplied data. |
| `storyboard` | Context and writer Artifacts | `media_storyboard_draft` | Generate executable shots without changing factual claims. |
| `deterministic_checks` | Context, writer, and storyboard Artifacts | `media_script_check_result` | Reuse legacy deterministic quality and boundary checks. |
| `finalize` | Context, writer, storyboard, and check Artifacts | `media_script_output` | Merge the script package without an LLM call or human-review pause. |

## Input Fields

The Pipeline reuses `MediaScriptGenerateInput` and adds optional compatibility fields:

| Field | Meaning |
| --- | --- |
| `topic` | Required script topic. |
| `topic_card_id` | Existing `media_military` topic-card id. |
| `source_material_id` | Existing material id used to load full text and comments. |
| `topic_card` | Supplied topic-card payload when no legacy id is available. |
| `duration_seconds` | Requested video duration, 15-300 seconds. |
| `source_brief` | Source summary or operator-provided factual context. |
| `materials` | Additional source records. |
| `comments` | Audience-language input; comments are not treated as facts. |
| `manual_direction` | Operator instruction for the content angle. |
| `persona_id` / `persona` | Optional locked persona. |
| `require_human_review` | Accepted for API compatibility but ignored by the Lite Pipeline. |
| `conversation_thread_id` | Compatibility thread id for future legacy conversation bridging. |

## Gateway Boundary

The Aituge context and check stages call:

```text
POST /internal/aituge/script/context
POST /internal/aituge/script/checks
```

Set the same `MEDIA_MILITARY_GATEWAY_TOKEN` in both services. The context endpoint
performs read-only access. It expands only the selected role, strategy, template,
script type, and at most two risk-rule cards; it does not expose the whole master
library to the Agent. Writer treats this bounded context as the complete available
evidence and does not call web search.

## Review And Warning Semantics

The Lite Pipeline does not run Research Agent or Review Agent and never enters
`waiting_human`. Deterministic Schema validation still fails the responsible stage
when an Agent returns an invalid structure. Successful deterministic checks retain
their word-count, duration, and rule findings as warnings, but a low score or
high-risk finding does not pause or fail the task.

Final output records:

```json
{
  "generation_meta": {
    "workflow": "media-script-lite-pipeline-v1",
    "reviewed": false,
    "web_research_used": false
  }
}
```

## Manual Test

1. Start the isolated `media_military` service and set its Gateway token.
2. Start Aituge with `MEDIA_MILITARY_GATEWAY_BASE_URL` pointing to that service.
3. Open the Aituge test frontend and expand `TaskManager v1`.
4. Select `media.script.pipeline.generate`.
5. Enter a topic, source brief, platform, and duration.
6. Run the task and inspect Run, Stage, Artifact, and Event state.
7. Confirm the Stage list contains only context, writer, storyboard,
   deterministic_checks, and finalize.

The production `media.script.generate` task and legacy script buttons are not changed
by this branch.
