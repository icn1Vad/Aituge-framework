from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from common.system_constants import DEFAULT_TENANT_ID


TaskStatus = str


class TaskCreateRequest(BaseModel):
    task_type: str
    parent_task_id: Optional[str] = None
    root_task_id: Optional[str] = None
    task_key: Optional[str] = None
    title: str = ""
    input_payload: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    user_id: str = "default_user"
    tenant_id: str = DEFAULT_TENANT_ID
    stream: bool = True
    agent_id: Optional[str] = None
    thread_id: Optional[str] = None
    session_id: Optional[str] = None
    priority: int = 0
    expires_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskRunRequest(BaseModel):
    stream: Optional[bool] = None
    user_id: Optional[str] = None
    input_patch: dict[str, Any] = Field(default_factory=dict)
    metadata_patch: dict[str, Any] = Field(default_factory=dict)


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    parent_task_id: Optional[str] = None
    root_task_id: Optional[str] = None
    task_key: Optional[str] = None
    task_type: str
    status: TaskStatus
    title: str
    handler_name: str
    input_payload_json: dict[str, Any]
    result_payload_json: Optional[dict[str, Any]] = None
    error_payload_json: Optional[dict[str, Any]] = None
    definition_snapshot_json: dict[str, Any]
    output_schema_json: dict[str, Any]
    agent_id: str
    thread_id: Optional[str] = None
    session_id: Optional[str] = None
    user_id: str
    tenant_id: str
    stream_mode: bool
    current_run_id: Optional[str] = None
    attempt_count: int
    progress_current: int
    progress_total: int
    cancel_requested: bool
    priority: int
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    updated_at: datetime
    metadata_json: dict[str, Any]


class TaskEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    run_id: Optional[str] = None
    parent_event_id: Optional[str] = None
    sequence: int
    event_type: str
    level: str
    stage: str
    step_id: Optional[str] = None
    step_index: Optional[int] = None
    item_id: Optional[str] = None
    duration_ms: Optional[int] = None
    token_usage_json: dict[str, Any]
    error_code: Optional[str] = None
    visible: bool
    message: str
    payload_json: dict[str, Any]
    created_at: datetime


class TaskItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    run_id: Optional[str] = None
    item_type: str
    item_key: str
    sequence: int
    status: str
    input_payload_json: dict[str, Any]
    result_payload_json: Optional[dict[str, Any]] = None
    error_payload_json: Optional[dict[str, Any]] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: datetime


class TaskCreateResponse(BaseModel):
    task: TaskRead


class TaskRunResponse(BaseModel):
    task: TaskRead
    events: list[TaskEventRead] = Field(default_factory=list)


class TaskDefinitionRead(BaseModel):
    task_type: str
    name: str
    description: str = ""
    handler: str
    default_agent_id: str
    default_primary_skill: Optional[str] = None
    default_candidate_skills: list[str] = Field(default_factory=list)
    default_tools: list[str] = Field(default_factory=list)
    default_datasets: list[str] = Field(default_factory=list)
    input_schema_name: Optional[str] = None
    output_schema_name: Optional[str] = None
    item_output_schema_name: Optional[str] = None
