from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class SchedulingRuntimeOptions(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    local_python_artifact_dir: Path
    artifact_base_url: str = "/tool-artifacts/local-python"
    rag_store: Any = None


class SchedulingChatRequest(BaseModel):
    messages: Optional[list[dict]] = None
    message: Optional[str] = None
    model: Optional[str] = None
    thread_id: Optional[str] = None
    session_id: Optional[str] = None
    user_id: str = "default_user"
    stream: bool = False
    primary_skill: Optional[str] = None
    candidate_skills: Optional[list[str]] = None
    extra_tools: list[str] = []
    extra_skills: list[str] = []
    extra_datasets: list[str] = []

