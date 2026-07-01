# TaskManager V1

## Positioning

TaskManager is the business task runtime above Scheduler and Single Agent. It does not replace ReAct, Scheduler, Agent Registry, ToolBundle, SkillBundle, RAG, Redis history, or SQLite conversation storage.

TaskManager owns:

- Business task intake.
- Task lifecycle state.
- Code registry from `task_type` to handler and default execution config.
- Scheduler request construction.
- Stream event collection.
- Final result and error persistence.

TaskManager does not own:

- ReAct reasoning.
- Context trimming or compression.
- Tool/Skill/RAG low-level implementations.
- Agent Profile schema or default profile management.
- LLM API details.

## Added Modules

- `backend/task_manager/api.py`: FastAPI router.
- `backend/task_manager/models.py`: SQLModel tables.
- `backend/task_manager/registry.py`: code-level task registry.
- `backend/task_manager/service.py`: lifecycle and event persistence.
- `backend/task_manager/handlers/base.py`: handler protocol and runtime event model.
- `backend/task_manager/handlers/scheduler_task.py`: Scheduler-backed handler.
- `backend/skill/skills/media-script-generator/SKILL.md`: media script generation skill.
- `backend/skill/skills/media-script-selector/SKILL.md`: media script selection skill.

The router is mounted from `backend/local_code_chat_app.py`. The SQLModel tables are imported by `db.init_db()`.

## Supported Task Types

### `media.script.generate`

Generates a short-video script from topic, platform, duration, source brief, persona, materials, and other task inputs.

Defaults:

- `handler`: `scheduler`
- `agent_id`: `default-single-agent`
- `primary_skill`: `media-script-generator`
- `candidate_skills`: `media-script-selector`
- `extra_tools`: `rag_retrieval`
- `extra_datasets`: `local_rag`

### `media.script.select`

Selects the best script candidate and returns a structured decision card.

Defaults:

- `handler`: `scheduler`
- `agent_id`: `default-single-agent`
- `primary_skill`: `media-script-selector`
- `candidate_skills`: `media-script-generator`
- `extra_tools`: `rag_retrieval`
- `extra_datasets`: `local_rag`

### `media.chat`

Continues a media task conversation with task lifecycle and event persistence.

## Database Tables

### `tuge_task`

Main task instance table.

- `id`: unique task id.
- `parent_task_id`: parent task id for split tasks or subflows.
- `root_task_id`: root task id for a task tree. Root tasks point to themselves.
- `task_key`: optional business idempotency or trace key from the caller.
- `task_type`: business task type such as `media.script.generate`.
- `status`: `created`, `running`, `succeeded`, `failed`, or `cancelled`.
- `title`: display title.
- `handler_name`: handler selected from the task registry, currently `scheduler`.
- `input_payload_json`: business input and resource references. Do not store large files or full PDFs here.
- `result_payload_json`: final structured result, parsed JSON if available, usage, thread id, and session id.
- `error_payload_json`: failure type, stage, message, and retryability.
- `definition_snapshot_json`: registry definition captured at task creation time.
- `output_schema_json`: expected output contract provided by the caller.
- `agent_id`: Agent Profile used by this task.
- `thread_id`: SQLite conversation thread id from Single Agent.
- `session_id`: Redis live-context session id from Single Agent.
- `user_id`: task owner.
- `tenant_id`: tenant scope.
- `stream_mode`: whether the task was requested as streaming.
- `current_run_id`: current run attempt id.
- `attempt_count`: number of execution attempts.
- `progress_current`, `progress_total`: task-level progress counters.
- `cancel_requested`: cooperative cancellation flag for future long-running handlers.
- `priority`: scheduling priority placeholder.
- `created_at`, `started_at`, `finished_at`, `expires_at`, `updated_at`: lifecycle timestamps.
- `metadata_json`: request metadata such as source, version, or request id.

### `tuge_task_event`

Task timeline and debugging event table.

- `id`: unique event id.
- `task_id`: owning task id.
- `run_id`: owning run attempt id.
- `parent_event_id`: optional parent event id.
- `sequence`: per-task event sequence.
- `event_type`: `task_created`, `task_started`, `scheduler_request_built`, `stream_chunk`, `agent_metadata`, `agent_final`, `task_succeeded`, `task_failed`, etc.
- `level`: `info`, `warning`, or `error`.
- `stage`: `task_manager`, `scheduler_request_build`, `agent_stream`, `result_save`, etc.
- `step_id`, `step_index`: stable step identifiers for timeline display and debugging.
- `item_id`: optional related `tuge_task_item.id`.
- `duration_ms`: optional step duration.
- `token_usage_json`: token usage for agent-related events when available.
- `error_code`: structured error code or exception class.
- `visible`: whether the event should be shown in normal UI timelines.
- `message`: short readable log message.
- `payload_json`: bounded event details. Stream chunks are buffered before being persisted.
- `created_at`: event timestamp.

### `tuge_task_item`

Per-object work table for script candidates, table rows, files, or other batch items. The first version creates items from `input_payload.items`, `input_payload.rows`, or `input_payload.script_candidates`; future handlers can update each item while running.

- `id`: unique item id.
- `task_id`: owning task id.
- `run_id`: run attempt id once an item is processed.
- `item_type`: `item`, `table_row`, `script_candidate`, etc.
- `item_key`: caller-facing item id, row key, candidate id, or generated key.
- `sequence`: item order inside the task.
- `status`: `pending`, `running`, `succeeded`, `failed`, or `skipped`.
- `input_payload_json`: bounded input for this item.
- `result_payload_json`: item-level result.
- `error_payload_json`: item-level error.
- `created_at`, `started_at`, `finished_at`, `updated_at`: item lifecycle timestamps.

## API

### List Definitions

```http
GET /task-manager/definitions
```

### Create Task

```http
POST /task-manager/tasks
```

```json
{
  "task_type": "media.script.generate",
  "title": "生成短视频脚本",
  "input_payload": {
    "topic": "Agent 架构设计",
    "platform": "douyin",
    "duration_seconds": 60,
    "source_brief": "根据会议内容解释 Agent 三层架构和 TaskManager 边界。"
  },
  "user_id": "linzetao",
  "stream": true
}
```

### Run Existing Task

```http
POST /task-manager/tasks/{task_id}/run
```

### Stream Existing Task

```http
POST /task-manager/tasks/{task_id}/stream
```

### Create And Run

```http
POST /task-manager/run
```

### Create And Stream

```http
POST /task-manager/stream
```

### Query Task

```http
GET /task-manager/tasks/{task_id}
```

### Query Events

```http
GET /task-manager/tasks/{task_id}/events
```

### Query Items

```http
GET /task-manager/tasks/{task_id}/items
```

## Frontend Test Flow

1. Start the local app.
2. Open `http://127.0.0.1:8894/`.
3. In the left sidebar, find `TaskManager v1`.
4. For generation testing, keep `media.script.generate`, fill topic, duration, and source brief, then click `Run media script task`.
5. For structure/item testing, switch to `media.script.select`. The candidate JSON textarea is sent as `input_payload.script_candidates`, and each candidate is persisted as one `tuge_task_item`.
6. Watch the assistant area stream output.
7. After success, the final assistant bubble includes both the model result and a `task_manager` block with `task_id`, `handler`, `progress`, and `item_count`.
8. The sidebar `structure` box shows the durable task snapshot, item list, and recent event steps.
9. Use API calls to inspect durable records:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8894/task-manager/tasks/{task_id}
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8894/task-manager/tasks/{task_id}/events
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8894/task-manager/tasks/{task_id}/items
```

## Automated Tests

TaskManager test only:

```powershell
E:\MyProjects\AIflamework\.tools\poetry-venv\Scripts\poetry.exe run pytest tests/test_task_manager.py -q
```

Full test suite:

```powershell
E:\MyProjects\AIflamework\.tools\poetry-venv\Scripts\poetry.exe run pytest -q
```

Current verified commands:

- `.\.venv\Scripts\python.exe -m pytest tests/test_task_manager.py`
- `.\.venv\Scripts\python.exe -m compileall backend/task_manager backend/single-agent/db/db_context.py`

## Coordination Needed With Ning Ruixuan

No bottom-layer code was changed in this implementation.

Coordinate with Ning Ruixuan before changing any of the following:

- Scheduler request schema or stream event contract.
- Agent Profile fields or default profile semantics.
- SingleAgentRunner, ReactAgent, or ConversationManager behavior.
- Tool/Skill Registry rules.
- A forced tool-call protocol for card output.

The current media task output is controlled by Skill prompt and TaskManager JSON parsing. If production requires hard-guaranteed card output, the clean next step is a dedicated output/tool protocol agreed with the Scheduler/Single Agent owner.
