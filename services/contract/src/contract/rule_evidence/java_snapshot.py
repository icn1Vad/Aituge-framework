from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from contract.api.models import StrictModel
from contract.rule_evidence.models import (
    ReviewRuleSnapshot,
    RuleEvidenceIssue,
    RuleEvidencePlanRequest,
    RuleLibraryRelation,
)


class JavaRuleLibrarySnapshot(StrictModel):
    """Wire contract returned by ``GET /business/review-rules/snapshot``."""

    schema_version: Literal["1.0"]
    source_version: str = Field(min_length=1, max_length=160)
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tenant_id: str = Field(min_length=1, max_length=160)
    as_of_date: date
    rules: list[ReviewRuleSnapshot] = Field(default_factory=list, max_length=10000)

    @model_validator(mode="after")
    def validate_scope(self) -> JavaRuleLibrarySnapshot:
        if any(rule.tenant_id not in {"0", self.tenant_id} for rule in self.rules):
            raise ValueError("Rule snapshot contains a foreign tenant")
        return self

    def planning_request(
        self,
        *,
        review_id: str,
        generation_id: str,
        contract_type: str,
        perspective: str,
        jurisdiction: str | None,
        contract_date: date | None,
        issues: list[RuleEvidenceIssue],
        relations: list[RuleLibraryRelation] | None = None,
        review_standard: Literal["neutral", "strong", "weak"] = "neutral",
        business_role: str | None = None,
        contract_type_aliases: list[str] | None = None,
    ) -> RuleEvidencePlanRequest:
        return RuleEvidencePlanRequest(
            review_id=review_id,
            generation_id=generation_id,
            tenant_id=self.tenant_id,
            contract_type=contract_type,
            perspective=perspective,
            review_standard=review_standard,
            business_role=business_role,
            contract_type_aliases=contract_type_aliases or [],
            jurisdiction=jurisdiction,
            contract_date=contract_date,
            review_as_of_date=self.as_of_date,
            source_version=self.source_version,
            frozen_snapshot_hash=self.snapshot_hash,
            issues=issues,
            rules=self.rules,
            relations=relations or [],
        )
