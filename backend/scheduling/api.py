from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from db.db_context import create_db_session
from skill import ensure_default_skill_packages
from .agent_registry import (
    ensure_default_agent_profiles,
    get_agent_profile,
    list_agent_profiles,
)
from .discussion import create_discussion_router
from .scheduler import SchedulingChatRequest, SchedulingRuntimeOptions, SchedulingService


def create_scheduling_router(options: SchedulingRuntimeOptions) -> APIRouter:
    router = APIRouter(prefix="/scheduling", tags=["scheduling"])
    router.include_router(create_discussion_router(options))

    @router.get("/agents")
    async def agents():
        async with create_db_session() as session:
            await ensure_default_skill_packages(session)
            await ensure_default_agent_profiles(session)
            profiles = await list_agent_profiles(session)
        return {"agents": [profile.to_read_model() for profile in profiles]}

    @router.get("/agents/{agent_id}")
    async def agent(agent_id: str):
        async with create_db_session() as session:
            await ensure_default_skill_packages(session)
            await ensure_default_agent_profiles(session)
            profile = await get_agent_profile(session, agent_id)
        if profile is None or not profile.enabled:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
        return {"agent": profile.to_read_model()}

    @router.post("/agents/{agent_id}/chat")
    async def chat(agent_id: str, request: SchedulingChatRequest):
        async with create_db_session() as session:
            await ensure_default_skill_packages(session)
            await ensure_default_agent_profiles(session)
            profile = await get_agent_profile(session, agent_id)
        if profile is None or not profile.enabled:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
        if profile.agent_type != "single":
            raise HTTPException(
                status_code=400,
                detail=f"Agent type '{profile.agent_type}' is not runnable by the single scheduler.",
            )

        service = SchedulingService(options)
        if request.stream:
            async def event_stream():
                async for event in service.stream_chat(profile, request):
                    yield (
                        f"event: {event.event}\n"
                        f"data: {event.model_dump_json(exclude_none=True)}\n\n"
                    )

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        try:
            return await service.chat(profile, request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
