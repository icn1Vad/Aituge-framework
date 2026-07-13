from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from common.system_constants import DEFAULT_TENANT_ID
from scheduling.scheduler import SchedulingRuntimeOptions

from .schemas import MainAgentChatRequest, ScriptWorkspaceCreateRequest, ScriptWorkspaceUpdateRequest
from .service import MainAgentService
from .store import ManagedSingleAgentStore, ScriptWorkspaceStore


def create_main_agent_router(options: SchedulingRuntimeOptions) -> APIRouter:
    router = APIRouter(tags=["main-agent"])

    @router.get("/agents/delegatable")
    async def delegatable_agents():
        return {"agents": await MainAgentService(options).list_delegatable_agents()}

    @router.post("/script-workspaces")
    async def create_workspace(request: ScriptWorkspaceCreateRequest):
        row = await ScriptWorkspaceStore().create(
            script_text=request.script_text,
            storyboard_text=request.storyboard_text,
            user_id=request.user_id,
            tenant_id=DEFAULT_TENANT_ID,
        )
        return {"workspace": row.to_read_model()}

    @router.get("/script-workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str, user_id: str = Query(default="default_user")):
        row = await ScriptWorkspaceStore().get(
            workspace_id,
            user_id=user_id,
            tenant_id=DEFAULT_TENANT_ID,
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"Script workspace '{workspace_id}' not found.")
        return {"workspace": row.to_read_model()}

    @router.put("/script-workspaces/{workspace_id}")
    async def update_workspace(workspace_id: str, request: ScriptWorkspaceUpdateRequest):
        try:
            row = await ScriptWorkspaceStore().update(
                workspace_id,
                script_text=request.script_text,
                storyboard_text=request.storyboard_text,
                user_id=request.user_id,
                tenant_id=DEFAULT_TENANT_ID,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"workspace": row.to_read_model()}

    @router.get("/main/managed-agents")
    async def managed_agents(
        primary_session_id: str = Query(min_length=1),
        user_id: str = Query(default="default_user"),
    ):
        rows = await ManagedSingleAgentStore().list(
            primary_session_id=primary_session_id,
            user_id=user_id,
            tenant_id=DEFAULT_TENANT_ID,
        )
        return {"managed_agents": [row.to_read_model() for row in rows]}

    @router.post("/main/chat")
    async def main_agent_chat(request: MainAgentChatRequest):
        try:
            return await MainAgentService(options).chat(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
