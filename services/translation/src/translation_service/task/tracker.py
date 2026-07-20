from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import desc
from sqlmodel import select

from db.db_context import create_db_session
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.artifact_service import TaskArtifactPublisher, resolve_artifact_path
from task_manager.models import (
    TaskArtifactEntity,
    TaskEntity,
    TaskRunEntity,
    utc_now,
)
from task_manager.pipeline.store import create_stage_run, update_stage_run
from task_manager.registry import TaskType, register_task_definition
from task_manager.schemas import TaskCreateRequest
from task_manager.service import TaskManagerService

from translation_service.config import Settings
from translation_service.context import InternalRequestContext, require_idempotency_key
from translation_service.domain.models import (
    ArtifactData,
    TranslationLanguage,
    TranslationStage,
    TranslationTaskData,
    TranslationTaskType,
)
from translation_service.errors import TranslationError

TASK_TYPE_TEXT = "translation.text"
TASK_TYPE_FILE = "translation.file"
TASK_SOURCE = "translation-service"


def register_translation_task_types() -> None:
    register_task_definition(
        TaskType(
            task_type=TASK_TYPE_TEXT,
            name="Text Translation",
            description="Translate text with the configured base model.",
            handler="external",
            default_agent_id="translation-service",
        ),
        source=TASK_SOURCE,
    )
    register_task_definition(
        TaskType(
            task_type=TASK_TYPE_FILE,
            name="File Translation",
            description="Translate PDF, DOC or DOCX while preserving an exportable file.",
            handler="external",
            default_agent_id="translation-service",
        ),
        source=TASK_SOURCE,
    )


class TranslationTaskTracker:
    def __init__(self, settings: Settings) -> None:
        options = SchedulingRuntimeOptions(
            local_python_artifact_dir=settings.artifact_root,
            local_python_work_dir=settings.temp_root,
        )
        self._settings = settings
        self._manager = TaskManagerService(options)

    async def create_text_task(
        self,
        *,
        context: InternalRequestContext,
        source_language: TranslationLanguage,
        target_language: TranslationLanguage,
        source_sha256: str,
        source_character_count: int,
    ) -> tuple[TaskEntity, bool]:
        return await self._create_task(
            context=context,
            task_type=TASK_TYPE_TEXT,
            title="Text translation",
            input_payload={
                "task_type": TranslationTaskType.TEXT.value,
                "source_language": source_language.value,
                "target_language": target_language.value,
                "source_sha256": source_sha256,
                "source_character_count": source_character_count,
            },
        )

    async def create_file_task(
        self,
        *,
        context: InternalRequestContext,
        source_language: TranslationLanguage,
        target_language: TranslationLanguage,
        source_sha256: str,
        source_size: int,
        source_file_name: str,
        source_file_type: str,
        output_format: str,
    ) -> tuple[TaskEntity, bool]:
        return await self._create_task(
            context=context,
            task_type=TASK_TYPE_FILE,
            title=f"Translate {source_file_name[:120]}",
            input_payload={
                "task_type": TranslationTaskType.FILE.value,
                "source_language": source_language.value,
                "target_language": target_language.value,
                "source_sha256": source_sha256,
                "source_size": source_size,
                "source_file_name": source_file_name,
                "source_file_type": source_file_type,
                "output_format": output_format,
            },
        )

    async def _create_task(
        self,
        *,
        context: InternalRequestContext,
        task_type: str,
        title: str,
        input_payload: dict,
    ) -> tuple[TaskEntity, bool]:
        existing = await self._find_by_idempotency(context)
        if existing is not None:
            if existing.task_type != task_type:
                raise TranslationError(
                    "IDEMPOTENCY_CONFLICT",
                    "Idempotency key already belongs to another translation task type",
                    status_code=409,
                )
            if (existing.input_payload_json or {}).get("source_sha256") != input_payload.get(
                "source_sha256"
            ):
                raise TranslationError(
                    "IDEMPOTENCY_CONFLICT",
                    "Idempotency key was reused for different source content",
                    status_code=409,
                )
            return existing, False
        task = await self._manager.create_task(
            TaskCreateRequest(
                task_type=task_type,
                idempotency_key=require_idempotency_key(context),
                title=title,
                input_payload=input_payload,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                stream=False,
                metadata={
                    "request_id": context.request_id,
                    "dept_id": context.dept_id,
                    "stage": TranslationStage.VALIDATING.value,
                    "progress": 0,
                    "content_retained": False,
                },
            )
        )
        return task, True

    async def begin(self, task: TaskEntity) -> TaskEntity:
        return await self._manager.begin_external_task(
            task.id, user_id=task.user_id, stream=False
        )

    @asynccontextmanager
    async def stage(
        self,
        task: TaskEntity,
        stage: TranslationStage,
        *,
        progress: int,
    ) -> AsyncIterator[str]:
        if task.current_run_id is None:
            raise TranslationError(
                "TASK_RUN_MISSING", "Translation task does not have an active run"
            )
        started = time.monotonic()
        stage_run = await create_stage_run(
            task_id=task.id,
            run_id=task.current_run_id,
            stage_id=stage.value.lower(),
            stage_type="external",
            attempt=task.attempt_count,
            agent_id="translation-service",
            input_artifact_ids=[],
        )
        await self.update_progress(task.id, stage=stage, progress=progress)
        await self._manager.record_event(
            task_id=task.id,
            run_id=task.current_run_id,
            event_type="stage_started",
            stage=stage.value,
            message=f"{stage.value} started.",
            stage_run_id=stage_run.id,
            source={"type": "translation_service", "id": "translation-service"},
        )
        try:
            yield stage_run.id
        except asyncio.CancelledError:
            duration_ms = int((time.monotonic() - started) * 1000)
            await update_stage_run(
                stage_run.id,
                status="interrupted",
                error_code="PROCESS_INTERRUPTED",
                error_message="Translation execution was interrupted",
                duration_ms=duration_ms,
                finished_at=utc_now(),
            )
            await self._manager.record_event(
                task_id=task.id,
                run_id=task.current_run_id,
                event_type="stage_interrupted",
                stage=stage.value,
                level="error",
                message=f"{stage.value} was interrupted.",
                error_code="PROCESS_INTERRUPTED",
                stage_run_id=stage_run.id,
                duration_ms=duration_ms,
                source={"type": "translation_service", "id": "translation-service"},
            )
            raise
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            error_code = (
                exc.code if isinstance(exc, TranslationError) else "UNEXPECTED_ERROR"
            )
            await update_stage_run(
                stage_run.id,
                status="failed",
                error_code=error_code,
                error_message=(
                    exc.message
                    if isinstance(exc, TranslationError)
                    else "Unexpected translation stage failure"
                ),
                duration_ms=duration_ms,
                finished_at=utc_now(),
            )
            await self._manager.record_event(
                task_id=task.id,
                run_id=task.current_run_id,
                event_type="stage_failed",
                stage=stage.value,
                level="error",
                message=f"{stage.value} failed.",
                error_code=error_code,
                stage_run_id=stage_run.id,
                duration_ms=duration_ms,
                source={"type": "translation_service", "id": "translation-service"},
            )
            raise
        else:
            duration_ms = int((time.monotonic() - started) * 1000)
            await update_stage_run(
                stage_run.id,
                status="succeeded",
                duration_ms=duration_ms,
                finished_at=utc_now(),
            )
            await self._manager.record_event(
                task_id=task.id,
                run_id=task.current_run_id,
                event_type="stage_succeeded",
                stage=stage.value,
                message=f"{stage.value} completed.",
                stage_run_id=stage_run.id,
                duration_ms=duration_ms,
                source={"type": "translation_service", "id": "translation-service"},
            )

    async def notice(
        self,
        task: TaskEntity,
        event_type: str,
        subject: str,
        reason: str,
    ) -> None:
        await self._manager.record_event(
            task_id=task.id,
            run_id=task.current_run_id,
            event_type=event_type.lower(),
            stage=TranslationStage.REBUILDING.value,
            level="warning",
            message=reason,
            payload={"subject": subject},
            source={"type": "translation_service", "id": "translation-service"},
        )

    async def update_progress(
        self,
        task_id: str,
        *,
        stage: TranslationStage,
        progress: int,
        metadata: dict | None = None,
    ) -> None:
        bounded_progress = max(0, min(100, progress))
        async with create_db_session() as session:
            task = await session.get(TaskEntity, task_id)
            if task is None:
                raise TranslationError("TASK_NOT_FOUND", "Translation task was not found")
            task.progress_total = 100
            task.progress_current = bounded_progress
            task.metadata_json = {
                **(task.metadata_json or {}),
                **(metadata or {}),
                "stage": stage.value,
                "progress": bounded_progress,
            }
            task.updated_at = utc_now()
            session.add(task)
            await session.commit()

    async def publish_artifact(
        self,
        *,
        task: TaskEntity,
        stage_run_id: str,
        source_path: Path,
        sequence: int,
        output_type: str,
        download_name: str,
        mime_type: str,
        size: int,
        sha256: str,
    ) -> ArtifactData:
        if task.current_run_id is None:
            raise TranslationError("TASK_RUN_MISSING", "Translation task run is missing")
        publisher = TaskArtifactPublisher(
            root=self._settings.artifact_root,
            task_id=task.id,
            run_id=task.current_run_id,
            stage_run_id=stage_run_id,
        )
        reference = await publisher.publish(
            source_path, sequence=sequence, mime=mime_type
        )
        async with create_db_session() as session:
            artifact = await session.get(TaskArtifactEntity, reference.id)
            if artifact is None:
                raise TranslationError(
                    "ARTIFACT_PUBLISH_FAILED", "Published artifact record was not found"
                )
            if artifact.checksum != sha256:
                raise TranslationError(
                    "ARTIFACT_INTEGRITY_MISMATCH",
                    "Published artifact checksum does not match the generated file",
                )
            artifact.metadata_json = {
                **(artifact.metadata_json or {}),
                "download_name": download_name,
                "output_type": output_type,
                "size": size,
                "sha256": sha256,
            }
            session.add(artifact)
            await session.commit()
        return ArtifactData(
            artifact_id=reference.id,
            output_type=output_type,
            file_name=download_name,
            mime_type=mime_type,
            size=size,
            sha256=sha256,
        )

    async def complete(
        self,
        task: TaskEntity,
        *,
        result_metadata: dict,
    ) -> TaskEntity:
        await self.update_progress(
            task.id,
            stage=(
                TranslationStage.OUTPUT_READY
                if task.task_type == TASK_TYPE_FILE
                else TranslationStage.COMPLETED
            ),
            progress=100,
            metadata=result_metadata,
        )
        return await self._manager.complete_external_task(
            task.id,
            result={
                **result_metadata,
                "content_retained": False,
            },
        )

    async def fail(self, task_id: str, error: Exception) -> TaskEntity:
        if isinstance(error, TranslationError):
            code = error.code
            message = error.message
            retryable = error.retryable
        else:
            code = "UNEXPECTED_TRANSLATION_ERROR"
            message = "Unexpected translation failure"
            retryable = True
        async with create_db_session() as session:
            task = await session.get(TaskEntity, task_id)
            if task is None:
                raise TranslationError("TASK_NOT_FOUND", "Translation task was not found")
            now = utc_now()
            task.status = "failed"
            task.error_payload_json = {
                "code": code,
                "message": message[:500],
                "retryable": retryable,
            }
            task.metadata_json = {
                **(task.metadata_json or {}),
                "stage": TranslationStage.FAILED.value,
                "progress": task.progress_current,
            }
            task.finished_at = now
            task.updated_at = now
            session.add(task)
            if task.current_run_id:
                run = await session.get(TaskRunEntity, task.current_run_id)
                if run is not None:
                    run.status = "failed"
                    run.outcome = "failure"
                    run.error_code = code
                    run.error_message = message[:500]
                    run.finished_at = now
                    run.updated_at = now
                    session.add(run)
            await session.commit()
            await session.refresh(task)
        await self._manager.record_event(
            task_id=task.id,
            run_id=task.current_run_id,
            event_type="task_failed",
            stage=TranslationStage.FAILED.value,
            level="error",
            message="Translation task failed.",
            error_code=code,
            payload={"retryable": retryable},
            source={"type": "translation_service", "id": "translation-service"},
        )
        return task

    async def interrupt(self, task_id: str) -> TaskEntity:
        async with create_db_session() as session:
            task = await session.get(TaskEntity, task_id)
            if task is None:
                raise TranslationError("TASK_NOT_FOUND", "Translation task was not found")
            now = utc_now()
            task.status = "interrupted"
            task.error_payload_json = {
                "code": "PROCESS_INTERRUPTED",
                "message": "Translation execution was interrupted",
                "retryable": True,
            }
            task.metadata_json = {
                **(task.metadata_json or {}),
                "stage": TranslationStage.FAILED.value,
            }
            task.finished_at = now
            task.updated_at = now
            session.add(task)
            if task.current_run_id:
                run = await session.get(TaskRunEntity, task.current_run_id)
                if run is not None:
                    run.status = "interrupted"
                    run.outcome = "failure"
                    run.error_code = "PROCESS_INTERRUPTED"
                    run.error_message = "Translation execution was interrupted"
                    run.finished_at = now
                    run.updated_at = now
                    session.add(run)
            await session.commit()
            await session.refresh(task)
        await self._manager.record_event(
            task_id=task.id,
            run_id=task.current_run_id,
            event_type="task_interrupted",
            stage=TranslationStage.FAILED.value,
            level="error",
            message="Translation execution was interrupted.",
            error_code="PROCESS_INTERRUPTED",
            payload={"retryable": True},
            source={"type": "translation_service", "id": "translation-service"},
        )
        return task

    async def active_file_task_count(self) -> int:
        async with create_db_session() as session:
            result = await session.exec(
                select(TaskEntity).where(
                    TaskEntity.task_type == TASK_TYPE_FILE,
                    TaskEntity.status.in_(["created", "running"]),
                )
            )
            return len(list(result.all()))

    async def get_owned_task(
        self, task_id: str, context: InternalRequestContext
    ) -> TaskEntity:
        task = await self._manager.get_task(task_id)
        if (
            task is None
            or task.task_type not in {TASK_TYPE_TEXT, TASK_TYPE_FILE}
            or task.user_id != context.user_id
            or task.tenant_id != context.tenant_id
        ):
            raise TranslationError(
                "TASK_NOT_FOUND", "Translation task was not found", status_code=404
            )
        return task

    async def to_data(self, task: TaskEntity) -> TranslationTaskData:
        input_payload = task.input_payload_json or {}
        result = task.result_payload_json or {}
        metadata = task.metadata_json or {}
        artifacts = await self.list_artifacts(task)
        task_type = (
            TranslationTaskType.TEXT
            if task.task_type == TASK_TYPE_TEXT
            else TranslationTaskType.FILE
        )
        status = task.status.upper()
        if task.status == "succeeded" and task_type is TranslationTaskType.FILE:
            status = "OUTPUT_READY"
        error = task.error_payload_json or {}
        detected = result.get("detected_source_language") or metadata.get(
            "detected_source_language"
        )
        return TranslationTaskData(
            task_id=task.id,
            run_id=task.current_run_id,
            task_type=task_type,
            status=status,
            stage=str(metadata.get("stage") or TranslationStage.VALIDATING.value),
            progress=int(metadata.get("progress") or task.progress_current or 0),
            source_language=TranslationLanguage(input_payload["source_language"]),
            detected_source_language=(
                TranslationLanguage(detected) if detected else None
            ),
            target_language=TranslationLanguage(input_payload["target_language"]),
            source_sha256=str(input_payload.get("source_sha256") or ""),
            source_character_count=_optional_int(
                result.get("source_character_count")
                or input_payload.get("source_character_count")
            ),
            result_sha256=result.get("result_sha256"),
            result_character_count=_optional_int(result.get("result_character_count")),
            content_retained=False,
            artifacts=artifacts,
            error_code=error.get("code"),
            error_message=error.get("message"),
            retryable=bool(error.get("retryable")),
            created_at=task.created_at.isoformat(),
            updated_at=task.updated_at.isoformat(),
        )

    async def list_artifacts(self, task: TaskEntity) -> list[ArtifactData]:
        async with create_db_session() as session:
            result = await session.exec(
                select(TaskArtifactEntity)
                .where(TaskArtifactEntity.task_id == task.id)
                .order_by(TaskArtifactEntity.created_at)
            )
            rows = list(result.all())
        artifacts: list[ArtifactData] = []
        for row in rows:
            metadata = row.metadata_json or {}
            artifacts.append(
                ArtifactData(
                    artifact_id=row.id,
                    output_type=str(metadata.get("output_type") or row.artifact_type),
                    file_name=str(metadata.get("download_name") or row.summary),
                    mime_type=str(metadata.get("mime") or "application/octet-stream"),
                    size=int(metadata.get("size") or 0),
                    sha256=str(metadata.get("sha256") or row.checksum),
                )
            )
        return artifacts

    async def resolve_owned_artifact(
        self,
        artifact_id: str,
        context: InternalRequestContext,
    ) -> tuple[Path, ArtifactData]:
        async with create_db_session() as session:
            artifact = await session.get(TaskArtifactEntity, artifact_id)
        if artifact is None:
            raise TranslationError(
                "ARTIFACT_NOT_FOUND", "Translation artifact was not found", status_code=404
            )
        task = await self.get_owned_task(artifact.task_id, context)
        if task.status != "succeeded" or not artifact.content_uri:
            raise TranslationError(
                "ARTIFACT_NOT_READY", "Translation artifact is not ready", status_code=409
            )
        metadata = artifact.metadata_json or {}
        data = ArtifactData(
            artifact_id=artifact.id,
            output_type=str(metadata.get("output_type") or artifact.artifact_type),
            file_name=str(metadata.get("download_name") or artifact.summary),
            mime_type=str(metadata.get("mime") or "application/octet-stream"),
            size=int(metadata.get("size") or 0),
            sha256=str(metadata.get("sha256") or artifact.checksum),
        )
        return (
            resolve_artifact_path(self._settings.artifact_root, artifact.content_uri),
            data,
        )

    async def interrupt_active_tasks(self) -> int:
        async with create_db_session() as session:
            result = await session.exec(
                select(TaskEntity).where(
                    TaskEntity.task_type.in_([TASK_TYPE_TEXT, TASK_TYPE_FILE]),
                    TaskEntity.status.in_(["created", "running"]),
                )
            )
            tasks = list(result.all())
            now = utc_now()
            for task in tasks:
                task.status = "interrupted"
                task.error_payload_json = {
                    "code": "PROCESS_INTERRUPTED",
                    "message": "Translation service restarted before the task completed",
                    "retryable": True,
                }
                task.metadata_json = {
                    **(task.metadata_json or {}),
                    "stage": TranslationStage.FAILED.value,
                }
                task.finished_at = now
                task.updated_at = now
                session.add(task)
                if task.current_run_id:
                    run = await session.get(TaskRunEntity, task.current_run_id)
                    if run is not None:
                        run.status = "interrupted"
                        run.outcome = "failure"
                        run.error_code = "PROCESS_INTERRUPTED"
                        run.error_message = "Translation service restarted"
                        run.finished_at = now
                        run.updated_at = now
                        session.add(run)
            await session.commit()
        for task in tasks:
            await self._manager.record_event(
                task_id=task.id,
                run_id=task.current_run_id,
                event_type="task_interrupted",
                stage=TranslationStage.FAILED.value,
                level="error",
                message="Translation service restarted before the task completed.",
                error_code="PROCESS_INTERRUPTED",
                payload={"retryable": True},
                source={"type": "translation_service", "id": "translation-service"},
            )
        return len(tasks)

    async def _find_by_idempotency(
        self, context: InternalRequestContext
    ) -> TaskEntity | None:
        async with create_db_session() as session:
            result = await session.exec(
                select(TaskEntity)
                .where(TaskEntity.user_id == context.user_id)
                .where(TaskEntity.tenant_id == context.tenant_id)
                .where(TaskEntity.idempotency_key == require_idempotency_key(context))
                .order_by(desc(TaskEntity.created_at))
                .limit(1)
            )
            return result.first()


def _optional_int(value) -> int | None:
    return int(value) if value is not None else None
