from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=12, ge=1, le=20)
    retrieval_mode: Literal["hybrid", "keyword", "vector"] = "hybrid"

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


class SqlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    sql: str = Field(min_length=1, max_length=20_000)


class QaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=12, ge=1, le=20)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)


class ChatHistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    history: list[ChatHistoryItem] = Field(default_factory=list, max_length=12)
    top_k: int = Field(default=12, ge=1, le=20)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be blank")
        return value
