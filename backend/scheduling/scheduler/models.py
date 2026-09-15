from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class SchedulingRuntimeOptions(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    local_python_artifact_dir: Path
    local_python_work_dir: Path | None = None
    rag_store: Any = None


class SchedulingChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: Optional[list[dict]] = None
    message: Optional[str] = None
    model: Optional[str] = None
    thread_id: Optional[str] = None
    session_id: Optional[str] = None
    user_id: str = "default_user"
    stream: bool = False
    attachments: list[dict[str, str]] = Field(default_factory=list)
    extra_tools: list[str] = Field(default_factory=list)
    skill_package: Optional[str] = None
    extra_datasets: list[str] = Field(default_factory=list)
