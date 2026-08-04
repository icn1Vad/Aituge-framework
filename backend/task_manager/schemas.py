from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.system_constants import DEFAULT_TENANT_ID


TaskStatus = str


class TaskCreateRequest(BaseModel):
    task_type: str
    parent_task_id: Optional[str] = None
    root_task_id: Optional[str] = None
    task_key: Optional[str] = None
    idempotency_key: Optional[str] = None
    title: str = ""
    input_payload: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    user_id: str = "default_user"
    tenant_id: str = DEFAULT_TENANT_ID
    stream: bool = True
    agent_id: Optional[str] = None
    model_pack_id: Optional[str] = Field(default=None, max_length=120)
    thread_id: Optional[str] = None
    session_id: Optional[str] = None
    priority: int = 0
    expires_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("model_pack_id")
    @classmethod
    def normalize_model_pack_id(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @model_validator(mode="before")
    @classmethod
    def accept_v1_api_aliases(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "input_payload" not in normalized and "input" in normalized:
            normalized["input_payload"] = normalized["input"]
        if "metadata" not in normalized and "client_context" in normalized:
            normalized["metadata"] = normalized["client_context"]
        return normalized


class TaskRunRequest(BaseModel):
    stream: Optional[bool] = None
    user_id: Optional[str] = None
    input_patch: dict[str, Any] = Field(default_factory=dict)
    metadata_patch: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = None


class TaskMemoryCompressRequest(BaseModel):
    new_information: str = Field(min_length=1, max_length=8000)


class TaskMemoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_key: str
    version: int
    content: str
    created_at: datetime


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    parent_task_id: Optional[str] = None
    root_task_id: Optional[str] = None
    task_key: Optional[str] = None
    idempotency_key: Optional[str] = None
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
    model_pack_id: str
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
    schema_version: str = "1.0"
    parent_event_id: Optional[str] = None
    sequence: int
    event_type: str
    level: str
    stage: str
    step_id: Optional[str] = None
    step_index: Optional[int] = None
    item_id: Optional[str] = None
    stage_run_id: Optional[str] = None
    agent_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    stream_semantics: str = "status"
    source_json: dict[str, Any] = Field(default_factory=dict)
    duration_ms: Optional[int] = None
    token_usage_json: dict[str, Any]
    error_code: Optional[str] = None
    visible: bool
    message: str
    payload_json: dict[str, Any]
    tenant_id: Optional[str] = None
    user_id: Optional[str] = None
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    privacy_mode: Optional[str] = None
    route_type: Optional[str] = None
    service_name: Optional[str] = None
    service_version: Optional[str] = None
    environment: Optional[str] = None
    occurred_at: Optional[datetime] = None
    ingested_at: Optional[datetime] = None
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
    required_task_key: Optional[str] = None
    handler: str
    default_agent_id: str
    default_skill_package: Optional[str] = None
    default_primary_skill: Optional[str] = None
    default_candidate_skills: list[str] = Field(default_factory=list)
    default_tools: list[str] = Field(default_factory=list)
    default_datasets: list[str] = Field(default_factory=list)
    input_schema_name: Optional[str] = None
    output_schema_name: Optional[str] = None
    item_output_schema_name: Optional[str] = None
    pipeline_id: Optional[str] = None
    stream_chunk_chars: int = 400
    conversation_message_field: Optional[str] = None
    resource_pool: Optional[str] = None
    access_mode: Optional[str] = None


class TaskRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    idempotency_key: Optional[str] = None
    pipeline_id: str
    pipeline_version: str
    model_pack_id: str
    status: str
    outcome: Optional[str] = None
    current_stage_id: Optional[str] = None
    cancel_requested: bool
    pause_requested: bool
    warning_count: int
    error_code: Optional[str] = None
    error_message: str
    resource_pool: str = "default"
    resource_access_mode: Optional[str] = None
    execution_state: Optional[str] = None
    blocking_reader_count: int = 0
    metadata_json: dict[str, Any]
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class TaskStageRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    run_id: str
    stage_id: str
    stage_type: str
    attempt: int
    status: str
    agent_id: Optional[str] = None
    thread_id: Optional[str] = None
    session_id: Optional[str] = None
    input_artifact_ids_json: list[str]
    output_artifact_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: str
    metadata_json: dict[str, Any]
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class TaskArtifactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    run_id: str
    stage_run_id: str
    artifact_type: str
    artifact_version: int
    schema_name: str
    schema_version: str
    content_json: Optional[dict[str, Any]] = None
    content_uri: Optional[str] = None
    summary: str
    parent_artifact_ids_json: list[str]
    checksum: str
    metadata_json: dict[str, Any]
    created_at: datetime


class TaskRunStartResponse(BaseModel):
    task_id: str
    run_id: str
    status: str
    stream_url: str


class HumanReviewRequest(BaseModel):
    action: str
    comment: str = ""
    patch: dict[str, Any] = Field(default_factory=dict)
    resume_from_stage: Optional[str] = None


class StageRetryRequest(BaseModel):
    comment: str = ""
