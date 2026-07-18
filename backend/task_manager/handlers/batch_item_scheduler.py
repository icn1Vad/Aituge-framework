from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

from db.db_context import create_db_session
from scheduling.agent_registry import ensure_default_agent_profiles, get_agent_profile
from scheduling.scheduler import (
    SchedulingChatRequest,
    SchedulingRuntimeContext,
    SchedulingRuntimeOptions,
    SchedulingService,
)
from skill import ensure_default_skill_packages

from task_manager import item_store
from task_manager.handlers.base import TaskExecutionContext, TaskHandlerEvent
from task_manager.models import TaskEntity, TaskItemEntity
from task_manager.output_parser import parse_json_output
from task_manager.payload_schemas import validate_output_payload
from task_manager.registry import TaskType
from task_manager.result_sink import deliver_task_result


class BatchItemSchedulerHandler:
    def __init__(self, options: SchedulingRuntimeOptions) -> None:
        self.options = options

    async def stream(
        self,
        *,
        context: TaskExecutionContext,
        item_type: str | None = None,
    ) -> AsyncIterator[TaskHandlerEvent]:
        task = context.task
        definition = context.task_type
        config = _batch_config(task)
        items = await item_store.list_pending_items(task.id, item_type=item_type)
        async with create_db_session() as session:
            # Seed packages before parallel workers create their skill contexts.
            await ensure_default_skill_packages(session)
            await ensure_default_agent_profiles(session)
            profile = await get_agent_profile(session, task.agent_id or definition.default_agent_id)

        if profile is None or not profile.enabled:
            raise ValueError(f"Agent profile '{task.agent_id or definition.default_agent_id}' is not available.")
        if profile.agent_type != "single":
            raise ValueError(f"Agent profile '{profile.agent_id}' has unsupported type '{profile.agent_type}'.")

        yield TaskHandlerEvent(
            event_type="batch_started",
            stage="batch_item_scheduler",
            message="Batch item scheduler started.",
            step_id="batch_start",
            step_index=10,
            payload={
                "item_count": len(items),
                "max_concurrency": config["max_concurrency"],
                "failure_policy": config["failure_policy"],
                "retry_per_item": config["retry_per_item"],
            },
        )

        if not items:
            final = _summarize_results(
                await item_store.load_item_results(task.id, item_type=item_type)
            )
            await _deliver_result(task, definition, final)
            yield TaskHandlerEvent(
                event_type="batch_succeeded",
                stage="batch_item_scheduler",
                message="Batch item scheduler finished with no pending items.",
                step_id="batch_finish",
                step_index=90,
                payload=final["summary"],
                final_content=json.dumps(final, ensure_ascii=False),
                usage={},
            )
            return

        queue: asyncio.Queue[TaskHandlerEvent | None] = asyncio.Queue()
        stop_event = asyncio.Event()
        semaphore = asyncio.Semaphore(config["max_concurrency"])

        async def worker(item: TaskItemEntity) -> None:
            async with semaphore:
                if stop_event.is_set():
                    return
                await _process_item(
                    item=item,
                    task=task,
                    definition=definition,
                    profile=profile,
                    config=config,
                    queue=queue,
                    stop_event=stop_event,
                    options=self.options,
                    runtime_context=context.runtime_context,
                )

        workers = [asyncio.create_task(worker(item)) for item in items]

        async def close_queue_when_done() -> None:
            await asyncio.gather(*workers, return_exceptions=True)
            await queue.put(None)

        closer = asyncio.create_task(close_queue_when_done())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            await closer

        results = await item_store.load_item_results(task.id, item_type=item_type)
        final = _summarize_results(results)
        if final["summary"]["failed"] and config["failure_policy"] == "fail_fast":
            await _deliver_result(task, definition, final)
            yield TaskHandlerEvent(
                event_type="batch_failed",
                stage="batch_item_scheduler",
                message="Batch item scheduler failed.",
                step_id="batch_finish",
                step_index=90,
                payload=final["summary"],
                level="error",
            )
            raise ValueError("Batch item scheduler failed with failure_policy=fail_fast.")

        await _deliver_result(task, definition, final)
        yield TaskHandlerEvent(
            event_type="batch_succeeded",
            stage="batch_item_scheduler",
            message="Batch item scheduler succeeded.",
            step_id="batch_finish",
            step_index=90,
            payload=final["summary"],
            final_content=json.dumps(final, ensure_ascii=False),
            usage=_aggregate_usage(results),
        )


async def _process_item(
    *,
    item: TaskItemEntity,
    task: TaskEntity,
    definition: TaskType,
    profile,
    config: dict[str, Any],
    queue: asyncio.Queue[TaskHandlerEvent | None],
    stop_event: asyncio.Event,
    options: SchedulingRuntimeOptions,
    runtime_context: SchedulingRuntimeContext,
) -> None:
    attempts = config["retry_per_item"] + 1
    retry_feedback = ""
    for attempt in range(1, attempts + 1):
        start = time.perf_counter()
        try:
            running_item = await item_store.mark_item_running(item.id, task.current_run_id)
            await queue.put(
                TaskHandlerEvent(
                    event_type="item_started",
                    stage="batch_item_scheduler",
                    message="Task item started.",
                    step_id="item_start",
                    step_index=20,
                    item_id=running_item.id,
                    payload={
                        "item_key": running_item.item_key,
                        "item_type": running_item.item_type,
                        "attempt": attempt,
                        "skill_package": _item_skill_package(running_item, definition),
                    },
                )
            )

            content, usage = await _run_scheduler_for_item(
                item=running_item,
                task=task,
                definition=definition,
                profile=profile,
                options=options,
                queue=queue,
                runtime_context=runtime_context,
                retry_feedback=retry_feedback,
            )
            parse_result = parse_json_output(content)
            parsed = parse_result.structured
            if parse_result.error is not None:
                retry_feedback = (
                    "The previous response was not valid JSON: "
                    f"{parse_result.error}. Return a corrected JSON object only."
                )
                await queue.put(
                    TaskHandlerEvent(
                        event_type="item_output_parse_failed",
                        stage="result_validate",
                        message="Task item output was not valid JSON.",
                        step_id="item_output_parse",
                        step_index=60,
                        item_id=running_item.id,
                        level="warning",
                        payload={
                            "item_key": running_item.item_key,
                            "parser": parse_result.error,
                        },
                    )
                )
                raise ValueError("Task item output was not valid JSON.")
            elif definition.item_output_schema_name:
                is_valid, validation_error = validate_output_payload(
                    definition.item_output_schema_name,
                    parsed,
                )
                if not is_valid:
                    retry_feedback = _validation_retry_feedback(validation_error)
                    await queue.put(
                        TaskHandlerEvent(
                            event_type="item_output_validation_failed",
                            stage="result_validate",
                            message="Task item structured output did not match the registered output schema.",
                            step_id="item_output_validate",
                            step_index=61,
                            item_id=running_item.id,
                            level="warning",
                            payload={
                                "item_key": running_item.item_key,
                                **(validation_error or {}),
                            },
                        )
                    )
                    raise ValueError(
                        "Task item structured output did not match the registered output schema."
                    )
            result = {
                "item_key": running_item.item_key,
                "status": "succeeded",
                "result": parsed if parsed is not None else {"content": content},
                "raw_content": content,
                "usage": usage or {},
            }
            await item_store.finish_item_success(running_item.id, result)
            completed, total = await item_store.refresh_task_progress(task.id)
            await queue.put(
                TaskHandlerEvent(
                    event_type="item_succeeded",
                    stage="batch_item_scheduler",
                    message="Task item succeeded.",
                    step_id="item_finish",
                    step_index=70,
                    item_id=running_item.id,
                    duration_ms=_duration_ms(start),
                    token_usage=usage,
                    payload={
                        "item_key": running_item.item_key,
                        "progress_current": completed,
                        "progress_total": total,
                    },
                )
            )
            return
        except Exception as exc:
            if attempt < attempts:
                await queue.put(
                    TaskHandlerEvent(
                        event_type="item_retry",
                        stage="batch_item_scheduler",
                        message=str(exc),
                        step_id="item_retry",
                        step_index=50,
                        item_id=item.id,
                        level="warning",
                        error_code=exc.__class__.__name__,
                        payload={"item_key": item.item_key, "attempt": attempt},
                    )
                )
                continue

            error = {
                "type": exc.__class__.__name__,
                "message": str(exc),
                "retryable": False,
                "attempt": attempt,
            }
            await item_store.finish_item_failed(item.id, error)
            completed, total = await item_store.refresh_task_progress(task.id)
            await queue.put(
                TaskHandlerEvent(
                    event_type="item_failed",
                    stage="batch_item_scheduler",
                    message=str(exc),
                    step_id="item_finish",
                    step_index=70,
                    item_id=item.id,
                    duration_ms=_duration_ms(start),
                    level="error",
                    error_code=exc.__class__.__name__,
                    payload={
                        "item_key": item.item_key,
                        "progress_current": completed,
                        "progress_total": total,
                    },
                )
            )
            if config["failure_policy"] == "fail_fast":
                stop_event.set()
            return


async def _run_scheduler_for_item(
    *,
    item: TaskItemEntity,
    task: TaskEntity,
    definition: TaskType,
    profile,
    options: SchedulingRuntimeOptions,
    queue: asyncio.Queue[TaskHandlerEvent | None],
    runtime_context: SchedulingRuntimeContext,
    retry_feedback: str = "",
) -> tuple[str, dict[str, Any] | None]:
    request = SchedulingChatRequest(
        message=_build_item_message(task, definition, item, retry_feedback=retry_feedback),
        user_id=task.user_id,
        session_id=f"{task.id}:{item.id}",
        stream=True,
        skill_package=_item_skill_package(item, definition),
        extra_tools=definition.default_tools,
        extra_datasets=definition.default_datasets,
    )
    service = SchedulingService(options)
    final_content = ""
    final_usage = None
    buffered_delta: list[str] = []

    async for event in service.stream_chat(
        profile,
        request,
        runtime_context=runtime_context,
    ):
        payload = event.model_dump(exclude_none=True)
        if event.event == "final":
            data = event.data or {}
            final_content = str(data.get("content") or "")
            final_usage = data.get("usage")
            continue
        delta = _extract_delta(event.data or {})
        if delta:
            buffered_delta.append(delta)
            if sum(len(part) for part in buffered_delta) < 400:
                continue
            await queue.put(
                TaskHandlerEvent(
                    event_type="item_stream_chunk",
                    stage="agent_stream",
                    message="Task item stream chunk received.",
                    step_id="item_agent_stream",
                    step_index=40,
                    item_id=item.id,
                    payload={"item_key": item.item_key, "delta": "".join(buffered_delta)},
                )
            )
            buffered_delta.clear()

    if buffered_delta:
        await queue.put(
            TaskHandlerEvent(
                event_type="item_stream_chunk",
                stage="agent_stream",
                message="Task item stream chunk received.",
                step_id="item_agent_stream",
                step_index=40,
                item_id=item.id,
                payload={"item_key": item.item_key, "delta": "".join(buffered_delta)},
            )
        )
    return final_content, final_usage


def _item_skill_package(item: TaskItemEntity, definition: TaskType) -> str | None:
    requested = str((item.input_payload_json or {}).get("skill_package") or "").strip()
    return requested or definition.default_skill_package


def _build_item_message(
    task: TaskEntity,
    definition: TaskType,
    item: TaskItemEntity,
    *,
    retry_feedback: str = "",
) -> str:
    global_input = dict(task.input_payload_json or {})
    for key in tuple(global_input):
        if key in {"items", "rows", "script_candidates", "summary_chunks"} or key.endswith("_items"):
            global_input.pop(key, None)
    parts = [
            f"You are executing task_type: {task.task_type}.",
            f"Task title: {task.title or definition.name}",
            "Global task input:",
            json.dumps(global_input, ensure_ascii=False, indent=2),
            "Current item:",
            json.dumps(item.input_payload_json or {}, ensure_ascii=False, indent=2),
    ]
    if retry_feedback:
        parts.extend(["Previous response validation feedback:", retry_feedback])
    parts.append("Return exactly one valid JSON object for this item. Do not add Markdown outside the JSON.")
    return "\n".join(parts)


def _validation_retry_feedback(error: dict[str, Any] | None) -> str:
    details: list[str] = []
    for item in (error or {}).get("errors") or []:
        if not isinstance(item, dict):
            continue
        location = ".".join(str(part) for part in (item.get("loc") or [])) or "output"
        message = str(item.get("msg") or "invalid value")
        details.append(f"{location}: {message}")
        if len(details) == 5:
            break
    summary = "; ".join(details) or "the output did not match the registered schema"
    return (
        f"The previous response failed schema validation ({summary}). "
        "Correct these exact issues; do not repeat the invalid response."
    )


def _batch_config(task: TaskEntity) -> dict[str, Any]:
    payload = task.input_payload_json or {}
    metadata = task.metadata_json or {}
    max_concurrency = int(payload.get("max_concurrency") or metadata.get("max_concurrency") or 1)
    retry_per_item = int(payload.get("retry_per_item") or metadata.get("retry_per_item") or 0)
    failure_policy = str(payload.get("failure_policy") or metadata.get("failure_policy") or "continue")
    return {
        "max_concurrency": max(1, min(max_concurrency, 8)),
        "retry_per_item": max(0, min(retry_per_item, 3)),
        "failure_policy": failure_policy if failure_policy in {"continue", "fail_fast"} else "continue",
    }


def _summarize_results(items: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "total": len(items),
        "succeeded": len([item for item in items if item["status"] == "succeeded"]),
        "failed": len([item for item in items if item["status"] == "failed"]),
        "skipped": len([item for item in items if item["status"] == "skipped"]),
    }
    return {"summary": summary, "items": items}


def _aggregate_usage(items: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, int] = {}
    for item in items:
        usage = ((item.get("result") or {}).get("usage") or {})
        for key, value in usage.items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    return totals


def _extract_delta(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return str(delta.get("content") or "")


def _duration_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


async def _deliver_result(task: TaskEntity, definition: TaskType, output: dict[str, Any]) -> None:
    await deliver_task_result(task, definition, output)
