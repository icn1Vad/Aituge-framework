"""Durable attachment jobs run through the Framework TaskManager/Worker."""
import asyncio
import os
import time
from loguru import logger
from sqlmodel import select
from db.db_context import create_db_session
from .models import FileEntity, FileUploadSessionEntity
from .service.file_resource_service import FileResourceService
from .support import FileStatus

_options = None

def enabled():
    return os.getenv("AITUGE_ATTACHMENTS_ENABLED", "false").lower() in {"true", "1", "yes"}

def configure(options):
    global _options
    _options = options

def register_tasks():
    from task_manager.registry import TaskType, register_task_definition
    for name in ("parse", "gc"):
        register_task_definition(TaskType(task_type=f"attachments.{name}", name=f"Attachments {name}", handler="attachment"), source="attachments")

async def schedule_parse(entity):
    from task_manager.service import TaskManagerService
    from task_manager.schemas import TaskCreateRequest, TaskRunRequest
    from scheduling.scheduler import SchedulingRuntimeOptions
    register_tasks()
    key = f"parse:{entity.id}:{entity.updated_at.isoformat()}"
    service = TaskManagerService(_options or SchedulingRuntimeOptions(local_python_artifact_dir=__import__("pathlib").Path("localdata/artifacts")))
    task = await service.create_task(
        TaskCreateRequest(task_type="attachments.parse", tenant_id=entity.tenant_id,
                          input_payload={"file_id": entity.id}, idempotency_key=key, stream=False),
        service_name="attachments")
    await service.start_task_run(task.id, TaskRunRequest(idempotency_key=key))
    return task

async def process_file(file_id, tenant_id):
    from .service.content_extractor import extract_text_from_bytes, EXTRACTOR_VERSION, chunk_text, should_chunk
    async with create_db_session() as session:
        svc = FileResourceService(session)
        row = await svc.get_file(file_id, tenant_id)
        if row is None or row.status == "succeeded":
            return {"file_id": file_id, "skipped": True}
        await svc.mark_status(file_id=file_id, tenant_id=tenant_id, status=FileStatus.parsing)
        try:
            stream = await svc.read_bytes(file_id, tenant_id)
            raw = stream.read()
            result = await asyncio.to_thread(extract_text_from_bytes, raw, row.file_extension,
                                             file_name=row.file_name, tenant_id=tenant_id)
            if result is None:
                if not (row.mime_type or "").startswith(("image/", "video/")):
                    raise ValueError("暂不支持该文件格式")
                result = ("", False)
            content, truncated = result
            await svc.write_text_content(file_id=file_id, tenant_id=tenant_id, content=content,
                                         extractor_version=EXTRACTOR_VERSION, truncated_at_extract=truncated)
            await svc.replace_chunks(file_id=file_id, tenant_id=tenant_id,
                                     chunks=chunk_text(content) if should_chunk(len(content)) else [])
            await svc.mark_status(file_id=file_id, tenant_id=tenant_id, status=FileStatus.succeeded)
            return {"file_id": file_id, "characters": len(content), "truncated_at_extract": truncated}
        except Exception as exc:
            # No-text photos remain available to the visual tool; connection/format
            # failures still have an explicit warning and are never invented text.
            from .extraction_models import DocumentExtractionError
            if (row.mime_type or "").startswith("image/") and isinstance(exc, DocumentExtractionError) and "未识别到可读取的文字" in str(exc):
                await svc.write_text_content(file_id=file_id, tenant_id=tenant_id, content="",
                                             extractor_version=EXTRACTOR_VERSION)
                await svc.mark_status(file_id=file_id, tenant_id=tenant_id, status=FileStatus.succeeded)
                return {"file_id": file_id, "characters": 0, "visual_available": True}
            await svc.mark_status(file_id=file_id, tenant_id=tenant_id, status=FileStatus.failed, failed_reason=str(exc))
            raise

async def cleanup_expired():
    from datetime import datetime, timezone
    from .service.upload_session_service import UploadSessionService
    deleted = 0
    async with create_db_session() as session:
        svc = FileResourceService(session)
        for row in await svc.sweep_expired_candidates():
            if await svc.hard_delete(row.id, row.tenant_id):
                deleted += 1
        rows = (await session.exec(select(FileUploadSessionEntity).where(
            FileUploadSessionEntity.status == "active",
            FileUploadSessionEntity.expires_at < datetime.now(timezone.utc).replace(tzinfo=None)).limit(200))).all()
        uploads = UploadSessionService(session)
        for row in rows:
            await uploads.cancel(upload_id=row.id, tenant_id=row.tenant_id)
            row.status = "expired"
            session.add(row)
        await session.commit()
    return {"deleted_files": deleted}

class AttachmentTaskHandler:
    async def stream(self, *, context):
        from task_manager.handlers.base import TaskHandlerEvent
        task = context.task
        result = (await cleanup_expired() if task.task_type == "attachments.gc" else
                  await process_file(task.input_payload_json["file_id"], task.tenant_id))
        yield TaskHandlerEvent(event_type="completed", stage="attachment", message="Attachment job completed",
                               final_content="completed", structured_output=result, terminal_status="succeeded")

async def maintenance_loop():
    from task_manager.service import TaskManagerService
    from task_manager.schemas import TaskCreateRequest, TaskRunRequest
    from scheduling.scheduler import SchedulingRuntimeOptions
    while True:
        try:
            # Reconcile durable pending rows if an API process died between the
            # file commit and queue submission. The same key reuses the same job.
            async with create_db_session() as session:
                rows = (await session.exec(select(FileEntity).where(FileEntity.status == "pending").limit(200))).all()
                for row in rows:
                    await schedule_parse(row)
            service = TaskManagerService(_options or SchedulingRuntimeOptions(local_python_artifact_dir=__import__("pathlib").Path("localdata/artifacts")))
            key = f"gc:{int(time.time()) // 300}"
            task = await service.create_task(
                TaskCreateRequest(task_type="attachments.gc", idempotency_key=key, stream=False),
                service_name="attachments")
            await service.start_task_run(task.id, TaskRunRequest(idempotency_key=key))
        except Exception:
            logger.exception("Attachment maintenance submission failed; retrying")
        await asyncio.sleep(30)
