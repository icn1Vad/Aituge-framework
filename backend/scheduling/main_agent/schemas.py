from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class IndexedStoryboardShotUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_index: int = Field(ge=0)
    replacement: StoryboardShot


class ScriptStoryboardWorkspaceUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_text: str = Field(min_length=1)
    storyboard_updates: list[IndexedStoryboardShotUpdate] = Field(
        min_length=1,
        max_length=3,
    )
    role_id: str = ""
    strategy_id: str = ""
    template_id: str = ""
    script_type_id: str = ""
    script_example_ids: list[str] = Field(default_factory=list)
    risk_rule_ids: list[str] = Field(default_factory=list)
    replace_reason: str = ""

    @model_validator(mode="after")
    def validate_unique_shot_indexes(self):
        indexes = [item.shot_index for item in self.storyboard_updates]
        if len(indexes) != len(set(indexes)):
            raise ValueError("storyboard_updates must use unique shot_index values.")
        return self


class ScriptWorkspaceCreateRequest(BaseModel):
    script_text: str = ""
    storyboard_text: str = ""
    user_id: str = "default_user"


class ScriptWorkspaceUpdateRequest(BaseModel):
    script_text: str | None = None
    storyboard_text: str | None = None
    user_id: str = "default_user"
