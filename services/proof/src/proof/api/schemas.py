from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CategoryCreate(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


class RetrievalFetchRequest(BaseModel):
    unit_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("unit_ids")
    @classmethod
    def unique_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))


class RetrievalSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=8, ge=1, le=20)
    retrieval_mode: Literal["hybrid", "vector", "keyword"] = "hybrid"
    policy_ids: list[str] = Field(default_factory=list, max_length=100)
    level_codes: list[str] = Field(default_factory=list, max_length=20)
    category_codes: list[str] = Field(default_factory=list, max_length=50)


class ConflictRetrievalRequest(BaseModel):
    unit_id: str = Field(min_length=1, max_length=160)
    top_k: int = Field(default=10, ge=1, le=20)

    @field_validator("unit_id")
    @classmethod
    def normalize_unit_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("unit_id must not be blank")
        return normalized


class PolicySqlRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    sql: str = Field(min_length=1, max_length=20_000)
