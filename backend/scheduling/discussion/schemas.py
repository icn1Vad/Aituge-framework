from __future__ import annotations

from pydantic import BaseModel, Field


class DiscussionRunCreateRequest(BaseModel):
    topic: str
    participant_agent_ids: list[str] = Field(
        default_factory=lambda: [
            "default-single-agent",
            "media-writer-agent",
            "media-storyboard-agent",
        ]
    )
    moderator_agent_id: str | None = None
    user_id: str = "default_user"
    max_rounds: int = 1
    stream: bool = False


class DiscussionUserMessageRequest(BaseModel):
    content: str
    user_id: str = "default_user"
