from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict
from typing import Any, AsyncIterator, Optional

from loguru import logger
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from db.db_context import create_db_session
from scheduling.scheduler import SchedulingRuntimeOptions

from .adapters.douyin_report_compat import add_legacy_monthly_report
from .adapters.legacy_douyin import enrich_douyin_account_report_payload
from .gateway.service import DataAccessGateway
from .handlers.base import TaskExecutionContext, TaskHandlerEvent
from .handlers.batch_item_scheduler import BatchItemSchedulerHandler
from .handlers.pipeline_task import PipelineTaskHandler
from .handlers.scheduler_task import SchedulerTaskHandler
from .models import TaskEntity, TaskEventEntity, TaskItemEntity, TaskRunEntity, utc_now
from .idempotency import (
    assert_fingerprint_matches,
    build_run_request_fingerprint,
    build_task_request_fingerprint,
)
from .memory import TaskMemoryRefreshContext, TaskMemoryRefreshResult, TaskMemoryService
from .output_parser import parse_json_output
from .payload_schemas import validate_input_payload, validate_output_payload
from .registry import TaskType, get_task_definition
from .runtime import executor_lock, get_event_broker, start_background_run
from .runtime.fencing import RunLeaseLost, current_execution_lease, verify_execution_lease
from .schemas import TaskCreateRequest, TaskEventRead, TaskItemRead, TaskRead, TaskRunRequest
from .pipeline.registry import get_pipeline_definition
from .pipeline.store import (
    create_artifact,
    create_stage_run,
    get_artifact,
    get_run,
    list_artifacts,
    list_stage_runs,
    list_task_runs,
    update_run,
    update_stage_run,
)


STREAM_EVENT_BUFFER_CHARS = 1000
MAX_EVENT_PAYLOAD_CHARS = 8192


class TaskManagerService:
    def __init__(self, options: SchedulingRuntimeOptions) -> None:
        self.options = options

    async def create_task(
        self,
        request: TaskCreateRequest,
        *,
        service_name: str = "external",
    ) -> TaskEntity:
        request_fingerprint = (
            build_task_request_fingerprint(service_name=service_name, request=request)
            if request.idempotency_key
            else ""
        )
        definition = get_task_definition(request.task_type)
        if definition.required_task_key and request.task_key != definition.required_task_key:
            raise ValueError(
                f"Task type '{request.task_type}' requires task_key "
                f"'{definition.required_task_key}'."
            )
        if request.idempotency_key:
            async with create_db_session() as session:
                result = await session.exec(
                    select(TaskEntity)
                    .where(TaskEntity.service == service_name)
                    .where(TaskEntity.tenant_id == request.tenant_id)
                    .where(TaskEntity.idempotency_key == request.idempotency_key)
                )
                existing = result.first()
                if existing is not None:
                    assert_fingerprint_matches(
                        stored_fingerprint=existing.request_fingerprint,
                        request_fingerprint=request_fingerprint,
                        resource="task",
                        idempotency_key=request.idempotency_key,
                    )
                    return existing
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
            idempotency_key=request.idempotency_key,
            request_fingerprint=request_fingerprint,
            service=service_name,
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
        try:
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
        except IntegrityError:
            if not request.idempotency_key:
                raise
            async with create_db_session() as session:
                result = await session.exec(
                    select(TaskEntity)
                    .where(TaskEntity.service == service_name)
                    .where(TaskEntity.tenant_id == request.tenant_id)
                    .where(TaskEntity.idempotency_key == request.idempotency_key)
                )
                existing = result.first()
            if existing is None:
                raise
            assert_fingerprint_matches(
                stored_fingerprint=existing.request_fingerprint,
                request_fingerprint=request_fingerprint,
                resource="task",
                idempotency_key=request.idempotency_key,
            )
            return existing
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

    async def refresh_task_memory(self, task_id: str) -> TaskMemoryRefreshResult:
        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found.")
        task_type = get_task_definition(task.task_type)
        return await task_type.refresh_memory(
            task,
            TaskMemoryRefreshContext(options=self.options),
        )

    async def update_task_metadata(
        self,
        task_id: str,
        metadata_patch: dict[str, Any],
    ) -> TaskEntity:
        """Merge runtime business metadata without replacing Task ownership fields."""

        async with create_db_session() as session:
            task = await session.get(TaskEntity, task_id)
            if task is None:
                raise ValueError(f"Task '{task_id}' not found.")
            task.metadata_json = {**(task.metadata_json or {}), **metadata_patch}
            task.updated_at = utc_now()
            session.add(task)
            await session.commit()
            await session.refresh(task)
            return task

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
                .order_by(TaskEventEntity.created_at, TaskEventEntity.sequence)
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

    async def list_runs(self, task_id: str) -> list[TaskRunEntity]:
        return await list_task_runs(task_id)

    async def get_run(self, run_id: str) -> TaskRunEntity | None:
        return await get_run(run_id)

    async def begin_external_task(
        self,
        task_id: str,
        *,
        user_id: str,
        stream: bool = False,
    ) -> TaskEntity:
        """Open a TaskManager run whose executor lives in another scheduling mode."""

        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found.")
        if task.user_id != user_id:
            raise ValueError(f"Task '{task_id}' is not available to this user.")
        if task.handler_name != "external":
            raise ValueError(f"Task '{task_id}' is not an external task.")
        task = await self._prepare_run(
            task_id,
            TaskRunRequest(stream=stream, user_id=user_id),
        )
        await self.record_event(
            task_id=task.id,
            run_id=task.current_run_id,
            event_type="task_started",
            stage="main_agent",
            message="MainAgent started the task.",
            source={"type": "main_agent", "id": task.agent_id},
        )
        return task

    async def complete_external_task(
        self,
        task_id: str,
        *,
        result: dict[str, Any],
        thread_id: str | None = None,
        session_id: str | None = None,
    ) -> TaskEntity:
        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found.")
        definition = get_task_definition(task.task_type)
        is_valid, validation_error = validate_output_payload(
            definition.output_schema_name,
            result,
        )
        if not is_valid:
            raise ValueError(
                f"External task output does not match schema "
                f"'{definition.output_schema_name}': {validation_error}"
            )
        if thread_id or session_id:
            task = await self._update_task_session(
                task_id,
                thread_id=thread_id,
                session_id=session_id,
            )
        await self.record_event(
            task_id=task.id,
            run_id=task.current_run_id,
            event_type="task_succeeded",
            stage="main_agent",
            message="MainAgent completed the task.",
            payload={"result": result},
            source={"type": "main_agent", "id": task.agent_id},
        )
        return await self._finish_task(
            task_id,
            status="succeeded",
            result=result,
            outcome="success",
        )

    async def fail_external_task(self, task_id: str, error: Exception) -> TaskEntity:
        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found.")
        payload = {
            "type": error.__class__.__name__,
            "message": str(error),
            "retryable": True,
        }
        await self.record_event(
            task_id=task.id,
            run_id=task.current_run_id,
            event_type="task_failed",
            stage="main_agent",
            level="error",
            message=str(error),
            payload=payload,
            error_code=error.__class__.__name__,
            source={"type": "main_agent", "id": task.agent_id},
        )
        return await self._finish_task(
            task_id,
            status="failed",
            error=payload,
            outcome="failure",
        )

    async def list_run_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
        event_type: str | None = None,
        stage_id: str | None = None,
    ) -> list[TaskEventEntity]:
        async with create_db_session() as session:
            statement = (
                select(TaskEventEntity)
                .where(TaskEventEntity.run_id == run_id)
                .where(TaskEventEntity.sequence > after_sequence)
            )
            if event_type:
                statement = statement.where(TaskEventEntity.event_type == event_type)
            if stage_id:
                statement = statement.where(TaskEventEntity.stage == stage_id)
            result = await session.exec(statement.order_by(TaskEventEntity.sequence).limit(limit))
            return list(result.all())

    async def list_run_stages(self, run_id: str):
        return await list_stage_runs(run_id)

    async def list_task_artifacts(self, task_id: str):
        return await list_artifacts(task_id=task_id)

    async def get_artifact(self, artifact_id: str):
        return await get_artifact(artifact_id)

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
        async for event in self._stream_prepared_task(task):
            yield event

    async def start_task_run(
        self,
        task_id: str,
        request: TaskRunRequest | None = None,
    ) -> TaskRunEntity:
        request = request or TaskRunRequest()
        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found.")
        request_fingerprint = (
            build_run_request_fingerprint(
                service_name=task.service,
                task_id=task.id,
                tenant_id=task.tenant_id,
                user_id=request.user_id or task.user_id,
                request=request,
            )
            if request.idempotency_key
            else ""
        )
        if request.idempotency_key:
            async with create_db_session() as session:
                result = await session.exec(
                    select(TaskRunEntity)
                    .where(TaskRunEntity.task_id == task_id)
                    .where(TaskRunEntity.idempotency_key == request.idempotency_key)
                )
                existing = result.first()
                if existing is not None:
                    assert_fingerprint_matches(
                        stored_fingerprint=existing.request_fingerprint,
                        request_fingerprint=request_fingerprint,
                        resource="run",
                        idempotency_key=request.idempotency_key,
                    )
                    return existing
        try:
            task = await self._prepare_run(
                task_id,
                request,
                request_fingerprint=request_fingerprint,
            )
        except IntegrityError:
            if not request.idempotency_key:
                raise
            async with create_db_session() as session:
                result = await session.exec(
                    select(TaskRunEntity)
                    .where(TaskRunEntity.task_id == task_id)
                    .where(TaskRunEntity.idempotency_key == request.idempotency_key)
                )
                existing = result.first()
            if existing is None:
                raise
            assert_fingerprint_matches(
                stored_fingerprint=existing.request_fingerprint,
                request_fingerprint=request_fingerprint,
                resource="run",
                idempotency_key=request.idempotency_key,
            )
            return existing
        if not task.current_run_id:
            raise ValueError(f"Task '{task_id}' did not create a run.")
        run = await get_run(task.current_run_id)
        if run is None:
            raise ValueError(f"Run '{task.current_run_id}' not found.")
        return run

    async def _drain_prepared_task(self, task_id: str, *, run_id: str | None = None) -> None:
        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found.")
        effective_run_id = run_id or task.current_run_id
        if not effective_run_id:
            raise ValueError(f"Task '{task_id}' has no active run.")
        if task.current_run_id != effective_run_id:
            raise ValueError(f"Task '{task_id}' does not point to Run '{effective_run_id}'.")
        async with executor_lock(effective_run_id) as acquired:
            if not acquired:
                return
            async for _ in self._stream_prepared_task(task):
                pass

    async def request_cancel(self, run_id: str) -> TaskRunEntity:
        run = await get_run(run_id)
        if run is None:
            raise ValueError(f"Run '{run_id}' not found.")
        run = await update_run(run_id, cancel_requested=True)
        async with create_db_session() as session:
            task = await session.get(TaskEntity, run.task_id)
            if task is not None:
                task.cancel_requested = True
                task.updated_at = utc_now()
                session.add(task)
                await session.commit()
        if run.status == "waiting_human":
            await update_run(run_id, status="cancelled", outcome="cancelled", finished_at=utc_now())
            await self._finish_task(run.task_id, status="cancelled", outcome="cancelled")
            await self.record_event(
                task_id=run.task_id,
                run_id=run.id,
                event_type="task_cancelled",
                stage=run.current_stage_id or "pipeline",
                message="Run cancelled while waiting for human review.",
                payload={"run_id": run.id},
                source={"type": "task_manager", "id": run.task_id},
            )
            run = (await get_run(run_id)) or run
        return run

    async def submit_human_review(
        self,
        run_id: str,
        *,
        action: str,
        comment: str = "",
        patch: dict[str, Any] | None = None,
        resume_from_stage: str | None = None,
    ) -> TaskRunEntity:
        if action not in {"approve", "reject", "revise_input", "rerun_stage"}:
            raise ValueError(f"Unsupported review action '{action}'.")
        run = await get_run(run_id)
        if run is None:
            raise ValueError(f"Run '{run_id}' not found.")
        if run.status != "waiting_human":
            raise ValueError(f"Run '{run_id}' is not waiting for human review.")
        task = await self.get_task(run.task_id)
        if task is None:
            raise ValueError(f"Task '{run.task_id}' not found.")
        if action == "reject":
            error = {"type": "human_rejected", "message": comment or "Human reviewer rejected the run."}
            await self._finish_task(task.id, status="failed", error=error, outcome="failure")
            await self.record_event(
                task_id=task.id,
                run_id=run.id,
                event_type="task_failed",
                stage=run.current_stage_id or "human_review",
                message=error["message"],
                payload=error,
                level="error",
                source={"type": "human", "id": task.user_id},
            )
            return (await get_run(run_id)) or run

        definition = get_task_definition(task.task_type)
        review_patch = dict(patch or {})
        if review_patch:
            updated_input = validate_input_payload(
                definition.input_schema_name,
                {**(task.input_payload_json or {}), **review_patch},
            )
            async with create_db_session() as session:
                task_row = await session.get(TaskEntity, task.id)
                if task_row is None:
                    raise ValueError(f"Task '{task.id}' not found.")
                task_row.input_payload_json = updated_input
                task_row.updated_at = utc_now()
                session.add(task_row)
                await session.commit()
            task.input_payload_json = updated_input

        review_attempts = [item for item in await list_stage_runs(run_id) if item.stage_id == "human_review"]
        stage_run = await create_stage_run(
            task_id=task.id,
            run_id=run.id,
            stage_id="human_review",
            stage_type="deterministic",
            attempt=len(review_attempts) + 1,
            agent_id=None,
            input_artifact_ids=[],
        )
        review_content = {
            "action": action,
            "comment": comment,
            "patch": review_patch,
            "resume_from_stage": resume_from_stage or run.current_stage_id,
        }
        artifact = await create_artifact(
            task_id=task.id,
            run_id=run.id,
            stage_run_id=stage_run.id,
            artifact_type="human_review",
            schema_name="",
            content=review_content,
            parent_artifact_ids=[],
            summary=comment,
        )
        await update_stage_run(
            stage_run.id,
            status="succeeded",
            output_artifact_id=artifact.id,
            finished_at=utc_now(),
        )
        metadata = {
            **(run.metadata_json or {}),
            "human_review": review_content,
            "resume_from_stage": resume_from_stage or run.current_stage_id,
        }
        run = await update_run(
            run.id,
            status="running",
            outcome=None,
            cancel_requested=False,
            metadata_json=metadata,
            finished_at=None,
        )
        async with create_db_session() as session:
            task_row = await session.get(TaskEntity, task.id)
            if task_row is None:
                raise ValueError(f"Task '{task.id}' not found.")
            task_row.status = "running"
            task_row.cancel_requested = False
            task_row.finished_at = None
            task_row.updated_at = utc_now()
            session.add(task_row)
            await session.commit()
        await self.record_event(
            task_id=task.id,
            run_id=run.id,
            event_type="human_review_submitted",
            stage="human_review",
            message=comment or f"Human review action '{action}' submitted.",
            payload={**review_content, "artifact_id": artifact.id},
            stream_semantics="reference",
            source={"type": "human", "id": task.user_id},
        )
        await self.record_event(
            task_id=task.id,
            run_id=run.id,
            event_type="pipeline_resumed",
            stage=resume_from_stage or run.current_stage_id or "pipeline",
            message="Pipeline resumed after human review.",
            payload={"resume_from_stage": resume_from_stage or run.current_stage_id},
            source={"type": "task_manager", "id": task.id},
        )
        return run

    async def apply_script_change_proposal(
        self,
        run_id: str,
        *,
        proposal_artifact_id: str,
        comment: str = "",
    ) -> tuple[TaskEntity, TaskEntity, TaskRunEntity]:
        run = await get_run(run_id)
        if run is None:
            raise ValueError(f"Run '{run_id}' not found.")
        proposal_task = await self.get_task(run.task_id)
        if proposal_task is None:
            raise ValueError(f"Task '{run.task_id}' not found.")
        if proposal_task.task_type != "media.script.change.propose":
            raise ValueError("Only media.script.change.propose runs can be applied.")

        artifact = await get_artifact(proposal_artifact_id)
        if artifact is None:
            raise ValueError(f"Proposal Artifact '{proposal_artifact_id}' not found.")
        if artifact.task_id != proposal_task.id or artifact.run_id != run.id:
            raise ValueError("Proposal Artifact does not belong to the requested run.")
        if artifact.artifact_type != "media_script_change_proposal" or artifact.content_json is None:
            raise ValueError("Artifact is not an applicable media script change proposal.")
        proposal = dict(artifact.content_json)
        if proposal.get("status") != "pending_confirmation":
            raise ValueError("Only pending_confirmation proposals can be applied.")
        if not proposal.get("changes"):
            raise ValueError("Proposal contains no actionable changes.")
        proposal_artifacts = [
            item
            for item in await list_artifacts(run_id=run.id)
            if item.artifact_type == "media_script_change_proposal"
        ]
        latest_proposal = proposal_artifacts[-1] if proposal_artifacts else None
        if latest_proposal is None or (
            latest_proposal.id != artifact.id and latest_proposal.checksum != artifact.checksum
        ):
            raise ValueError("Proposal Artifact is stale and no longer matches the latest confirmed content.")

        if run.status == "waiting_human":
            await self.submit_human_review(
                run.id,
                action="approve",
                comment=comment or "Approved for a new revision script task.",
                resume_from_stage="await_confirmation",
            )
        elif run.status not in {"running", "succeeded"}:
            raise ValueError(f"Proposal Run '{run.id}' cannot be applied from status '{run.status}'.")

        run = await _wait_for_run_status(run.id, {"succeeded", "failed", "cancelled"})
        review = dict(run.metadata_json or {}).get("human_review") or {}
        if run.status != "succeeded" or review.get("action") != "approve":
            raise ValueError("Proposal Run did not complete with human approval.")

        apply_key = f"script-revision:{run.id}:{artifact.checksum}"
        proposal_input = dict(proposal_task.input_payload_json or {})
        revision_payload = _build_script_revision_payload(
            proposal_task=proposal_task,
            proposal_run=run,
            proposal_artifact_id=artifact.id,
            proposal=proposal,
        )
        existing_revision = await _find_task_by_idempotency(
            user_id=proposal_task.user_id,
            tenant_id=proposal_task.tenant_id,
            idempotency_key=apply_key,
        )
        if existing_revision is not None:
            metadata = dict(existing_revision.metadata_json or {})
            if (
                metadata.get("proposal_run_id") != run.id
                or metadata.get("proposal_checksum") != artifact.checksum
            ):
                raise ValueError("Idempotency key already belongs to a different script proposal application.")
        revision_task = await self.create_task(
            TaskCreateRequest(
                task_type="media.script.pipeline.generate",
                parent_task_id=proposal_task.id,
                root_task_id=proposal_task.root_task_id or proposal_task.id,
                task_key=f"script-revision:{proposal_input.get('base_script_id')}",
                idempotency_key=apply_key,
                title=f"Revision of {proposal_input.get('base_script_id')}",
                input_payload=revision_payload,
                user_id=proposal_task.user_id,
                tenant_id=proposal_task.tenant_id,
                stream=True,
                metadata={
                    "revision_mode": True,
                    "proposal_task_id": proposal_task.id,
                    "proposal_run_id": run.id,
                    "proposal_artifact_id": artifact.id,
                    "proposal_checksum": artifact.checksum,
                    "base_script_id": proposal_input.get("base_script_id"),
                    "base_artifact_id": proposal_input.get("base_artifact_id"),
                },
            )
        )
        revision_run = await self.start_task_run(
            revision_task.id,
            TaskRunRequest(
                stream=True,
                user_id=proposal_task.user_id,
                idempotency_key=f"{apply_key}:run",
                metadata_patch={
                    "proposal_task_id": proposal_task.id,
                    "proposal_run_id": run.id,
                    "proposal_artifact_id": artifact.id,
                },
            ),
        )
        if existing_revision is None:
            await self.record_event(
                task_id=proposal_task.id,
                run_id=run.id,
                event_type="revision_dispatched",
                stage="apply",
                message="Approved proposal dispatched to a new revision task.",
                payload={
                    "proposal_artifact_id": artifact.id,
                    "revision_task_id": revision_task.id,
                    "revision_run_id": revision_run.id,
                },
                stream_semantics="reference",
                source={"type": "task_manager", "id": proposal_task.id},
            )
        return proposal_task, revision_task, revision_run

    async def _stream_prepared_task(self, task: TaskEntity) -> AsyncIterator[TaskEventRead]:
        definition = get_task_definition(task.task_type)
        handler = self._get_handler(definition)
        memory_view = await TaskMemoryService(self.options).load_view(task)
        execution_context = TaskExecutionContext(
            task=task,
            task_type=definition,
            memory_view=memory_view,
            runtime_context=memory_view.to_runtime_context(),
        )
        final_content = ""
        final_usage = None
        structured_output: Any = None
        stream_buffer: list[str] = []
        buffered_item: TaskHandlerEvent | None = None
        terminal_status: str | None = None
        terminal_outcome: str | None = None

        async def flush_stream_buffer() -> TaskEventEntity | None:
            nonlocal buffered_item
            if buffered_item is None or not stream_buffer:
                return None
            buffered_item.payload = {**buffered_item.payload, "delta": "".join(stream_buffer)}
            buffered_item.delta = ""
            stream_buffer.clear()
            event = await self.record_event_from_handler(task.id, task.current_run_id, buffered_item)
            buffered_item = None
            return event

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
            async for item in handler.stream(context=execution_context):
                if item.delta:
                    buffer_key = (item.event_type, item.stage, item.stage_run_id, item.agent_id)
                    current_key = (
                        buffered_item.event_type,
                        buffered_item.stage,
                        buffered_item.stage_run_id,
                        buffered_item.agent_id,
                    ) if buffered_item else None
                    if buffered_item is not None and current_key != buffer_key:
                        flushed = await flush_stream_buffer()
                        if flushed is not None:
                            yield TaskEventRead.model_validate(flushed)
                    buffered_item = buffered_item or item
                    stream_buffer.append(item.delta)
                    if sum(len(part) for part in stream_buffer) < definition.stream_chunk_chars:
                        continue
                    flushed = await flush_stream_buffer()
                    if flushed is not None:
                        yield TaskEventRead.model_validate(flushed)
                    continue

                flushed = await flush_stream_buffer()
                if flushed is not None:
                    yield TaskEventRead.model_validate(flushed)
                if item.thread_id or item.session_id:
                    task = await self._update_task_session(
                        task.id,
                        thread_id=item.thread_id,
                        session_id=item.session_id,
                    )
                if item.final_content is not None:
                    final_content = item.final_content
                    final_usage = item.usage or item.token_usage
                    structured_output = item.structured_output
                    if structured_output is None:
                        parse_result = parse_json_output(final_content)
                        structured_output = parse_result.structured
                terminal_status = item.terminal_status or terminal_status
                terminal_outcome = item.outcome or terminal_outcome

                event = await self.record_event_from_handler(task.id, task.current_run_id, item)
                yield TaskEventRead.model_validate(event)

            flushed = await flush_stream_buffer()
            if flushed is not None:
                yield TaskEventRead.model_validate(flushed)

            if terminal_status in {"waiting_human", "cancelled"}:
                await self._finish_task(
                    task.id,
                    status=terminal_status,
                    result={"structured": structured_output} if structured_output is not None else None,
                    outcome=terminal_outcome,
                )
                return

            result = {
                "content": final_content,
                "structured": structured_output,
                "usage": final_usage,
                "thread_id": task.thread_id,
                "session_id": task.session_id,
            }
            if task.task_type == "analytics.douyin.account_report.generate":
                structured_output = add_legacy_monthly_report(
                    structured_output,
                    task.input_payload_json or {},
                )
                result["structured"] = structured_output
            synced_items = await self._sync_result_items(task, structured_output)
            if synced_items:
                result["synced_items"] = synced_items
            structured_output_required = bool(definition.output_schema_name) or definition.handler == "pipeline"
            if structured_output is None and structured_output_required:
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
                if definition.handler == "pipeline":
                    raise ValueError("Pipeline final output is not valid JSON.")
            elif structured_output is not None:
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
                    if definition.handler == "pipeline":
                        raise ValueError("Pipeline final output failed its registered schema.")
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
                stream_semantics="status",
                source={"type": "task_manager", "id": task.id},
            )
            task = await self._finish_task(
                task.id,
                status="succeeded",
                result=result,
                outcome=terminal_outcome or "success",
            )
            yield TaskEventRead.model_validate(succeeded)
        except RunLeaseLost:
            raise
        except Exception as exc:
            error = {
                "type": exc.__class__.__name__,
                "stage": "task_manager",
                "message": str(exc),
                "retryable": True,
            }
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
            task = await self._finish_task(task.id, status="failed", error=error, outcome="failure")
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
            stage_run_id=event.stage_run_id,
            agent_id=event.agent_id,
            tool_call_id=event.tool_call_id,
            stream_semantics=event.stream_semantics,
            source=event.source,
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
        stage_run_id: str | None = None,
        agent_id: str | None = None,
        tool_call_id: str | None = None,
        stream_semantics: str = "status",
        source: dict[str, Any] | None = None,
    ) -> TaskEventEntity:
        lease = current_execution_lease()
        if lease is not None:
            lease.assert_active()
            if run_id != lease.run_id:
                raise RunLeaseLost(f"Execution lease for Run '{lease.run_id}' cannot write an event without its run id.")
        async with create_db_session() as session:
            if run_id is not None:
                run_statement = (
                    select(TaskRunEntity)
                    .where(TaskRunEntity.id == run_id)
                    .with_for_update()
                )
                run = (await session.exec(run_statement)).first()
                if run is None:
                    raise ValueError(f"Run '{run_id}' not found.")
                await verify_execution_lease(session, run_id, run=run)
                run.next_event_sequence = (run.next_event_sequence or 0) + 1
                sequence = run.next_event_sequence
                session.add(run)
            else:
                statement = select(TaskEventEntity).where(TaskEventEntity.task_id == task_id)
                statement = statement.where(TaskEventEntity.run_id.is_(None))
                latest = (await session.exec(statement.order_by(desc(TaskEventEntity.sequence)).limit(1))).first()
                sequence = (latest.sequence + 1) if latest else 1
            event = TaskEventEntity(
                task_id=task_id,
                run_id=run_id,
                parent_event_id=parent_event_id,
                sequence=sequence,
                event_type=event_type,
                level=level,
                stage=stage,
                step_id=step_id,
                step_index=step_index,
                item_id=item_id,
                stage_run_id=stage_run_id,
                agent_id=agent_id,
                tool_call_id=tool_call_id,
                stream_semantics=stream_semantics,
                source_json=source or {},
                duration_ms=duration_ms,
                token_usage_json=token_usage or {},
                error_code=error_code,
                visible=visible,
                message=message,
                payload_json=_bounded_event_payload(payload or {}),
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
        if run_id:
            try:
                await get_event_broker().publish(run_id, event_to_envelope(event))
            except Exception as exc:  # Event persistence remains authoritative if the live broker is unavailable.
                logger.warning("Task event broker publish failed for run {}: {}", run_id, exc)
        return event

    def _get_handler(self, definition: TaskType):
        if definition.handler == "scheduler":
            return SchedulerTaskHandler(self.options)
        if definition.handler == "batch_item_scheduler":
            return BatchItemSchedulerHandler(self.options)
        if definition.handler == "pipeline":
            return PipelineTaskHandler(self.options)
        raise ValueError(f"Unsupported task handler '{definition.handler}'.")

    async def _prepare_run(
        self,
        task_id: str,
        request: TaskRunRequest | None,
        *,
        request_fingerprint: str = "",
    ) -> TaskEntity:
        async with create_db_session() as session:
            statement = select(TaskEntity).where(TaskEntity.id == task_id).with_for_update()
            task = (await session.exec(statement)).first()
            if task is None:
                raise ValueError(f"Task '{task_id}' not found.")
            if task.status == "running":
                if request and request.idempotency_key and task.current_run_id:
                    existing_run = await session.get(TaskRunEntity, task.current_run_id)
                    if (
                        existing_run is not None
                        and existing_run.idempotency_key == request.idempotency_key
                    ):
                        expected_fingerprint = request_fingerprint or build_run_request_fingerprint(
                            service_name=task.service,
                            task_id=task.id,
                            tenant_id=task.tenant_id,
                            user_id=request.user_id or task.user_id,
                            request=request,
                        )
                        assert_fingerprint_matches(
                            stored_fingerprint=existing_run.request_fingerprint,
                            request_fingerprint=expected_fingerprint,
                            resource="run",
                            idempotency_key=request.idempotency_key,
                        )
                        return task
                raise ValueError(f"Task '{task_id}' is already running.")
            definition = get_task_definition(task.task_type)
            run_fingerprint = request_fingerprint or (
                build_run_request_fingerprint(
                    service_name=task.service,
                    task_id=task.id,
                    tenant_id=task.tenant_id,
                    user_id=request.user_id or task.user_id,
                    request=request,
                )
                if request and request.idempotency_key
                else ""
            )

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
            pipeline_id = definition.pipeline_id or ""
            pipeline_version = ""
            if pipeline_id:
                pipeline_version = get_pipeline_definition(pipeline_id).version
            run = TaskRunEntity(
                id=task.current_run_id,
                task_id=task.id,
                idempotency_key=request.idempotency_key if request else None,
                request_fingerprint=run_fingerprint,
                pipeline_id=pipeline_id,
                pipeline_version=pipeline_version,
                status="running",
                started_at=task.started_at,
                metadata_json={"request_metadata": dict(request.metadata_patch) if request else {}},
            )
            session.add(task)
            session.add(run)
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
            if task.current_run_id:
                await verify_execution_lease(session, task.current_run_id)
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
            if task.current_run_id:
                await verify_execution_lease(session, task.current_run_id)
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
        outcome: str | None = None,
    ) -> TaskEntity:
        async with create_db_session() as session:
            task = await session.get(TaskEntity, task_id)
            if task is None:
                raise ValueError(f"Task '{task_id}' not found.")
            if task.current_run_id:
                await verify_execution_lease(session, task.current_run_id)
            task.status = status
            task.result_payload_json = result
            task.error_payload_json = error
            now = utc_now()
            if status == "succeeded":
                task.progress_current = task.progress_total or 1
            task.finished_at = None if status == "waiting_human" else now
            task.updated_at = now
            session.add(task)
            if task.current_run_id:
                run = await session.get(TaskRunEntity, task.current_run_id)
                if run is not None:
                    run.status = status
                    run.outcome = outcome
                    run.error_code = str((error or {}).get("type") or "") or None
                    run.error_message = str((error or {}).get("message") or "")
                    run.finished_at = None if status == "waiting_human" else now
                    run.updated_at = now
                    session.add(run)
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


def _bounded_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) <= MAX_EVENT_PAYLOAD_CHARS:
        return payload
    return {
        "truncated": True,
        "original_chars": len(encoded),
        "preview": encoded[: MAX_EVENT_PAYLOAD_CHARS // 2],
    }


def _definition_snapshot(definition: TaskType) -> dict[str, Any]:
    return asdict(definition)


async def _prepare_input_payload(task_type: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    if task_type == "analytics.douyin.account_report.generate":
        return await enrich_douyin_account_report_payload(dict(input_payload or {}))
    return input_payload


async def _wait_for_run_status(
    run_id: str,
    terminal_statuses: set[str],
    timeout_seconds: float = 10,
) -> TaskRunEntity:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        run = await get_run(run_id)
        if run is None:
            raise ValueError(f"Run '{run_id}' not found.")
        if run.status in terminal_statuses:
            return run
        await asyncio.sleep(0.03)
    raise ValueError(f"Run '{run_id}' did not finish approval within {timeout_seconds:g} seconds.")


async def _find_task_by_idempotency(
    *,
    user_id: str,
    tenant_id: str,
    idempotency_key: str,
) -> TaskEntity | None:
    async with create_db_session() as session:
        result = await session.exec(
            select(TaskEntity)
            .where(TaskEntity.user_id == user_id)
            .where(TaskEntity.tenant_id == tenant_id)
            .where(TaskEntity.idempotency_key == idempotency_key)
        )
        return result.first()


def _build_script_revision_payload(
    *,
    proposal_task: TaskEntity,
    proposal_run: TaskRunEntity,
    proposal_artifact_id: str,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    source = dict(proposal_task.input_payload_json or {})
    current_script = dict(source.get("current_script") or {})
    topic_card = dict(source.get("topic_card") or {})
    constraints = dict(source.get("user_constraints") or {})
    persona = dict(source.get("persona") or {})
    topic = str(
        topic_card.get("topic_name")
        or topic_card.get("title")
        or current_script.get("topic_name")
        or "Script revision"
    )
    persona_name = str(persona.get("display_name") or persona.get("name") or "") or None
    return {
        "topic": topic,
        "topic_card": topic_card,
        "platform": constraints.get("platform") or "douyin",
        "duration_seconds": constraints.get("duration_seconds") or current_script.get("duration_seconds") or 60,
        "persona": persona_name,
        "account_persona": persona_name,
        "manual_direction": proposal.get("summary") or "Apply the approved script change proposal.",
        "parent_script_id": source.get("base_script_id"),
        "conversation_thread_id": source.get("conversation_thread_id"),
        "revision_mode": True,
        "base_script_id": source.get("base_script_id"),
        "base_artifact_id": source.get("base_artifact_id"),
        "proposal_artifact_id": proposal_artifact_id,
        "previous_script": current_script,
        "change_proposal": proposal,
        "preserve_fields": proposal.get("preserve_fields") or [],
        "parent_task_id": proposal_task.id,
        "context": {
            "proposal_task_id": proposal_task.id,
            "proposal_run_id": proposal_run.id,
            "proposal_artifact_id": proposal_artifact_id,
        },
    }


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


def event_to_envelope(event: TaskEventEntity) -> dict[str, Any]:
    return {
        "schema_version": event.schema_version,
        "event_id": event.id,
        "task_id": event.task_id,
        "run_id": event.run_id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "stream_semantics": event.stream_semantics,
        "stage_id": event.stage,
        "stage_run_id": event.stage_run_id,
        "agent_id": event.agent_id,
        "tool_call_id": event.tool_call_id,
        "source": event.source_json or {},
        "payload": event.payload_json or {},
        "message": event.message,
        "level": event.level,
        "created_at": event.created_at.isoformat() + "Z",
    }


def item_to_read(item: TaskItemEntity) -> TaskItemRead:
    return TaskItemRead.model_validate(item)
