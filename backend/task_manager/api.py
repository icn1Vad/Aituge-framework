from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from db.db_context import create_db_session
from scheduling.scheduler import SchedulingRuntimeOptions

from .access import TaskAccessContext, assert_can_access_task, task_access_context
from .idempotency import IdempotencyConflictError
from .artifact_service import resolve_artifact_path
from .conversation_service import TaskConversationService
from .memory import TaskMemoryMaterial, TaskMemoryService
from .registry import list_task_definitions
from .pipeline.registry import list_pipeline_definitions
from .runtime import get_event_broker
from .runtime.quota import describe_resource_wait
from .schemas import (
    HumanReviewRequest,
    ScriptChangeApplyRequest,
    ScriptChangeApplyResponse,
    StageRetryRequest,
    TaskArtifactRead,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskDefinitionRead,
    TaskEventRead,
    TaskMemoryCompressRequest,
    TaskMemoryRead,
    TaskRunRead,
    TaskRunRequest,
    TaskRunResponse,
    TaskRunStartResponse,
    TaskStageRunRead,
)
from .service import TaskManagerService, event_to_envelope, event_to_read, item_to_read, task_to_read


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


_STREAM_DONE = object()
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _uses_persistent_worker() -> bool:
    return os.environ.get("TASK_EXECUTION_MODE", "inline").strip().lower() == "worker"


def _should_enqueue_run(handler_name: str) -> bool:
    return handler_name == "pipeline" or _uses_persistent_worker()


def _track_background(coroutine) -> asyncio.Task:
    task = asyncio.create_task(coroutine)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task
def _task_manager_http_error(exc: ValueError) -> HTTPException:
    if isinstance(exc, IdempotencyConflictError):
        return HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        )
    return HTTPException(status_code=400, detail=str(exc))


def _scope_task_create_request(
    request: TaskCreateRequest,
    context: TaskAccessContext,
    **updates,
) -> TaskCreateRequest:
    scoped_updates = {
        "user_id": context.user_id,
        "tenant_id": context.tenant_id,
        **updates,
    }
    if context.model_pack_id:
        scoped_updates["model_pack_id"] = context.model_pack_id
    return request.model_copy(update=scoped_updates)



async def _run_task_to_queue(
    service: TaskManagerService,
    task_id: str,
    request: TaskRunRequest,
    queue: asyncio.Queue,
) -> None:
    try:
        async for event in service.stream_task(task_id, request):
            await queue.put(event)
    except Exception as exc:
        await queue.put(exc)
    finally:
        await queue.put(_STREAM_DONE)


async def _queued_sse(queue: asyncio.Queue, task_id: str):
    while True:
        item = await queue.get()
        if item is _STREAM_DONE:
            return
        if isinstance(item, Exception):
            payload = json.dumps(
                {"task_id": task_id, "message": str(item), "type": item.__class__.__name__},
                ensure_ascii=False,
            )
            yield f"event: task_failed\ndata: {payload}\n\n"
            continue
        yield f"event: {item.event_type}\ndata: {item.model_dump_json()}\n\n"


def create_task_manager_router(options: SchedulingRuntimeOptions) -> APIRouter:
    router = APIRouter(prefix="/task-manager", tags=["task-manager"])

    @router.get("/definitions")
    async def definitions():
        return {
            "definitions": [
                TaskDefinitionRead(
                    task_type=item.task_type,
                    name=item.name,
                    description=item.description,
                    required_task_key=item.required_task_key,
                    handler=item.handler,
                    default_agent_id=item.default_agent_id,
                    default_skill_package=item.default_skill_package,
                    default_primary_skill=item.default_primary_skill,
                    default_candidate_skills=item.default_candidate_skills,
                    default_tools=item.default_tools,
                    default_datasets=item.default_datasets,
                    input_schema_name=item.input_schema_name,
                    output_schema_name=item.output_schema_name,
                    item_output_schema_name=item.item_output_schema_name,
                    pipeline_id=item.pipeline_id,
                    stream_chunk_chars=item.stream_chunk_chars,
                    conversation_message_field=item.conversation_message_field,
                    resource_pool=getattr(item, "resource_pool", None),
                    access_mode=getattr(item, "access_mode", None),
                )
                for item in list_task_definitions()
            ]
        }

    @router.get("/pipelines")
    async def pipelines():
        return {
            "pipelines": [
                {
                    "pipeline_id": item.pipeline_id,
                    "version": item.version,
                    "task_type": item.task_type,
                    "description": item.description,
                    "final_artifact_type": item.final_artifact_type,
                    "resumable": item.resumable,
                    "max_parallelism": item.max_parallelism,
                    "stages": [
                        {
                            "stage_id": stage.stage_id,
                            "name": stage.name,
                            "stage_type": stage.stage_type,
                            "depends_on": list(stage.depends_on),
                            "input_schema": stage.input_schema,
                            "output_schema": stage.output_schema,
                            "artifact_type": stage.artifact_type,
                            "failure_policy": stage.failure_policy,
                            "agent_id": (
                                stage.agent_config.agent_id
                                if stage.agent_config
                                else stage.batch_config.agent_id if stage.batch_config else None
                            ),
                        }
                        for stage in item.ordered_stages()
                    ],
                }
                for item in list_pipeline_definitions()
            ]
        }

    @router.get("/memories/{task_key}")
    async def get_task_memory(
        task_key: str,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        try:
            row = await TaskMemoryService(options).get_latest(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                task_key=task_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"memory": TaskMemoryRead.model_validate(row) if row else None}

    @router.post("/memories/{task_key}/compress")
    async def compress_task_memory(
        task_key: str,
        request: TaskMemoryCompressRequest,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        try:
            row = await TaskMemoryService(options).consolidate(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                task_key=task_key,
                materials=[
                    TaskMemoryMaterial(
                        kind="manual",
                        content=request.new_information,
                        source={"type": "manual_api"},
                    )
                ],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"memory": TaskMemoryRead.model_validate(row)}

    @router.post("/tasks", response_model=TaskCreateResponse)
    async def create_task(
        request: TaskCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        context: TaskAccessContext = Depends(task_access_context),
    ):
        try:
            scoped_request = _scope_task_create_request(
                request,
                context,
                idempotency_key=idempotency_key or request.idempotency_key,
            )
            task = await TaskManagerService(options).create_task(
                scoped_request,
                service_name=context.service_name,
            )
            return TaskCreateResponse(task=task_to_read(task))
        except ValueError as exc:
            raise _task_manager_http_error(exc) from exc

    @router.get("/tasks")
    async def tasks(
        user_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        scoped_user_id = user_id if context.is_admin else context.user_id
        scoped_tenant_id = None if context.is_admin else context.tenant_id
        rows = await service.list_tasks(
            user_id=scoped_user_id,
            tenant_id=scoped_tenant_id,
            limit=limit,
            offset=offset,
        )
        return {"tasks": [task_to_read(row) for row in rows]}

    @router.get("/conversations")
    async def conversations(
        task_type: str = Query(min_length=1),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        context: TaskAccessContext = Depends(task_access_context),
    ):
        try:
            rows, has_more = await TaskConversationService().list_conversations(
                task_type=task_type,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "conversations": rows,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "has_more": has_more,
            },
        }

    @router.get("/conversations/{thread_id}")
    async def conversation(
        thread_id: str,
        task_type: str = Query(min_length=1),
        context: TaskAccessContext = Depends(task_access_context),
    ):
        try:
            result = await TaskConversationService().get_conversation(
                thread_id=thread_id,
                task_type=task_type,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "conversation_not_found",
                    "message": "Task conversation was not found.",
                },
            )
        return result

    @router.get("/tasks/{task_id}")
    async def task(
        task_id: str,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        row = await TaskManagerService(options).get_task(task_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        assert_can_access_task(row, context)
        return {"task": task_to_read(row)}

    @router.get("/tasks/{task_id}/events")
    async def task_events(
        task_id: str,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        task_row = await service.get_task(task_id)
        if task_row is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        assert_can_access_task(task_row, context)
        rows = await service.list_events(task_id, limit=limit, offset=offset)
        return {"events": [event_to_read(row) for row in rows]}

    @router.get("/tasks/{task_id}/items")
    async def task_items(
        task_id: str,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        task_row = await service.get_task(task_id)
        if task_row is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        assert_can_access_task(task_row, context)
        rows = await service.list_items(task_id, limit=limit, offset=offset)
        return {"items": [item_to_read(row) for row in rows]}

    @router.post("/tasks/{task_id}/run", response_model=TaskRunResponse)
    async def run_task(
        task_id: str,
        request: TaskRunRequest | None = None,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        events: list[TaskEventRead] = []
        try:
            task_row = await service.get_task(task_id)
            if task_row is None:
                raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
            assert_can_access_task(task_row, context)
            run_request = request or TaskRunRequest(stream=False)
            run_request = run_request.model_copy(update={"user_id": context.user_id})
            async for event in service.stream_task(task_id, run_request):
                events.append(event)
        except ValueError as exc:
            raise _task_manager_http_error(exc) from exc
        task = await service.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        return TaskRunResponse(task=task_to_read(task), events=events)

    @router.post("/tasks/{task_id}/runs", response_model=TaskRunStartResponse)
    async def start_run(
        task_id: str,
        request: TaskRunRequest | None = None,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        task_row = await service.get_task(task_id)
        if task_row is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        assert_can_access_task(task_row, context)
        try:
            base_request = request or TaskRunRequest()
            run_request = base_request.model_copy(
                update={
                    "user_id": context.user_id,
                    "idempotency_key": idempotency_key or base_request.idempotency_key,
                }
            )
            run = await service.start_task_run(task_id, run_request)
        except ValueError as exc:
            raise _task_manager_http_error(exc) from exc
        return TaskRunStartResponse(
            task_id=task_id,
            run_id=run.id,
            status=run.status,
            stream_url=f"/task-manager/runs/{run.id}/events/stream",
        )

    @router.get("/runs/{run_id}")
    async def get_run(
        run_id: str,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        run = await service.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
        task_row = await service.get_task(run.task_id)
        if task_row is None:
            raise HTTPException(status_code=404, detail=f"Task '{run.task_id}' not found.")
        assert_can_access_task(task_row, context)
        async with create_db_session() as session:
            execution_state, blocking_reader_count = await describe_resource_wait(session, run)
        view = TaskRunRead.model_validate(run).model_copy(
            update={
                "execution_state": execution_state,
                "blocking_reader_count": blocking_reader_count,
            }
        )
        return {"run": view}

    @router.get("/runs/{run_id}/stages")
    async def run_stages(
        run_id: str,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service, _ = await _authorized_run(options, run_id, context)
        rows = await service.list_run_stages(run_id)
        return {"stages": [TaskStageRunRead.model_validate(row) for row in rows]}

    @router.get("/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=1000, ge=1, le=5000),
        event_type: str | None = None,
        stage_id: str | None = None,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service, _ = await _authorized_run(options, run_id, context)
        rows = await service.list_run_events(
            run_id,
            after_sequence=after_sequence,
            limit=limit,
            event_type=event_type,
            stage_id=stage_id,
        )
        return {"events": [event_to_envelope(row) for row in rows]}

    @router.get("/runs/{run_id}/events/stream")
    async def run_event_stream(
        run_id: str,
        after_sequence: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service, run = await _authorized_run(options, run_id, context)
        if last_event_id and ":" in last_event_id:
            try:
                after_sequence = max(after_sequence, int(last_event_id.rsplit(":", 1)[1]))
            except ValueError:
                pass

        return StreamingResponse(
            _stream_run_sse(service, run, after_sequence),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    @router.get("/tasks/{task_id}/artifacts")
    async def task_artifacts(
        task_id: str,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        task_row = await service.get_task(task_id)
        if task_row is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        assert_can_access_task(task_row, context)
        rows = await service.list_task_artifacts(task_id)
        return {"artifacts": [TaskArtifactRead.model_validate(row) for row in rows]}

    @router.get("/artifacts/{artifact_id}")
    async def artifact(
        artifact_id: str,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        row = await service.get_artifact(artifact_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Artifact '{artifact_id}' not found.")
        task_row = await service.get_task(row.task_id)
        if task_row is None:
            raise HTTPException(status_code=404, detail=f"Task '{row.task_id}' not found.")
        assert_can_access_task(task_row, context)
        return {"artifact": TaskArtifactRead.model_validate(row)}

    @router.get("/artifacts/{artifact_id}/content")
    async def artifact_content(
        artifact_id: str,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        row = await service.get_artifact(artifact_id)
        if row is None or not row.content_uri:
            raise HTTPException(status_code=404, detail=f"Artifact '{artifact_id}' content was not found.")
        task_row = await service.get_task(row.task_id)
        if task_row is None:
            raise HTTPException(status_code=404, detail=f"Task '{row.task_id}' not found.")
        assert_can_access_task(task_row, context)
        try:
            path = resolve_artifact_path(options.local_python_artifact_dir, row.content_uri)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"Artifact '{artifact_id}' content was not found.")
        metadata = row.metadata_json or {}
        return FileResponse(
            path,
            media_type=str(metadata.get("mime") or "application/octet-stream"),
            filename=str(metadata.get("name") or path.name),
            content_disposition_type="inline",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(
        run_id: str,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service, _ = await _authorized_run(options, run_id, context)
        return {"run": TaskRunRead.model_validate(await service.request_cancel(run_id))}

    @router.post("/runs/{run_id}/review")
    async def review_run(
        run_id: str,
        request: HumanReviewRequest,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service, _ = await _authorized_run(options, run_id, context)
        try:
            run = await service.submit_human_review(
                run_id,
                action=request.action,
                comment=request.comment,
                patch=request.patch,
                resume_from_stage=request.resume_from_stage,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"run": TaskRunRead.model_validate(run)}

    @router.post("/runs/{run_id}/apply", response_model=ScriptChangeApplyResponse)
    async def apply_script_change(
        run_id: str,
        request: ScriptChangeApplyRequest,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service, _ = await _authorized_run(options, run_id, context)
        try:
            proposal_task, revision_task, revision_run = await service.apply_script_change_proposal(
                run_id,
                proposal_artifact_id=request.proposal_artifact_id,
                comment=request.comment,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ScriptChangeApplyResponse(
            proposal_task_id=proposal_task.id,
            proposal_run_id=run_id,
            proposal_artifact_id=str(
                (revision_task.metadata_json or {}).get("proposal_artifact_id")
                or request.proposal_artifact_id
            ),
            revision_task_id=revision_task.id,
            revision_run_id=revision_run.id,
            status=revision_run.status,
            stream_url=f"/task-manager/runs/{revision_run.id}/events/stream",
        )

    @router.post("/runs/{run_id}/stages/{stage_id}/retry")
    async def retry_stage(
        run_id: str,
        stage_id: str,
        request: StageRetryRequest | None = None,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service, _ = await _authorized_run(options, run_id, context)
        try:
            run = await service.submit_human_review(
                run_id,
                action="rerun_stage",
                comment=(request.comment if request else ""),
                resume_from_stage=stage_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"run": TaskRunRead.model_validate(run)}

    @router.post("/tasks/{task_id}/stream")
    async def stream_task(
        task_id: str,
        request: TaskRunRequest | None = None,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        task_row = await service.get_task(task_id)
        if task_row is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        assert_can_access_task(task_row, context)
        run_request = (request or TaskRunRequest(stream=True)).model_copy(update={"user_id": context.user_id})
        if _should_enqueue_run(task_row.handler_name):
            run = await service.start_task_run(task_id, run_request)
            return StreamingResponse(
                _stream_run_sse(service, run),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        queue: asyncio.Queue = asyncio.Queue()
        _track_background(_run_task_to_queue(service, task_id, run_request, queue))
        return StreamingResponse(
            _queued_sse(queue, task_id),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    @router.post("/run", response_model=TaskRunResponse)
    async def create_and_run(
        request: TaskCreateRequest,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        try:
            scoped_request = _scope_task_create_request(request, context)
            task = await service.create_task(scoped_request, service_name=context.service_name)
            events: list[TaskEventRead] = []
            async for event in service.stream_task(task.id, TaskRunRequest(stream=False, user_id=context.user_id)):
                events.append(event)
            task = await service.get_task(task.id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task disappeared after run.")
            return TaskRunResponse(task=task_to_read(task), events=events)
        except ValueError as exc:
            raise _task_manager_http_error(exc) from exc

    @router.post("/stream")
    async def create_and_stream(
        request: TaskCreateRequest,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        try:
            scoped_request = _scope_task_create_request(request, context)
            task = await service.create_task(scoped_request, service_name=context.service_name)
        except ValueError as exc:
            raise _task_manager_http_error(exc) from exc

        created = TaskEventRead.model_validate((await service.list_events(task.id, limit=1))[0])
        queue: asyncio.Queue | None = None
        if not _should_enqueue_run(task.handler_name):
            queue = asyncio.Queue()
            _track_background(
                _run_task_to_queue(
                    service,
                    task.id,
                    TaskRunRequest(stream=True, user_id=context.user_id),
                    queue,
                )
            )

        async def event_stream():
            try:
                yield (
                    "event: task_created\n"
                    f"data: {created.model_dump_json()}\n\n"
                )
                if _should_enqueue_run(task.handler_name):
                    run = await service.start_task_run(
                        task.id,
                        TaskRunRequest(stream=True, user_id=context.user_id),
                    )
                    async for chunk in _stream_run_sse(service, run):
                        yield chunk
                    return
                assert queue is not None
                async for block in _queued_sse(queue, task.id):
                    yield block
            except Exception as exc:
                payload = json.dumps(
                    {"message": str(exc), "type": exc.__class__.__name__},
                    ensure_ascii=False,
                )
                yield (
                    "event: task_failed\n"
                    f"data: {payload}\n\n"
                )

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    return router


async def _authorized_run(options, run_id: str, context: TaskAccessContext):
    service = TaskManagerService(options)
    run = await service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    task_row = await service.get_task(run.task_id)
    if task_row is None:
        raise HTTPException(status_code=404, detail=f"Task '{run.task_id}' not found.")
    assert_can_access_task(task_row, context)
    return service, run


async def _stream_run_sse(service: TaskManagerService, run, after_sequence: int = 0):
    run_id = run.id
    last_sequence = after_sequence
    broker = get_event_broker()
    async with broker.subscribe(run_id) as live:
        replay = await service.list_run_events(run_id, after_sequence=last_sequence, limit=5000)
        for row in replay:
            envelope = event_to_envelope(row)
            last_sequence = max(last_sequence, row.sequence)
            yield _sse(envelope)
        latest = await service.get_run(run_id)
        if latest is None or latest.status in {"succeeded", "failed", "cancelled", "waiting_human"}:
            return

        pending = asyncio.create_task(anext(live))
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=10)
                if not done:
                    missed = await service.list_run_events(
                        run_id,
                        after_sequence=last_sequence,
                        limit=5000,
                    )
                    for row in missed:
                        envelope = event_to_envelope(row)
                        last_sequence = max(last_sequence, row.sequence)
                        yield _sse(envelope)
                    latest = await service.get_run(run_id)
                    if latest is None or latest.status in {"succeeded", "failed", "cancelled", "waiting_human"}:
                        return
                    yield _sse(
                        {
                            "schema_version": "1.0",
                            "event_id": None,
                            "task_id": run.task_id,
                            "run_id": run_id,
                            "sequence": last_sequence,
                            "event_type": "heartbeat",
                            "stream_semantics": "heartbeat",
                            "stage_id": latest.current_stage_id,
                            "payload": {"run_status": latest.status},
                        },
                        include_id=False,
                    )
                    continue
                envelope = pending.result()
                pending = asyncio.create_task(anext(live))
                sequence = int(envelope.get("sequence") or 0)
                if sequence <= last_sequence:
                    continue
                last_sequence = sequence
                yield _sse(envelope)
                if envelope.get("event_type") in {
                    "task_succeeded",
                    "task_failed",
                    "task_cancelled",
                    "human_review_required",
                }:
                    return
        finally:
            pending.cancel()


def _sse(envelope: dict, *, include_id: bool = True) -> str:
    lines = []
    if include_id and envelope.get("run_id") and envelope.get("sequence") is not None:
        lines.append(f"id: {envelope['run_id']}:{envelope['sequence']}")
    lines.append(f"event: {envelope.get('event_type') or 'message'}")
    lines.append(f"data: {json.dumps(envelope, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"
