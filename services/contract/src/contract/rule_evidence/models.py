from __future__ import annotations

import hashlib
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from contract.api.models import StrictModel
from contract.application.idempotency import canonical_json


class ReviewRuleSnapshot(StrictModel):
    """Immutable shape consumed from the Java rule-library snapshot API."""

    rule_id: str = Field(min_length=1, max_length=160)
    code: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    tenant_id: str = Field(min_length=1, max_length=160)
    review_direction: str = Field(min_length=1, max_length=1000)
    name: str = Field(min_length=1, max_length=1000)
    contract_type_path: list[str] = Field(default_factory=list, max_length=30)
    contract_type_id: str | None = Field(default=None, max_length=160)
    party_stance: str | None = Field(default=None, max_length=80)
    review_standard: str = Field(min_length=1, max_length=80)
    rule_type: str = Field(min_length=1, max_length=80)
    source: str = Field(min_length=1, max_length=80)
    reference_basis: str | None = Field(default=None, max_length=4000)
    content: str = Field(min_length=1, max_length=20000)
    review_method: str = Field(min_length=1, max_length=10000)
    status: str = Field(min_length=1, max_length=80)
    jurisdiction: str | None = Field(default=None, max_length=128)
    effective_from: date | None = None
    effective_to: date | None = None
    legal_source_version: str | None = Field(default=None, max_length=300)
    target_check_codes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_dates_and_codes(self) -> ReviewRuleSnapshot:
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            raise ValueError("effective_from must not be after effective_to")
        self.target_check_codes = list(dict.fromkeys(self.target_check_codes))
        return self


class RuleLibraryRelation(StrictModel):
    relation_id: str = Field(min_length=1, max_length=160)
    source_rule_id: str = Field(min_length=1, max_length=160)
    target_rule_id: str = Field(min_length=1, max_length=160)
    relation_type: Literal[
        "DEPENDS_ON",
        "IMPLEMENTS",
        "SUPPLEMENTS",
        "EXCEPTION_TO",
        "CONFLICTS_WITH",
        "SUPERSEDES",
    ]
    evidence_text: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0, le=1)
    verification_status: Literal["VERIFIED", "CANDIDATE"] = "CANDIDATE"


class RuleEvidenceIssue(StrictModel):
    issue_id: str = Field(pattern=r"^rule-issue-[0-9a-f]{32}$")
    domain: str = Field(min_length=1, max_length=160)
    query: str = Field(min_length=1, max_length=8000)
    facts: list[str] = Field(default_factory=list, max_length=50)
    required_concepts: list[str] = Field(default_factory=list, max_length=100)
    check_codes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def normalize_lists(self) -> RuleEvidenceIssue:
        self.facts = list(dict.fromkeys(item.strip() for item in self.facts if item.strip()))
        self.required_concepts = list(
            dict.fromkeys(item.strip() for item in self.required_concepts if item.strip())
        )
        self.check_codes = list(dict.fromkeys(item.strip() for item in self.check_codes if item.strip()))
        return self


class RuleEvidencePlanRequest(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=160)
    contract_type: str = Field(min_length=1, max_length=160)
    perspective: str = Field(min_length=1, max_length=80)
    review_standard: Literal["neutral", "strong", "weak"] = "neutral"
    business_role: str | None = Field(default=None, max_length=80)
    contract_type_aliases: list[str] = Field(default_factory=list, max_length=30)
    preview_pending: bool = False
    jurisdiction: str | None = Field(default=None, max_length=128)
    contract_date: date | None = None
    review_as_of_date: date
    source_version: str = Field(min_length=1, max_length=160)
    frozen_snapshot_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    planner_version: str = Field(default="adaptive-rule-evidence-planner-v1", max_length=160)
    issues: list[RuleEvidenceIssue] = Field(min_length=1, max_length=100)
    rules: list[ReviewRuleSnapshot] = Field(default_factory=list, max_length=10000)
    relations: list[RuleLibraryRelation] = Field(default_factory=list, max_length=50000)

    @model_validator(mode="after")
    def validate_snapshot(self) -> RuleEvidencePlanRequest:
        for values, label in (
            ([item.issue_id for item in self.issues], "issue IDs"),
            ([item.rule_id for item in self.rules], "rule IDs"),
            ([item.relation_id for item in self.relations], "relation IDs"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        return self

    @property
    def snapshot_hash(self) -> str:
        if self.frozen_snapshot_hash is not None:
            return self.frozen_snapshot_hash
        payload = {
            "tenant_id": self.tenant_id,
            "source_version": self.source_version,
            "rules": [item.model_dump(mode="json") for item in self.rules],
            "relations": [item.model_dump(mode="json") for item in self.relations],
        }
        return "sha256:" + hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()


class RuleEvidence(StrictModel):
    evidence_id: str = Field(pattern=r"^rule-evidence-[0-9a-f]{32}$")
    issue_ids: list[str] = Field(min_length=1)
    check_codes: list[str] = Field(default_factory=list)
    rule: ReviewRuleSnapshot
    relevance_score: float = Field(ge=0, le=1)
    matched_concepts: list[str] = Field(default_factory=list)
    retrieval_channels: list[Literal["EXACT", "KEYWORD", "RELATION"]] = Field(min_length=1)
    relation_path: list[str] = Field(default_factory=list)


class RuleIssueCoverage(StrictModel):
    issue_id: str
    required_concepts: list[str]
    covered_concepts: list[str]
    evidence_ids: list[str]
    complete: bool


class RuleEvidenceBundle(StrictModel):
    bundle_version: Literal["1.0"] = "1.0"
    bundle_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_version: str
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    planner_version: str
    review_standard: Literal["neutral", "strong", "weak"] = "neutral"
    preview_only: bool = False
    binding_profile_version: str | None = None
    binding_status: Literal["NOT_BOUND", "NONE", "PARTIAL", "COMPLETE"] = "NOT_BOUND"
    status: Literal["READY", "DEGRADED", "NO_RELEVANT_EVIDENCE"]
    issues: list[RuleEvidenceIssue]
    evidence: list[RuleEvidence]
    relations: list[RuleLibraryRelation]
    coverage: list[RuleIssueCoverage]
    unresolved_issue_ids: list[str]
    stop_reason: Literal[
        "COVERAGE_SATISFIED",
        "CANDIDATES_EXHAUSTED",
        "SAFETY_BUDGET_REACHED",
    ]
    examined_candidate_count: int = Field(ge=0)
    round_count: int = Field(ge=0)

    @property
    def usable(self) -> bool:
        return (
            not self.preview_only
            and self.binding_status in {"PARTIAL", "COMPLETE"}
            and any(item.check_codes for item in self.evidence)
            and self.status in {"READY", "DEGRADED"}
        )
