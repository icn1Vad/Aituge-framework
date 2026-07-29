from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SimilarityDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["new_version", "separate"]
    candidate_policy_id: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def require_candidate_for_new_version(self) -> SimilarityDecisionRequest:
        candidate = str(self.candidate_policy_id or "").strip()
        if self.decision == "new_version" and not candidate:
            raise ValueError("candidate_policy_id is required for new_version")
        self.candidate_policy_id = candidate or None
        return self


class PolicyLifecycleActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["expire", "delete"]


class InternalPolicyActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=160)
    policy_id: str = Field(min_length=1, max_length=160)
    action: Literal["activate", "expire", "delete"]


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


class IntraConflictRetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str = Field(min_length=1, max_length=160)

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
