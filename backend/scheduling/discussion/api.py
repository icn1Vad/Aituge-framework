from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from scheduling.scheduler import SchedulingRuntimeOptions

from .schemas import DiscussionRunCreateRequest, DiscussionUserMessageRequest
from .service import DiscussionService


def create_discussion_router(options: SchedulingRuntimeOptions) -> APIRouter:
    router = APIRouter(prefix="/discussions", tags=["discussion"])

    @router.post("/runs")
    async def create_run(request: DiscussionRunCreateRequest):
        service = DiscussionService(options)
        try:
            run = await service.create_run(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if request.stream:
            async def event_stream():
                async for item in service.stream_run(run.id):
                    yield (
                        f"event: {item['event']}\n"
                        f"data: {json.dumps(item['data'], ensure_ascii=False, default=str)}\n\n"
                    )

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        return await service.execute_run(run.id)

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str):
        try:
            return await DiscussionService(options).get_run_payload(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/runs/{run_id}/messages")
    async def add_user_message(run_id: str, request: DiscussionUserMessageRequest):
        try:
            message = await DiscussionService(options).add_user_message(
                run_id=run_id,
                content=request.content,
                user_id=request.user_id,
            )
            return {"message": message}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
