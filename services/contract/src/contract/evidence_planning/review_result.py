"""Domain-independent, traceable supplemental evidence-review result."""
from __future__ import annotations

import hashlib
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuleReviewCitation(ReviewModel):
    source_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quoted_text: str = Field(min_length=1)
    quoted_text_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def check_quote(self):
        if self.char_end - self.char_start != len(self.quoted_text):
            raise ValueError("Rule citation offsets do not match its text")
        if self.quoted_text_hash != "sha256:" + hashlib.sha256(self.quoted_text.encode()).hexdigest():
            raise ValueError("Rule citation text hash mismatch")
        return self


class RuleReviewBasis(ReviewModel):
    evidence_id: str = Field(pattern=r"^rule-evidence-[0-9a-f]{32}$")
    rule_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    version: int = Field(ge=1)
    name: str = Field(min_length=1)
    content: str = Field(min_length=1)
    review_method: str = Field(min_length=1)
    source_status: str
    check_codes: list[str]


class RuleReviewDecision(ReviewModel):
    decision_id: str = Field(pattern=r"^rule-decision-[0-9a-f]{32}$")
    evidence_id: str = Field(pattern=r"^rule-evidence-[0-9a-f]{32}$")
    outcome: Literal["RISK", "NO_RISK", "INSUFFICIENT_EVIDENCE"]
    title: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=4000)
    suggestion: str = Field(default="", max_length=4000)
    citations: list[RuleReviewCitation] = Field(default_factory=list)


class RuleReviewResult(ReviewModel):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["PREVIEW", "ACTIVE"]
    status: Literal["COMPLETED", "PARTIAL", "NO_APPLICABLE_RULES", "SELECTION_UNRESOLVED"]
    review_id: str
    generation_id: str
    tenant_id: str
    perspective: Literal["PARTY_A", "PARTY_B"]
    # Stage transport omits null fields. Unknown is valid and must not cause HTTP 422.
    business_role: str | None = None
    review_standard: Literal["neutral", "strong", "weak"]
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    bundle_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_version: str
    reviewer_version: str = "rule-review-v1"
    model_id: str
    evidence: list[RuleReviewBasis] = Field(default_factory=list)
    decisions: list[RuleReviewDecision] = Field(default_factory=list)
    pending_evidence_ids: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    model_calls: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_links(self):
        evidence = {item.evidence_id: item for item in self.evidence}
        ids = [item.evidence_id for item in self.decisions]
        if len(evidence) != len(self.evidence) or len(ids) != len(set(ids)):
            raise ValueError("Duplicate rule evidence or decision")
        if len({item.decision_id for item in self.decisions}) != len(self.decisions):
            raise ValueError("Duplicate rule decision ID")
        if len(set(self.pending_evidence_ids)) != len(self.pending_evidence_ids):
            raise ValueError("Duplicate pending rule evidence")
        if set(ids) & set(self.pending_evidence_ids):
            raise ValueError("Evaluated rules cannot remain pending")
        if set(ids) | set(self.pending_evidence_ids) != set(evidence):
            raise ValueError("Every rule must have one decision or explicit pending state")
        if self.status == "COMPLETED" and self.pending_evidence_ids:
            raise ValueError("Incomplete rule review cannot be marked complete")
        if self.mode == "ACTIVE" and any(item.source_status != "active" for item in self.evidence):
            raise ValueError("Active review requires published rules")
        for decision in self.decisions:
            if decision.outcome == "RISK" and (not decision.citations or not decision.suggestion.strip()):
                raise ValueError("Rule finding requires contract citations and a suggestion")
        return self
