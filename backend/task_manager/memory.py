from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import desc
from sqlmodel import select

from db.db_context import create_db_session
from scheduling.scheduler import SchedulingRuntimeOptions
from scheduling.scheduler.runtime_context import RuntimeContextBlock, SchedulingRuntimeContext
from service.conversation import LlmRuntime
from skill import SkillManager

from .models import TaskEntity, TaskMemoryEntity
from .output_parser import parse_json_output


DEFAULT_MEMORY_SKILL_PACKAGE = "task-memory-compression-package"
MEMORY_MATERIAL_MAX_CHARS = 8000

TaskMemoryMaterialKind = Literal["manual", "conversation", "artifact", "result"]


@dataclass(frozen=True, slots=True)
class TaskMemoryMaterial:
    kind: TaskMemoryMaterialKind
    content: str
    source: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaskMemoryView:
    task_key: str | None = None
    version: int | None = None
    rendered_prompt: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.rendered_prompt.strip()

    def to_runtime_context(self) -> SchedulingRuntimeContext:
        if self.is_empty:
            return SchedulingRuntimeContext()
        return SchedulingRuntimeContext(
            blocks=(
                RuntimeContextBlock(
                    kind="task_memory",
                    content=self.rendered_prompt,
                    metadata={
                        "task_key": self.task_key,
                        "version": self.version,
                    },
                ),
            )
        )


@dataclass(frozen=True, slots=True)
class TaskMemoryRefreshContext:
    options: SchedulingRuntimeOptions


@dataclass(frozen=True, slots=True)
class TaskMemoryRefreshResult:
    status: Literal["updated", "skipped"]
    memory: TaskMemoryEntity | None = None
    reason: str = ""


class TaskMemoryService:
    """Read and consolidate immutable Task-scoped memory versions."""

    def __init__(self, options: SchedulingRuntimeOptions) -> None:
        self.options = options

    async def get_latest(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_key: str,
    ) -> TaskMemoryEntity | None:
        normalized_key = _normalize_task_key(task_key)
        async with create_db_session() as session:
            result = await session.exec(
                select(TaskMemoryEntity)
                .where(TaskMemoryEntity.tenant_id == tenant_id)
                .where(TaskMemoryEntity.user_id == user_id)
                .where(TaskMemoryEntity.task_key == normalized_key)
                .order_by(desc(TaskMemoryEntity.version))
                .limit(1)
            )
            return result.first()

    async def load_view(self, task: TaskEntity) -> TaskMemoryView:
        if not task.task_key:
            return TaskMemoryView()
        memory = await self.get_latest(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            task_key=task.task_key,
        )
        if memory is None:
            return TaskMemoryView(task_key=task.task_key)
        return TaskMemoryView(
            task_key=memory.task_key,
            version=memory.version,
            rendered_prompt=render_task_memory(memory),
        )

    async def consolidate(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_key: str,
        materials: list[TaskMemoryMaterial],
        skill_package: str = DEFAULT_MEMORY_SKILL_PACKAGE,
        model_pack_id: str | None = None,
    ) -> TaskMemoryEntity:
        normalized_key = _normalize_task_key(task_key)
        normalized_materials = _normalize_materials(materials)
        if not normalized_materials:
            raise ValueError("memory materials must not be empty.")

        previous = await self.get_latest(
            tenant_id=tenant_id,
            user_id=user_id,
            task_key=normalized_key,
        )
        next_version = (previous.version if previous else 0) + 1
        source_text = _render_materials(normalized_materials)
        message = _consolidation_message(
            task_key=normalized_key,
            previous_memory=previous.content if previous else "",
            materials=normalized_materials,
        )

        skill_context = await SkillManager(tenant_id=tenant_id).create_context(
            skill_package
        )
        content = await LlmRuntime(
            tenant_id=tenant_id,
            model_pack_id=model_pack_id,
        ).complete(
            messages=[{"role": "user", "content": message}],
            system_prompt=skill_context.task_prompt,
        )
        parsed = parse_json_output(content)
        if not parsed.ok or not isinstance(parsed.structured, dict):
            raise ValueError("Memory consolidation did not return a valid JSON object.")
        memory_text = str(parsed.structured.get("memory") or "").strip()
        if not memory_text:
            raise ValueError("Memory consolidation returned an empty memory.")

        row = TaskMemoryEntity(
            tenant_id=tenant_id,
            user_id=user_id,
            task_key=normalized_key,
            version=next_version,
            content=memory_text,
            source_text=source_text,
        )
        async with create_db_session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row


def render_task_memory(memory: TaskMemoryEntity | None) -> str:
    if memory is None or not memory.content.strip():
        return ""
    return "\n".join(
        [
            "# Task Memory",
            "Use this shared memory as persistent background for the current business Task.",
            "The user's explicit instruction in the current request always takes priority.",
            memory.content.strip(),
        ]
    )


def _normalize_task_key(task_key: str) -> str:
    value = (task_key or "").strip()
    if not value:
        raise ValueError("task_key must not be empty.")
    if len(value) > 160:
        raise ValueError("task_key must be at most 160 characters.")
    return value


def _normalize_materials(materials: list[TaskMemoryMaterial]) -> list[TaskMemoryMaterial]:
    normalized: list[TaskMemoryMaterial] = []
    for material in materials:
        content = (material.content or "").strip()
        if not content:
            continue
        normalized.append(
            TaskMemoryMaterial(
                kind=material.kind,
                content=content[:MEMORY_MATERIAL_MAX_CHARS],
                source=dict(material.source),
            )
        )
    return normalized


def _render_materials(materials: list[TaskMemoryMaterial]) -> str:
    if len(materials) == 1 and materials[0].kind == "manual":
        return materials[0].content
    return "\n\n".join(
        f"[{material.kind}]\n{material.content}" for material in materials
    )


def _consolidation_message(
    *,
    task_key: str,
    previous_memory: str,
    materials: list[TaskMemoryMaterial],
) -> str:
    if len(materials) == 1 and materials[0].kind == "manual":
        material_label = "New user-confirmed information:"
    else:
        material_label = (
            "New memory materials:\n"
            "Only preserve durable facts, preferences, constraints, or decisions that the user "
            "actually stated or confirmed. Assistant suggestions are not facts unless the user "
            "accepted them. Ignore temporary progress and tool chatter."
        )
    return "\n".join(
        [
            "Consolidate the Task Memory using the active memory-consolidation skill.",
            'Return exactly one JSON object: {"memory": "the complete updated memory"}.',
            "Do not return Markdown or any text outside the JSON object.",
            f"Stable business Task ID: {task_key}",
            "Previous memory:",
            previous_memory.strip() or "(empty)",
            material_label,
            _render_materials(materials),
        ]
    )
