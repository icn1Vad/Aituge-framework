from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scheduling.scheduler import SchedulingChatRequest


class MainAgentChatRequest(SchedulingChatRequest):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str | None = None
    task_id: str | None = None


class StoryboardShot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time: str = Field(min_length=1)
    scene: str = Field(min_length=1)
    shot: str = Field(min_length=1)
    action: str = Field(min_length=1)
    voiceover: str = Field(min_length=1)
    subtitle_focus: str = Field(min_length=1)
    visual_prompt: str = Field(min_length=1)


class StoryboardWorkspacePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storyboard: list[StoryboardShot] = Field(min_length=1)
    storyboard_plan: dict[str, Any]
    visual_direction: list[str] | str
    warnings: list[str]


class ScriptWorkspaceCreateRequest(BaseModel):
    script_text: str = ""
    storyboard_text: str = ""
    user_id: str = "default_user"


class ScriptWorkspaceUpdateRequest(BaseModel):
    script_text: str | None = None
    storyboard_text: str | None = None
    user_id: str = "default_user"
