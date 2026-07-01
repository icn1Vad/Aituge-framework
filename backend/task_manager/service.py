from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any, AsyncIterator, Optional

from sqlalchemy import desc
from sqlmodel import select

from db.db_context import create_db_session
from scheduling.scheduler import SchedulingRuntimeOptions

from .adapters.legacy_douyin import enrich_douyin_account_report_payload
from .gateway.service import DataAccessGateway
from .handlers.base import TaskHandlerEvent
from .handlers.batch_item_scheduler import BatchItemSchedulerHandler
from .handlers.scheduler_task import SchedulerTaskHandler
from .models import TaskEntity, TaskEventEntity, TaskItemEntity, utc_now
from .output_parser import parse_json_output
from .payload_schemas import validate_input_payload, validate_output_payload
from .registry import TaskDefinition, get_task_definition
from .schemas import TaskCreateRequest, TaskEventRead, TaskItemRead, TaskRead, TaskRunRequest


class TaskManagerService:
    def __init__(self, options: SchedulingRuntimeOptions) -> None:
        self.options = options

    async def create_task(self, request: TaskCreateRequest) -> TaskEntity:
        definition = get_task_definition(request.task_type)
        raw_input_payload = await _prepare_input_payload(request.task_type, request.input_payload)
        input_payload = validate_input_payload(definition.input_schema_name, raw_input_payload)
        validated_refs = DataAccessGateway().validate_resource_refs(
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            payload=input_payload,
        )
        if validated_refs:
            input_payload = {**input_payload, "validated_resource_refs": validated_refs}
        task_id = uuid.uuid4().hex
        task_items = _extract_task_items(input_payload)
        task = TaskEntity(
            id=task_id,
            parent_task_id=request.parent_task_id,
            root_task_id=request.root_task_id or request.parent_task_id or task_id,
            task_key=request.task_key,
            task_type=request.task_type,
            title=request.title or definition.name,
            handler_name=definition.handler,
            input_payload_json=input_payload,
            definition_snapshot_json=_definition_snapshot(definition),
            output_schema_json=request.output_schema,
            agent_id=request.agent_id or definition.default_agent_id,
            thread_id=request.thread_id,
            session_id=request.session_id,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            stream_mode=request.stream,
            progress_total=len(task_items) or 1,
            priority=request.priority,
            expires_at=request.expires_at,
            metadata_json=request.metadata,
        )
        async with create_db_session() as session:
            session.add(task)
            for index, item in enumerate(task_items, start=1):
                session.add(
                    TaskItemEntity(
                        task_id=task.id,
                        item_type=item["item_type"],
                        item_key=item["item_key"],
                        sequence=index,
                        input_payload_json=item["payload"],
                    )
                )
            await session.commit()
            await session.refresh(task)
        await self.record_event(
            task_id=task.id,
            run_id=None,
            event_type="task_created",
            stage="task_manager",
            message="Task created.",
            step_id="task_create",
            step_index=0,
            payload={
                "task_type": task.task_type,
                "agent_id": task.agent_id,
                "handler_name": task.handler_name,
                "item_count": len(task_items),
            },
        )
        return task

    async def get_task(self, task_id: str) -> TaskEntity | None:
        async with create_db_session() as session:
            return await session.get(TaskEntity, task_id)

    async def list_tasks(
        self,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TaskEntity]:
        async with create_db_session() as session:
            statement = select(TaskEntity).order_by(desc(TaskEntity.created_at)).offset(offset).limit(limit)
            if user_id:
                statement = statement.where(TaskEntity.user_id == user_id)
            if tenant_id:
                statement = statement.where(TaskEntity.tenant_id == tenant_id)
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

    async def list_items(self, task_id: str, limit: int = 200, offset: int = 0) -> list[TaskItemEntity]:
        async with create_db_session() as session:
            statement = (
                select(TaskItemEntity)
                .where(TaskItemEntity.task_id == task_id)
                .order_by(TaskItemEntity.sequence)
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
            step_id="task_start",
            step_index=1,
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
                    parse_result = parse_json_output(final_content)
                    structured_output = parse_result.structured
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
                    step_id="agent_stream",
                    step_index=30,
                )
                yield TaskEventRead.model_validate(event)

            result = {
                "content": final_content,
                "structured": structured_output,
                "usage": final_usage,
                "thread_id": task.thread_id,
                "session_id": task.session_id,
            }
            synced_items = await self._sync_result_items(task, structured_output)
            if synced_items:
                result["synced_items"] = synced_items
            if structured_output is None:
                parse_failed = await self.record_event(
                    task_id=task.id,
                    run_id=task.current_run_id,
                    event_type="output_parse_failed",
                    level="warning",
                    stage="result_validate",
                    message="Task output was not valid JSON; structured output is null.",
                    payload={
                        "output_schema_name": definition.output_schema_name,
                        "parser": parse_json_output(final_content).error,
                    },
                    step_id="output_parse",
                    step_index=95,
                )
                yield TaskEventRead.model_validate(parse_failed)
            else:
                is_valid, validation_error = validate_output_payload(
                    definition.output_schema_name,
                    structured_output,
                )
                if not is_valid:
                    validation_failed = await self.record_event(
                        task_id=task.id,
                        run_id=task.current_run_id,
                        event_type="output_validation_failed",
                        level="warning",
                        stage="result_validate",
                        message="Task structured output did not match the registered output schema.",
                        payload=validation_error or {},
                        step_id="output_validate",
                        step_index=96,
                    )
                    yield TaskEventRead.model_validate(validation_failed)
            task = await self._finish_task(task.id, status="succeeded", result=result)
            succeeded = await self.record_event(
                task_id=task.id,
                run_id=task.current_run_id,
                event_type="task_succeeded",
                stage="result_save",
                message="Task succeeded.",
                payload={"has_structured_output": structured_output is not None, "synced_items": synced_items},
                step_id="task_finish",
                step_index=99,
                token_usage=final_usage,
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
                step_id="task_finish",
                step_index=99,
                error_code=exc.__class__.__name__,
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
            step_id=event.step_id,
            step_index=event.step_index,
            item_id=event.item_id,
            duration_ms=event.duration_ms,
            token_usage=event.token_usage or event.usage,
            error_code=event.error_code,
            visible=event.visible,
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
        parent_event_id: str | None = None,
        step_id: str | None = None,
        step_index: int | None = None,
        item_id: str | None = None,
        duration_ms: int | None = None,
        token_usage: dict[str, Any] | None = None,
        error_code: str | None = None,
        visible: bool = True,
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
                parent_event_id=parent_event_id,
                sequence=(latest.sequence + 1) if latest else 1,
                event_type=event_type,
                level=level,
                stage=stage,
                step_id=step_id,
                step_index=step_index,
                item_id=item_id,
                duration_ms=duration_ms,
                token_usage_json=token_usage or {},
                error_code=error_code,
                visible=visible,
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
        if definition.handler == "batch_item_scheduler":
            return BatchItemSchedulerHandler(self.options)
        raise ValueError(f"Unsupported task handler '{definition.handler}'.")

    async def _prepare_run(self, task_id: str, request: TaskRunRequest | None) -> TaskEntity:
        async with create_db_session() as session:
            task = await session.get(TaskEntity, task_id)
            if task is None:
                raise ValueError(f"Task '{task_id}' not found.")
            if task.status == "running":
                raise ValueError(f"Task '{task_id}' is already running.")
            definition = get_task_definition(task.task_type)

            if request:
                if request.stream is not None:
                    task.stream_mode = request.stream
                if request.user_id:
                    task.user_id = request.user_id
                if request.input_patch:
                    patched_input = {**(task.input_payload_json or {}), **request.input_patch}
                    task.input_payload_json = validate_input_payload(definition.input_schema_name, patched_input)
                if request.metadata_patch:
                    task.metadata_json = {**(task.metadata_json or {}), **request.metadata_patch}

            task.status = "running"
            task.current_run_id = uuid.uuid4().hex
            task.attempt_count += 1
            task.progress_current = 0
            if not task.progress_total:
                task.progress_total = 1
            task.started_at = utc_now()
            task.finished_at = None
            task.error_payload_json = None
            task.result_payload_json = None
            task.updated_at = utc_now()
            session.add(task)
            await session.commit()
            await session.refresh(task)
            return task

    async def _sync_result_items(self, task: TaskEntity, structured_output: Any) -> dict[str, Any] | None:
        if task.task_type not in {"ai.search.chat", "media.topic.search"} or not isinstance(structured_output, dict):
            return None
        results = structured_output.get("results") if isinstance(structured_output.get("results"), list) else []
        topic_suggestions = (
            structured_output.get("topic_suggestions")
            if isinstance(structured_output.get("topic_suggestions"), list)
            else []
        )
        if not results and not topic_suggestions:
            return None

        now = utc_now()
        created_results = 0
        created_topics = 0
        async with create_db_session() as session:
            existing_result = await session.exec(select(TaskItemEntity).where(TaskItemEntity.task_id == task.id))
            existing_items = {item.item_key: item for item in existing_result.all()}
            for index, raw_item in enumerate(results, start=1):
                if not isinstance(raw_item, dict):
                    continue
                item_key = str(raw_item.get("url") or raw_item.get("title") or f"search-result-{index}")[:160]
                item = existing_items.get(item_key)
                if item is None:
                    item = TaskItemEntity(
                        task_id=task.id,
                        run_id=task.current_run_id,
                        item_type="search_result",
                        item_key=item_key,
                        sequence=index,
                        input_payload_json={
                            "query_plan": structured_output.get("query_plan") or {},
                            "source": "agent_structured_output",
                        },
                    )
                    created_results += 1
                item.run_id = task.current_run_id
                item.status = "succeeded"
                item.result_payload_json = raw_item
                item.started_at = item.started_at or task.started_at or now
                item.finished_at = now
                item.updated_at = now
                session.add(item)

            for index, raw_item in enumerate(topic_suggestions, start=1):
                if not isinstance(raw_item, dict):
                    continue
                item_key = str(raw_item.get("topic_title") or f"topic-suggestion-{index}")[:160]
                item = existing_items.get(item_key)
                if item is None:
                    item = TaskItemEntity(
                        task_id=task.id,
                        run_id=task.current_run_id,
                        item_type="topic_suggestion",
                        item_key=item_key,
                        sequence=len(results) + index,
                        input_payload_json={
                            "query_plan": structured_output.get("query_plan") or {},
                            "source": "agent_structured_output",
                        },
                    )
                    created_topics += 1
                item.run_id = task.current_run_id
                item.status = "succeeded"
                item.result_payload_json = raw_item
                item.started_at = item.started_at or task.started_at or now
                item.finished_at = now
                item.updated_at = now
                session.add(item)

            task_row = await session.get(TaskEntity, task.id)
            if task_row is not None:
                item_count = len(results) + len(topic_suggestions)
                task_row.progress_total = max(task_row.progress_total or 0, item_count or 1)
                task_row.progress_current = item_count or task_row.progress_current
                task_row.updated_at = now
                session.add(task_row)
            await session.commit()
        return {
            "item_type": "search_result",
            "count": len(results),
            "created": created_results,
            "topic_suggestion_count": len(topic_suggestions),
            "topic_suggestions_created": created_topics,
        }

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
            now = utc_now()
            if status == "succeeded":
                task.progress_current = task.progress_total or 1
            task.finished_at = now
            task.updated_at = now
            session.add(task)
            item_result = await session.exec(select(TaskItemEntity).where(TaskItemEntity.task_id == task_id))
            for item in item_result.all():
                if item.status not in {"pending", "running"}:
                    continue
                item.run_id = task.current_run_id
                item.started_at = item.started_at or task.started_at or now
                item.finished_at = now
                item.updated_at = now
                if status == "succeeded":
                    item.status = "succeeded"
                    item.result_payload_json = item.result_payload_json or {"processed_by": "task_level_handler"}
                elif status == "failed":
                    item.status = "failed"
                    item.error_payload_json = item.error_payload_json or error
                session.add(item)
            await session.commit()
            await session.refresh(task)
            return task


def _definition_snapshot(definition: TaskDefinition) -> dict[str, Any]:
    return asdict(definition)


async def _prepare_input_payload(task_type: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    if task_type == "analytics.douyin.account_report.generate":
        return await enrich_douyin_account_report_payload(dict(input_payload or {}))
    return input_payload


def _extract_task_items(input_payload: dict[str, Any]) -> list[dict[str, Any]]:
    for source_key, item_type in (
        ("items", "item"),
        ("rows", "table_row"),
        ("script_candidates", "script_candidate"),
    ):
        raw_items = input_payload.get(source_key)
        if isinstance(raw_items, list):
            items: list[dict[str, Any]] = []
            for index, raw_item in enumerate(raw_items, start=1):
                if isinstance(raw_item, dict):
                    payload = raw_item
                    item_key = (
                        raw_item.get("id")
                        or raw_item.get("key")
                        or raw_item.get("name")
                        or raw_item.get("title")
                        or f"{source_key}-{index}"
                    )
                else:
                    payload = {"value": raw_item}
                    item_key = f"{source_key}-{index}"
                items.append(
                    {
                        "item_type": item_type,
                        "item_key": str(item_key),
                        "payload": payload,
                    }
                )
            return items
    return []


def task_to_read(task: TaskEntity) -> TaskRead:
    return TaskRead.model_validate(task)


def event_to_read(event: TaskEventEntity) -> TaskEventRead:
    return TaskEventRead.model_validate(event)


def item_to_read(item: TaskItemEntity) -> TaskItemRead:
    return TaskItemRead.model_validate(item)
