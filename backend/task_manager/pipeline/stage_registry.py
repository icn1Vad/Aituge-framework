from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from task_manager.models import TaskArtifactEntity, TaskEntity, TaskRunEntity, TaskStageRunEntity

from .models import StageDefinition


@dataclass(slots=True)
class StageExecutionContext:
    task: TaskEntity
    run: TaskRunEntity
    stage: StageDefinition
    stage_run: TaskStageRunEntity
    stage_input: dict[str, Any]
    artifacts: dict[str, TaskArtifactEntity]


@dataclass(slots=True)
class StageServiceResult:
    output: dict[str, Any]
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    pause: bool = False
    pause_reason: str = ""
    pause_payload: dict[str, Any] = field(default_factory=dict)


StageServiceHandler = Callable[[StageExecutionContext], Awaitable[StageServiceResult]]


_HANDLERS: dict[str, StageServiceHandler] = {}


def register_stage_handler(name: str, handler: StageServiceHandler) -> None:
    existing = _HANDLERS.get(name)
    if existing is not None and existing is not handler:
        raise ValueError(f"Stage handler '{name}' is already registered.")
    _HANDLERS[name] = handler


def get_stage_handler(name: str) -> StageServiceHandler:
    try:
        return _HANDLERS[name]
    except KeyError as exc:
        available = ", ".join(sorted(_HANDLERS))
        raise ValueError(f"Unknown stage handler '{name}'. Available handlers: {available}.") from exc
