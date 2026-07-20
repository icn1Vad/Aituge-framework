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
_HANDLER_SOURCES: dict[str, str] = {}


def register_stage_handler(
    name: str,
    handler: StageServiceHandler,
    *,
    source: str = "framework",
) -> None:
    normalized_source = source.strip()
    if not normalized_source:
        raise ValueError("Stage handler source is required.")
    existing = _HANDLERS.get(name)
    existing_source = _HANDLER_SOURCES.get(name)
    if existing is not None and existing_source != normalized_source:
        raise ValueError(
            f"Stage handler '{name}' is already registered by "
            f"'{existing_source or 'framework'}'."
        )
    _HANDLERS[name] = handler
    _HANDLER_SOURCES[name] = normalized_source


def get_stage_handler(name: str) -> StageServiceHandler:
    try:
        return _HANDLERS[name]
    except KeyError as exc:
        available = ", ".join(sorted(_HANDLERS))
        raise ValueError(f"Unknown stage handler '{name}'. Available handlers: {available}.") from exc
