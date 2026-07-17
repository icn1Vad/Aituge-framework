from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from scheduling.scheduler import SchedulingRuntimeContext
from task_manager.memory import TaskMemoryView
from task_manager.models import TaskEntity
from task_manager.registry import TaskType


@dataclass(frozen=True, slots=True)
class TaskExecutionContext:
    task: TaskEntity
    task_type: TaskType
    memory_view: TaskMemoryView
    runtime_context: SchedulingRuntimeContext


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
    stage_run_id: str | None = None
    agent_id: str | None = None
    tool_call_id: str | None = None
    stream_semantics: str = "status"
    source: dict[str, Any] = field(default_factory=dict)
    structured_output: dict[str, Any] | None = None
    terminal_status: str | None = None
    outcome: str | None = None


class TaskHandler(Protocol):
    async def stream(
        self,
        *,
        context: TaskExecutionContext,
    ) -> AsyncIterator[TaskHandlerEvent]:
        ...
