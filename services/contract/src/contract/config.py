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
    )

    database_url: str = ""
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

    mock_mode: bool = True

    def resolved_data_dir(self) -> Path:
        return self.data_dir.expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
