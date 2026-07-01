from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from scheduling.scheduler import SchedulingRuntimeOptions

from .registry import list_task_definitions
from .schemas import (
    TaskCreateRequest,
    TaskCreateResponse,
    TaskDefinitionRead,
    TaskEventRead,
    TaskRunRequest,
    TaskRunResponse,
)
from .service import TaskManagerService, event_to_read, item_to_read, task_to_read


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
                    default_primary_skill=item.default_primary_skill,
                    default_candidate_skills=item.default_candidate_skills,
                    default_tools=item.default_tools,
                    default_datasets=item.default_datasets,
                )
                for item in list_task_definitions()
            ]
        }

    @router.post("/tasks", response_model=TaskCreateResponse)
    async def create_task(request: TaskCreateRequest):
        try:
            task = await TaskManagerService(options).create_task(request)
            return TaskCreateResponse(task=task_to_read(task))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/tasks")
    async def tasks(
        user_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        service = TaskManagerService(options)
        rows = await service.list_tasks(user_id=user_id, limit=limit, offset=offset)
        return {"tasks": [task_to_read(row) for row in rows]}

    @router.get("/tasks/{task_id}")
    async def task(task_id: str):
        row = await TaskManagerService(options).get_task(task_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        return {"task": task_to_read(row)}

    @router.get("/tasks/{task_id}/events")
    async def task_events(
        task_id: str,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ):
        service = TaskManagerService(options)
        if await service.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        rows = await service.list_events(task_id, limit=limit, offset=offset)
        return {"events": [event_to_read(row) for row in rows]}

    @router.get("/tasks/{task_id}/items")
    async def task_items(
        task_id: str,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ):
        service = TaskManagerService(options)
        if await service.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        rows = await service.list_items(task_id, limit=limit, offset=offset)
        return {"items": [item_to_read(row) for row in rows]}

    @router.post("/tasks/{task_id}/run", response_model=TaskRunResponse)
    async def run_task(task_id: str, request: TaskRunRequest | None = None):
        service = TaskManagerService(options)
        events: list[TaskEventRead] = []
        try:
            async for event in service.stream_task(task_id, request or TaskRunRequest(stream=False)):
                events.append(event)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        task = await service.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
        return TaskRunResponse(task=task_to_read(task), events=events)

    @router.post("/tasks/{task_id}/stream")
    async def stream_task(task_id: str, request: TaskRunRequest | None = None):
        service = TaskManagerService(options)

        async def event_stream():
            try:
                async for event in service.stream_task(task_id, request or TaskRunRequest(stream=True)):
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

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @router.post("/run", response_model=TaskRunResponse)
    async def create_and_run(request: TaskCreateRequest):
        service = TaskManagerService(options)
        try:
            task = await service.create_task(request)
            events: list[TaskEventRead] = []
            async for event in service.stream_task(task.id, TaskRunRequest(stream=False)):
                events.append(event)
            task = await service.get_task(task.id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task disappeared after run.")
            return TaskRunResponse(task=task_to_read(task), events=events)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/stream")
    async def create_and_stream(request: TaskCreateRequest):
        service = TaskManagerService(options)

        async def event_stream():
            try:
                task = await service.create_task(request)
                yield (
                    "event: task_created\n"
                    f"data: {TaskEventRead.model_validate((await service.list_events(task.id, limit=1))[0]).model_dump_json()}\n\n"
                )
                async for event in service.stream_task(task.id, TaskRunRequest(stream=True)):
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

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return router
