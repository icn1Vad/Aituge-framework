from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="PROOF_",
        extra="ignore",
    )

    database_url: str = ""
    storage_root: Path = Path(".proof-data")
    dataset_root: Path | None = None
    max_upload_bytes: int = 25 * 1024 * 1024
    model_pack_id: str = ""
    model_config_dir: Path | None = None
    model_secret_dir: Path | None = None

    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dimensions: int | None = Field(default=None, ge=1)
    embedding_max_input_chars: int | None = Field(default=None, ge=1)
    embedding_timeout_seconds: float = Field(default=30.0, gt=0)

    retrieval_keyword_limit: int = Field(default=30, ge=1, le=100)
    retrieval_vector_limit: int = Field(default=30, ge=1, le=100)
    similarity_title_threshold: float = Field(default=0.75, ge=0, le=1)
    similarity_edit_threshold: float = Field(default=0.80, ge=0, le=1)
    similarity_jaccard_threshold: float = Field(default=0.79, ge=0, le=1)
    similarity_containment_threshold: float = Field(default=0.98, ge=0, le=1)
    similarity_length_ratio_threshold: float = Field(default=0.80, ge=0, le=1)
    similarity_clause_coverage_threshold: float = Field(default=0.80, ge=0, le=1)
    similarity_candidate_limit: int = Field(default=3, ge=1, le=10)
    conflict_same_title_limit: int = Field(default=6, ge=1, le=100)
    conflict_leaf_category_limit: int = Field(default=6, ge=1, le=100)
    conflict_parent_category_limit: int = Field(default=4, ge=1, le=100)
    conflict_global_limit: int = Field(default=2, ge=1, le=100)
    conflict_max_candidates: int = Field(default=60, ge=1, le=200)

    rerank_base_url: str = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
    rerank_api_key: str = ""
    rerank_model: str = "qwen3-rerank"
    rerank_timeout_seconds: float = Field(default=30.0, gt=0)
    rerank_candidate_limit: int = Field(default=30, ge=1, le=100)
    rerank_instruction: str = "根据用户问题，按照制度条款对回答问题的相关性从高到低排序。"

    sql_statement_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    sql_max_rows: int = Field(default=100, ge=1, le=1000)
    sql_max_response_chars: int = Field(default=50_000, ge=1000, le=1_000_000)

    semantic_audit_enabled: bool = False
    framework_base_url: str = ""
    framework_user_id: str = "proof-service"
    audit_batch_max_chars: int = Field(default=6000, ge=1)
    audit_batch_max_chunks: int = Field(default=8, ge=1, le=8)
    audit_max_chunk_chars: int = Field(default=12_000, ge=1)
    audit_max_concurrency: int = Field(default=4, ge=1, le=8)
    summary_max_chars: int = Field(default=60_000, ge=1)

    @property
    def embedding_configured(self) -> bool:
        return bool(
            self.embedding_base_url.strip()
            and self.embedding_api_key.strip()
            and self.embedding_model.strip()
            and self.embedding_dimensions
        )

    @property
    def resolved_rerank_api_key(self) -> str:
        return self.rerank_api_key.strip() or self.embedding_api_key.strip()

    @property
    def reranker_configured(self) -> bool:
        return bool(
            self.rerank_base_url.strip()
            and self.resolved_rerank_api_key
            and self.rerank_model.strip()
        )

    def resolved_storage_root(self) -> Path:
        return self.storage_root.expanduser().resolve()

    def resolved_dataset_root(self) -> Path | None:
        if self.dataset_root is None:
            return None
        return self.dataset_root.expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
