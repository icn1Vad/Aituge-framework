from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="QXS_", extra="ignore")

    database_url: str = ""
    corpus_root: Path = Path("/app/corpus")
    storage_root: Path = Path(".qxs-data")
    model_pack_id: str = ""
    model_config_dir: Path | None = None
    model_secret_dir: Path | None = None
    ocr_enabled: bool = False
    ocr_device: str = "cpu"
    ocr_formula_enabled: bool = False
    ocr_progress_interval: int = Field(default=25, ge=1, le=1000)
    ocr_text_threshold: int = Field(default=80, ge=0)
    chunk_min_chars: int = Field(default=500, ge=100)
    chunk_max_chars: int = Field(default=900, ge=200)
    chunk_overlap_chars: int = Field(default=100, ge=0)
    embedding_progress_interval: int = Field(default=50, ge=1, le=10_000)
    retrieval_keyword_limit: int = Field(default=30, ge=1, le=100)
    retrieval_vector_limit: int = Field(default=30, ge=1, le=100)
    rerank_candidate_limit: int = Field(default=30, ge=1, le=100)
    llm_timeout_seconds: float = Field(default=180.0, gt=0, le=900)
    llm_answer_max_tokens: int = Field(default=1800, ge=256, le=8000)
    llm_answer_temperature: float = Field(default=0.1, ge=0, le=1.5)
    web_search_enabled: bool = True
    web_search_endpoint: str = "https://www.bing.com/search"
    web_search_result_count: int = Field(default=5, ge=1, le=10)
    web_search_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    code_interpreter_enabled: bool = True
    code_execution_timeout_seconds: int = Field(default=10, ge=1, le=30)
    code_execution_max_output_chars: int = Field(default=20_000, ge=1000, le=100_000)
    sql_statement_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    sql_max_rows: int = Field(default=100, ge=1, le=1000)
    sql_max_response_chars: int = Field(default=50_000, ge=1000)

    def resolved_corpus_root(self) -> Path:
        return self.corpus_root.expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
