from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="CONTRACT_",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = ""
    database_max_connections: int = Field(default=5, ge=1, le=100)
    database_connection_acquire_timeout_seconds: float = Field(
        default=30,
        gt=0,
        le=300,
    )
    database_application_name: str = Field(
        default="ai-contract",
        min_length=1,
        max_length=63,
    )
    data_dir: Path = Path(".contract-data")
    max_file_size: int = Field(default=25 * 1024 * 1024, gt=0)
    schema_version: Literal["1.0"] = "1.0"

    internal_auth_enabled: bool = True
    internal_token: str = ""
    framework_result_sink_internal_token: str = Field(
        default="",
        validation_alias="FRAMEWORK_RESULT_SINK_INTERNAL_TOKEN",
    )
    framework_base_url: str = Field(
        default="http://framework:8894",
        validation_alias="FRAMEWORK_BASE_URL",
    )
    framework_connect_timeout_seconds: float = Field(
        default=5,
        gt=0,
        validation_alias="FRAMEWORK_CONNECT_TIMEOUT_SECONDS",
    )
    framework_read_timeout_seconds: float = Field(
        default=30,
        gt=0,
        validation_alias="FRAMEWORK_READ_TIMEOUT_SECONDS",
    )
    revision_draft_read_timeout_seconds: float = Field(
        default=120,
        gt=0,
        validation_alias="REVISION_DRAFT_READ_TIMEOUT_SECONDS",
    )
    framework_cancel_wait_seconds: float = Field(
        default=5,
        ge=0,
        validation_alias="FRAMEWORK_CANCEL_WAIT_SECONDS",
    )
    grounded_answer_timeout_seconds: float = Field(
        default=360,
        gt=0,
        validation_alias="CONTRACT_GROUNDED_ANSWER_TIMEOUT_SECONDS",
    )
    framework_stage_timeout_seconds: int = Field(
        default=1800,
        gt=0,
        validation_alias="FRAMEWORK_STAGE_TIMEOUT_SECONDS",
    )
    framework_recovery_grace_seconds: int = Field(
        default=120,
        ge=0,
        validation_alias="FRAMEWORK_RECOVERY_GRACE_SECONDS",
    )
    dispatcher_poll_seconds: float = Field(
        default=1,
        gt=0,
        validation_alias="CONTRACT_DISPATCHER_POLL_SECONDS",
    )
    dispatcher_batch_size: int = Field(
        default=8,
        gt=0,
        validation_alias="CONTRACT_DISPATCHER_BATCH_SIZE",
    )
    dispatch_lease_seconds: int = Field(
        default=120,
        gt=0,
        validation_alias="CONTRACT_DISPATCH_LEASE_SECONDS",
    )

    mock_mode: bool = True

    legal_evidence_policy: Literal["OFF", "OPTIONAL", "REQUIRED"] = Field(
        default="OFF",
        validation_alias="LEGAL_EVIDENCE_POLICY",
    )
    legal_evidence_default_jurisdiction: str = Field(
        default="CN",
        max_length=128,
        validation_alias="LEGAL_EVIDENCE_DEFAULT_JURISDICTION",
    )
    legal_embedding_base_url: str = Field(
        default="", validation_alias="LEGAL_EMBEDDING_BASE_URL"
    )
    legal_embedding_api_key: str = Field(
        default="", validation_alias="LEGAL_EMBEDDING_API_KEY"
    )
    legal_embedding_registration_id: str = Field(
        default="", validation_alias="LEGAL_EMBEDDING_REGISTRATION_ID"
    )
    legal_embedding_model: str = Field(
        default="", validation_alias="LEGAL_EMBEDDING_MODEL"
    )
    legal_reranker_base_url: str = Field(
        default="", validation_alias="LEGAL_RERANKER_BASE_URL"
    )
    legal_reranker_registration_id: str = Field(
        default="", validation_alias="LEGAL_RERANKER_REGISTRATION_ID"
    )
    legal_reranker_model: str = Field(
        default="", validation_alias="LEGAL_RERANKER_MODEL"
    )
    legal_reranker_api_key: str = Field(
        default="", validation_alias="LEGAL_RERANKER_API_KEY"
    )

    @property
    def legal_evidence_enabled(self) -> bool:
        """Keep provider construction policy-driven without a second switch."""

        return self.legal_evidence_policy != "OFF"

    def resolved_data_dir(self) -> Path:
        return self.data_dir.expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
