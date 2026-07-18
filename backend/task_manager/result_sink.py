from __future__ import annotations

import asyncio
from typing import Any

import httpx

from task_manager.models import TaskEntity
from task_manager.registry import TaskType


async def deliver_task_result(
    task: TaskEntity,
    definition: TaskType,
    output: dict[str, Any] | None,
    *,
    stage_id: str | None = None,
    status: str = "completed",
    error_message: str | None = None,
) -> None:
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
