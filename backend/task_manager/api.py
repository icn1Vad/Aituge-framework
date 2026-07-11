from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from scheduling.scheduler import SchedulingRuntimeOptions

from .access import TaskAccessContext, assert_can_access_task, task_access_context
from .registry import list_task_definitions
from .pipeline.registry import list_pipeline_definitions
from .runtime import get_event_broker
from .schemas import (
    HumanReviewRequest,
    StageRetryRequest,
    TaskArtifactRead,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskDefinitionRead,
    TaskEventRead,
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
                            "agent_id": stage.agent_config.agent_id if stage.agent_config else None,
                        }
                        for stage in item.ordered_stages()
                    ],
                }
                for item in list_pipeline_definitions()
            ]
        }

    @router.post("/tasks", response_model=TaskCreateResponse)
    async def create_task(
        request: TaskCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        context: TaskAccessContext = Depends(task_access_context),
    ):
        try:
            scoped_request = request.model_copy(
                update={
                    "user_id": context.user_id,
                    "tenant_id": context.tenant_id,
                    "idempotency_key": idempotency_key or request.idempotency_key,
                }
            )
            task = await TaskManagerService(options).create_task(scoped_request)
            return TaskCreateResponse(task=task_to_read(task))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        return {"run": TaskRunRead.model_validate(run)}

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

        async def event_stream():
            try:
                task_row = await service.get_task(task_id)
                if task_row is None:
                    raise ValueError(f"Task '{task_id}' not found.")
                assert_can_access_task(task_row, context)
                run_request = request or TaskRunRequest(stream=True)
                run_request = run_request.model_copy(update={"user_id": context.user_id})
                if task_row.handler_name == "pipeline":
                    run = await service.start_task_run(task_id, run_request)
                    async for chunk in _stream_run_sse(service, run):
                        yield chunk
                    return
                async for event in service.stream_task(task_id, run_request):
                    yield f"event: {event.event_type}\ndata: {event.model_dump_json()}\n\n"
            except Exception as exc:
                payload = json.dumps(
                    {"task_id": task_id, "message": str(exc), "type": exc.__class__.__name__},
                    ensure_ascii=False,
                )
                yield (
                    "event: task_failed\n"
                    f"data: {payload}\n\n"
                )

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    @router.post("/run", response_model=TaskRunResponse)
    async def create_and_run(
        request: TaskCreateRequest,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)
        try:
            scoped_request = request.model_copy(update={"user_id": context.user_id, "tenant_id": context.tenant_id})
            task = await service.create_task(scoped_request)
            events: list[TaskEventRead] = []
            async for event in service.stream_task(task.id, TaskRunRequest(stream=False, user_id=context.user_id)):
                events.append(event)
            task = await service.get_task(task.id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task disappeared after run.")
            return TaskRunResponse(task=task_to_read(task), events=events)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/stream")
    async def create_and_stream(
        request: TaskCreateRequest,
        context: TaskAccessContext = Depends(task_access_context),
    ):
        service = TaskManagerService(options)

        async def event_stream():
            try:
                scoped_request = request.model_copy(update={"user_id": context.user_id, "tenant_id": context.tenant_id})
                task = await service.create_task(scoped_request)
                yield (
                    "event: task_created\n"
                    f"data: {TaskEventRead.model_validate((await service.list_events(task.id, limit=1))[0]).model_dump_json()}\n\n"
                )
                if task.handler_name == "pipeline":
                    run = await service.start_task_run(
                        task.id,
                        TaskRunRequest(stream=True, user_id=context.user_id),
                    )
                    async for chunk in _stream_run_sse(service, run):
                        yield chunk
                    return
                async for event in service.stream_task(task.id, TaskRunRequest(stream=True, user_id=context.user_id)):
                    yield f"event: {event.event_type}\ndata: {event.model_dump_json()}\n\n"
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
