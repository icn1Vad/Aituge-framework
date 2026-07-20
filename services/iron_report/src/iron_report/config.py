from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


SERVICE_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration; credentials are supplied outside Git."""

    model_config = SettingsConfigDict(env_prefix="IRON_REPORT_", extra="ignore")

    data_root: Path = SERVICE_ROOT / "demo_data"
    runtime_root: Path = Path("/app/runtime/iron-report")
    service_base_url: str = "http://127.0.0.1:18300"
    internal_token: str = Field(min_length=16)
    model_id: str = Field(min_length=1, max_length=64)
    model_name: str = ""
    model_base_url: str = ""
    model_api_key: str = ""
    model_provider: str = "openai_like"
    model_context_window: int = Field(default=64000, ge=4096)
    model_max_tokens: int = Field(default=8000, ge=1024)
    search_endpoint: str = ""
    search_api_key: str = ""
    search_count: int = Field(default=8, ge=1, le=10)
    libreoffice_path: str = "libreoffice"
    libreoffice_timeout_seconds: int = Field(default=90, ge=10, le=300)
    max_artifact_bytes: int = Field(default=30 * 1024 * 1024, ge=1024)

    @property
    def export_state_root(self) -> Path:
        return self.runtime_root / "exports"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
