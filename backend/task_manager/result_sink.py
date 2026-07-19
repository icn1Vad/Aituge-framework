from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from task_manager.models import TaskEntity
from task_manager.registry import TaskType


@dataclass(frozen=True, slots=True)
class ResultSinkDelivery:
    task: TaskEntity
    definition: TaskType
    output: dict[str, Any] | None
    stage_id: str | None
    status: str
    error_message: str | None


ResultSinkHandler = Callable[[ResultSinkDelivery], Awaitable[None]]


class RequiredResultSinkError(RuntimeError):
    """A required capability sink did not durably accept a result."""


@dataclass(frozen=True, slots=True)
class _RegisteredResultSink:
    handler: ResultSinkHandler
    source: str
    required: bool


_RESULT_SINKS: dict[str, _RegisteredResultSink] = {}


def register_result_sink_handler(
    task_type: str,
    handler: ResultSinkHandler,
    *,
    source: str,
    required: bool = False,
) -> None:
    normalized_task_type = task_type.strip()
    normalized_source = source.strip()
    if not normalized_task_type or not normalized_source:
        raise ValueError("Result sink task_type and source are required.")
    if not callable(handler):
        raise ValueError("Result sink handler must be callable.")
    existing = _RESULT_SINKS.get(normalized_task_type)
    if existing is not None and existing.source != normalized_source:
        raise ValueError(
            f"Result sink for '{normalized_task_type}' is already registered by "
            f"'{existing.source}'."
        )
    _RESULT_SINKS[normalized_task_type] = _RegisteredResultSink(
        handler=handler,
        source=normalized_source,
        required=required,
    )


async def deliver_task_result(
    task: TaskEntity,
    definition: TaskType,
    output: dict[str, Any] | None,
    *,
    stage_id: str | None = None,
    status: str = "completed",
    error_message: str | None = None,
) -> None:
    registered = _RESULT_SINKS.get(task.task_type)
    if registered is not None:
        delivery = ResultSinkDelivery(
            task=task,
            definition=definition,
            output=output,
            stage_id=stage_id,
            status=status,
            error_message=error_message,
        )
        try:
            await registered.handler(delivery)
        except Exception as exc:
            if registered.required:
                raise RequiredResultSinkError(
                    f"Required result sink for '{task.task_type}' failed: {exc}"
                ) from exc
            raise
        return
    url = str(definition.result_sink_url or "").strip()
    if not url:
        return
    payload: dict[str, Any] = {
        "task_id": task.id,
        "run_id": task.current_run_id,
        "task_type": task.task_type,
        "audit_id": str((task.input_payload_json or {}).get("audit_id") or ""),
        "status": status,
        "output": output,
    }
    if stage_id:
        payload["stage_id"] = stage_id
    if error_message:
        payload["error_message"] = error_message
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
            return
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            await asyncio.sleep(0.1)
    raise RuntimeError(f"Task result sink failed: {last_error}") from last_error
