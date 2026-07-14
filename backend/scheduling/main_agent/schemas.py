from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from scheduling.scheduler import SchedulingChatRequest


class MainAgentChatRequest(SchedulingChatRequest):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str | None = None
    task_id: str | None = None


class ScriptWorkspaceCreateRequest(BaseModel):
    script_text: str = ""
    storyboard_text: str = ""
    user_id: str = "default_user"


class ScriptWorkspaceUpdateRequest(BaseModel):
    script_text: str | None = None
    storyboard_text: str | None = None
    user_id: str = "default_user"
