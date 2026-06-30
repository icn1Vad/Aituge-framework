from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Optional

from sqlalchemy import desc
from sqlmodel import select

from db.db_context import create_db_session
from scheduling.scheduler import SchedulingRuntimeOptions

from .handlers.base import TaskHandlerEvent
from .handlers.scheduler_task import SchedulerTaskHandler
from .models import TaskEntity, TaskEventEntity, utc_now
from .registry import TaskDefinition, get_task_definition
from .schemas import TaskCreateRequest, TaskEventRead, TaskRead, TaskRunRequest


class TaskManagerService:
    def __init__(self, options: SchedulingRuntimeOptions) -> None:
        self.options = options

    async def create_task(self, request: TaskCreateRequest) -> TaskEntity:
        definition = get_task_definition(request.task_type)
        task = TaskEntity(
            task_type=request.task_type,
            title=request.title or definition.name,
            input_payload_json=request.input_payload,
            agent_id=request.agent_id or definition.default_agent_id,
            thread_id=request.thread_id,
            session_id=request.session_id,
            user_id=request.user_id,
            stream_mode=request.stream,
            metadata_json=request.metadata,
        )
        async with create_db_session() as session:
            session.add(task)
            await session.commit()
            await session.refresh(task)
        await self.record_event(
            task_id=task.id,
            run_id=None,
            event_type="task_created",
            stage="task_manager",
            message="Task created.",
            payload={"task_type": task.task_type, "agent_id": task.agent_id},
        )
        return task

    async def get_task(self, task_id: str) -> TaskEntity | None:
        async with create_db_session() as session:
            return await session.get(TaskEntity, task_id)

    async def list_tasks(self, user_id: Optional[str] = None, limit: int = 50, offset: int = 0) -> list[TaskEntity]:
        async with create_db_session() as session:
            statement = select(TaskEntity).order_by(desc(TaskEntity.created_at)).offset(offset).limit(limit)
            if user_id:
                statement = statement.where(TaskEntity.user_id == user_id)
            result = await session.exec(statement)
            return list(result.all())

    async def list_events(self, task_id: str, limit: int = 200, offset: int = 0) -> list[TaskEventEntity]:
        async with create_db_session() as session:
            statement = (
                select(TaskEventEntity)
                .where(TaskEventEntity.task_id == task_id)
                .order_by(TaskEventEntity.sequence)
                .offset(offset)
                .limit(limit)
            )
            result = await session.exec(statement)
            return list(result.all())

    async def run_task(self, task_id: str, request: TaskRunRequest | None = None) -> TaskEntity:
        async for _ in self.stream_task(task_id, request):
            pass
        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found.")
        return task

    async def stream_task(
        self,
        task_id: str,
        request: TaskRunRequest | None = None,
    ) -> AsyncIterator[TaskEventRead]:
        task = await self._prepare_run(task_id, request)
        definition = get_task_definition(task.task_type)
        handler = self._get_handler(definition)
        final_content = ""
        final_usage = None
        structured_output: Any = None
        stream_buffer: list[str] = []

        started = await self.record_event(
            task_id=task.id,
            run_id=task.current_run_id,
            event_type="task_started",
            stage="task_manager",
            message="Task started.",
            payload={"attempt_count": task.attempt_count, "task_type": task.task_type},
        )
        yield TaskEventRead.model_validate(started)

        try:
            async for item in handler.stream(task=task, definition=definition):
                if item.thread_id or item.session_id:
                    task = await self._update_task_session(
                        task.id,
                        thread_id=item.thread_id,
                        session_id=item.session_id,
                    )
                if item.final_content is not None:
                    final_content = item.final_content
                    final_usage = item.usage
                    structured_output = _try_parse_json(final_content)
                    event = await self.record_event_from_handler(task.id, task.current_run_id, item)
                    yield TaskEventRead.model_validate(event)
                    continue

                if item.delta:
                    stream_buffer.append(item.delta)
                    if sum(len(part) for part in stream_buffer) < 400:
                        continue
                    item.payload = {**item.payload, "delta": "".join(stream_buffer)}
                    stream_buffer.clear()

                event = await self.record_event_from_handler(task.id, task.current_run_id, item)
                yield TaskEventRead.model_validate(event)

            if stream_buffer:
                event = await self.record_event(
                    task_id=task.id,
                    run_id=task.current_run_id,
                    event_type="stream_chunk",
                    stage="agent_stream",
                    message="Buffered agent stream chunk received.",
                    payload={"delta": "".join(stream_buffer)},
                )
                yield TaskEventRead.model_validate(event)

            result = {
                "content": final_content,
                "structured": structured_output,
                "usage": final_usage,
                "thread_id": task.thread_id,
                "session_id": task.session_id,
            }
            task = await self._finish_task(task.id, status="succeeded", result=result)
            succeeded = await self.record_event(
                task_id=task.id,
                run_id=task.current_run_id,
                event_type="task_succeeded",
                stage="result_save",
                message="Task succeeded.",
                payload={"has_structured_output": structured_output is not None},
            )
            yield TaskEventRead.model_validate(succeeded)
        except Exception as exc:
            error = {
                "type": exc.__class__.__name__,
                "stage": "task_manager",
                "message": str(exc),
                "retryable": True,
            }
            task = await self._finish_task(task.id, status="failed", error=error)
            failed = await self.record_event(
                task_id=task.id,
                run_id=task.current_run_id,
                event_type="task_failed",
                level="error",
                stage="task_manager",
                message=str(exc),
                payload=error,
            )
            yield TaskEventRead.model_validate(failed)
            raise

    async def record_event_from_handler(
        self,
        task_id: str,
        run_id: str | None,
        event: TaskHandlerEvent,
    ) -> TaskEventEntity:
        return await self.record_event(
            task_id=task_id,
            run_id=run_id,
            event_type=event.event_type,
            level=event.level,
            stage=event.stage,
            message=event.message,
            payload=event.payload,
        )

    async def record_event(
        self,
        *,
        task_id: str,
        run_id: str | None,
        event_type: str,
        stage: str,
        message: str,
        payload: dict[str, Any] | None = None,
        level: str = "info",
    ) -> TaskEventEntity:
        async with create_db_session() as session:
            statement = (
                select(TaskEventEntity)
                .where(TaskEventEntity.task_id == task_id)
                .order_by(desc(TaskEventEntity.sequence))
                .limit(1)
            )
            latest = (await session.exec(statement)).first()
            event = TaskEventEntity(
                task_id=task_id,
                run_id=run_id,
                sequence=(latest.sequence + 1) if latest else 1,
                event_type=event_type,
                level=level,
                stage=stage,
                message=message,
                payload_json=payload or {},
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
            return event

    def _get_handler(self, definition: TaskDefinition):
        if definition.handler == "scheduler":
            return SchedulerTaskHandler(self.options)
        raise ValueError(f"Unsupported task handler '{definition.handler}'.")

    async def _prepare_run(self, task_id: str, request: TaskRunRequest | None) -> TaskEntity:
        async with create_db_session() as session:
            task = await session.get(TaskEntity, task_id)
            if task is None:
                raise ValueError(f"Task '{task_id}' not found.")
            if task.status == "running":
                raise ValueError(f"Task '{task_id}' is already running.")

            if request:
                if request.stream is not None:
                    task.stream_mode = request.stream
                if request.user_id:
                    task.user_id = request.user_id
                if request.input_patch:
                    task.input_payload_json = {**(task.input_payload_json or {}), **request.input_patch}
                if request.metadata_patch:
                    task.metadata_json = {**(task.metadata_json or {}), **request.metadata_patch}

            task.status = "running"
            task.current_run_id = uuid.uuid4().hex
            task.attempt_count += 1
            task.started_at = utc_now()
            task.finished_at = None
            task.error_payload_json = None
            task.result_payload_json = None
            task.updated_at = utc_now()
            session.add(task)
            await session.commit()
            await session.refresh(task)
            return task

    async def _update_task_session(
        self,
        task_id: str,
        *,
        thread_id: str | None,
        session_id: str | None,
    ) -> TaskEntity:
        async with create_db_session() as session:
            task = await session.get(TaskEntity, task_id)
            if task is None:
                raise ValueError(f"Task '{task_id}' not found.")
            if thread_id:
                task.thread_id = thread_id
            if session_id:
                task.session_id = session_id
            task.updated_at = utc_now()
            session.add(task)
            await session.commit()
            await session.refresh(task)
            return task

    async def _finish_task(
        self,
        task_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> TaskEntity:
        async with create_db_session() as session:
            task = await session.get(TaskEntity, task_id)
            if task is None:
                raise ValueError(f"Task '{task_id}' not found.")
            task.status = status
            task.result_payload_json = result
            task.error_payload_json = error
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            session.add(task)
            await session.commit()
            await session.refresh(task)
            return task


def _try_parse_json(content: str) -> Any:
    text = content.strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def task_to_read(task: TaskEntity) -> TaskRead:
    return TaskRead.model_validate(task)


def event_to_read(event: TaskEventEntity) -> TaskEventRead:
    return TaskEventRead.model_validate(event)
