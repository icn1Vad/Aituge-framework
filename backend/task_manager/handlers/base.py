from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from task_manager.models import TaskEntity
from task_manager.registry import TaskDefinition


@dataclass(slots=True)
class TaskHandlerEvent:
    event_type: str
    stage: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    level: str = "info"
    step_id: str | None = None
    step_index: int | None = None
    item_id: str | None = None
    duration_ms: int | None = None
    token_usage: dict[str, Any] | None = None
    error_code: str | None = None
    visible: bool = True
    delta: str = ""
    thread_id: str | None = None
    session_id: str | None = None
    final_content: str | None = None
    usage: dict[str, Any] | None = None


class TaskHandler(Protocol):
    async def stream(
        self,
        *,
        task: TaskEntity,
        definition: TaskDefinition,
    ) -> AsyncIterator[TaskHandlerEvent]:
        ...
