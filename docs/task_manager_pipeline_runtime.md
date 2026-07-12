# TaskManager Pipeline Runtime V1

## Scope

Pipeline Runtime adds a third TaskManager execution mode without replacing the existing handlers:

```text
Task Registry
  -> SchedulerTaskHandler
  -> BatchItemSchedulerHandler
  -> PipelineTaskHandler
       -> Pipeline Registry
       -> PipelineExecutor
       -> Agent / deterministic / gateway / finalizer stages
```

Agent stages always call `SchedulingService.stream_chat()`. The Pipeline code does not call `ReactAgent`, load tools, or manage conversation memory directly.

## Persistence

The runtime uses these tables:

| Table | Purpose |
| --- | --- |
| `tuge_task` | User-owned business task and current status. |
| `tuge_task_run` | One execution of a task, including pipeline version and control state. |
| `tuge_task_stage_run` | One immutable attempt record for a stage. Retries create new rows. |
| `tuge_task_artifact` | Immutable, checksummed JSON output passed between stages. |
| `tuge_task_event` | Persisted lifecycle and aggregated stream events. |

Stage output is parsed and validated before an Artifact is created. Pipeline tasks fail if the final output cannot pass the registered task output schema.

## Event Stream

The canonical endpoint is:

```text
GET /task-manager/runs/{run_id}/events/stream?after_sequence=N
```

Each persisted event has a sequence scoped to one Run. A client can reconnect with `Last-Event-ID: {run_id}:{sequence}` or `after_sequence`.

The server subscribes to the live Broker before replaying SQLite events, preventing a replay-to-live gap. Heartbeats are sent every 10 seconds and are not persisted. Set the production broker with:

```text
TASK_EVENT_BROKER=redis
```

Development and tests default to the in-memory Broker.

Each background Run also acquires `run:{run_id}:executor_lock`. It uses Redis automatically when the event Broker is Redis and otherwise uses an in-process lock. Production overrides are available through:

```text
TASK_EXECUTOR_LOCK_BACKEND=redis
TASK_EXECUTOR_LOCK_TTL_SECONDS=1800
```

The Redis lock renews while the Run is active and is released only by its token owner.

Important event types:

```text
task_started / task_succeeded / task_failed / task_cancelled
pipeline_started / pipeline_paused / pipeline_resumed / pipeline_completed
stage_started / stage_retrying / stage_completed / stage_failed / stage_skipped
agent_started / agent_delta / agent_completed / agent_failed
tool_started / tool_completed / tool_failed
artifact_created / result_snapshot
human_review_required / human_review_submitted
heartbeat
```

Agent deltas are buffered by TaskManager before persistence. Tool events contain summaries and references rather than complete raw tool results.

## API

```text
POST /task-manager/tasks
POST /task-manager/tasks/{task_id}/runs
GET  /task-manager/runs/{run_id}
GET  /task-manager/runs/{run_id}/stages
GET  /task-manager/runs/{run_id}/events
GET  /task-manager/runs/{run_id}/events/stream
GET  /task-manager/tasks/{task_id}/artifacts
GET  /task-manager/artifacts/{artifact_id}
POST /task-manager/runs/{run_id}/cancel
POST /task-manager/runs/{run_id}/review
POST /task-manager/runs/{run_id}/stages/{stage_id}/retry
```

Task creation accepts both `input_payload`/`metadata` and the V1 aliases `input`/`client_context`. Task and Run creation accept `Idempotency-Key`.

## Registering A Reusable Pipeline

1. Register the task in `task_manager/registry.py` with `handler="pipeline"` and a `pipeline_id`.
2. Register task and stage Pydantic schemas. No stage output is stored before validation.
3. Register a `PipelineDefinition` in the Pipeline Registry.
4. For Agent stages, register the Agent Profile and Skill Package through existing registries.
5. For deterministic, gateway, or finalizer stages, register a named stage handler.
6. Keep each Agent Profile's default tools inside the Stage tool allowlist. The runtime rejects excess profile tools.

Artifacts, not shared Agent conversations, are the formal stage-to-stage contract.

## Business-Neutral Demo

`pipeline.demo` validates the runtime without media-specific code:

```text
analyze (Agent)
  -> normalize (deterministic)
  -> finalize (finalizer)
```

Input:

```json
{
  "goal": "Design a reusable multi-stage task workflow",
  "context": {},
  "require_human_review": false
}
```

Set `require_human_review=true` to test pause and resume. The test page exposes Run, Cancel, and Approve actions and displays StageRun, Artifact, and recent event state.

## Verification

```powershell
poetry run pytest -q
poetry run uvicorn backend.local_code_chat_app:create_app --factory --host 0.0.0.0 --port 8894
```

Open `http://localhost:8894`, expand `TaskManager v1`, select `pipeline.demo`, and run the normal and human-review scenarios.
