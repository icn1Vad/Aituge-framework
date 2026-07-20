from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRANSLATION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_root: Path = Path(".translation-data")
    internal_token: SecretStr
    model_api_key: SecretStr
    model_base_url: str = "https://api.deepseek.com/v1"
    model_id: str = "deepseek-chat"
    model_name: str = "deepseek-chat"
    model_max_tokens: int = Field(default=8000, ge=256, le=65536)
    model_context_window: int = Field(default=64000, ge=4096)
    model_temperature: float = Field(default=0.1, ge=0, le=2)
    model_call_timeout_seconds: float = Field(default=120.0, gt=0, le=600)

    babeldoc_base_url: str = "http://babeldoc-sidecar:18600"
    babeldoc_internal_token: SecretStr
    babeldoc_poll_interval_seconds: float = Field(default=2.0, ge=0.2, le=30)
    babeldoc_timeout_seconds: float = Field(default=1800.0, gt=0, le=7200)

    max_text_chars: int = Field(default=100_000, ge=1)
    model_batch_max_chars: int = Field(default=12_000, ge=1000)
    model_batch_max_units: int = Field(default=20, ge=1, le=100)
    detection_sample_chars: int = Field(default=4000, ge=100, le=20000)
    detection_min_chars: int = Field(default=20, ge=1)
    detection_min_confidence: float = Field(default=0.75, ge=0, le=1)

    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    max_pdf_pages: int = Field(default=300, ge=1)
    pdf_detection_pages: int = Field(default=5, ge=1, le=20)
    max_docx_uncompressed_bytes: int = Field(
        default=200 * 1024 * 1024, ge=1024
    )
    max_docx_entries: int = Field(default=5000, ge=1)
    max_docx_compression_ratio: float = Field(default=100.0, ge=1)
    libreoffice_binary: str = "soffice"
    libreoffice_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    file_task_timeout_seconds: float = Field(default=1800.0, gt=0, le=7200)
    artifact_retention_hours: int = Field(default=72, ge=1, le=720)

    max_concurrent_text_tasks: int = Field(default=4, ge=1, le=32)
    max_concurrent_file_tasks: int = Field(default=2, ge=1, le=16)
    max_pending_file_tasks: int = Field(default=16, ge=1, le=1000)

    @field_validator("internal_token", "babeldoc_internal_token")
    @classmethod
    def validate_internal_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 16:
            raise ValueError("internal service tokens must contain at least 16 characters")
        return value

    @field_validator("model_api_key")
    @classmethod
    def validate_model_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("model API key is required")
        return value

    def resolved_data_root(self) -> Path:
        return self.data_root.expanduser().resolve()

    @property
    def temp_root(self) -> Path:
        return self.resolved_data_root() / "tmp"

    @property
    def artifact_root(self) -> Path:
        return self.resolved_data_root() / "artifacts"

    @property
    def sqlite_path(self) -> Path:
        return self.resolved_data_root() / "translation-framework.db"

    def prepare_runtime(self) -> None:
        for directory in (
            self.resolved_data_root(),
            self.temp_root,
            self.artifact_root,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        os.environ["DB_TYPE"] = "sqlite"
        os.environ["SQLITE_URL"] = (
            f"sqlite+aiosqlite:///{self.sqlite_path.as_posix()}"
        )
