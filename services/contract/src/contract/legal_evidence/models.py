from __future__ import annotations

import hashlib
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from contract.api.models import StrictModel
from contract.application.idempotency import canonical_json

LegalDomain = Literal[
    "formation_validity_authority",
    "commercial_financial",
    "performance_obligations",
    "ip_confidentiality_data",
    "liability_remedies_exit",
    "cross_clause_consistency",
    "missing_ambiguity_completeness",
]
LegalRelationType = Literal[
    "CITES",
    "BASED_ON",
    "IMPLEMENTS",
    "INTERPRETS",
    "AMENDS",
    "REPEALS",
    "REPLACES",
    "SUPPLEMENTS",
    "EXCEPTION_TO",
    "INTERNAL_REF",
]


class LegalEvidenceRelease(StrictModel):
    release_id: str = Field(min_length=1, max_length=160)
    source_release_id: str = Field(min_length=1, max_length=160)
    status: Literal["STAGED", "ACTIVE", "RETIRED"]
    projection_version: str = Field(min_length=1, max_length=160)
    embedding_profile_id: str | None = Field(default=None, max_length=160)
    relation_extractor_version: str = Field(
        default="legal-relation-extractor-v1", min_length=1, max_length=160
    )
    embedding_model_version: str | None = Field(default=None, max_length=300)


class LegalRetrievalUnit(StrictModel):
    unit_id: str = Field(min_length=1, max_length=160)
    release_id: str = Field(min_length=1, max_length=160)
    instrument_id: str = Field(min_length=1, max_length=160)
    version_id: str = Field(min_length=1, max_length=160)
    source_node_ids: list[str] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=1000)
    article_no: str | None = Field(default=None, max_length=160)
    heading_path: list[str] = Field(default_factory=list)
    content: str = Field(min_length=1)
    jurisdiction: str | None = Field(default=None, max_length=128)
    authority_level: str | None = Field(default=None, max_length=128)
    issuing_authority: str | None = Field(default=None, max_length=1000)
    effective_from: date | None = None
    effective_to: date | None = None
    validity_status: Literal[
        "ACTIVE",
        "NOT_YET_EFFECTIVE",
        "EXPIRED",
        "REPEALED",
        "UNKNOWN",
    ] | None = None
    metadata_verification_status: Literal["VERIFIED", "UNVERIFIED", "REJECTED"] = (
        "UNVERIFIED"
    )
    official_source_url: str | None = Field(default=None, max_length=4000)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_dates_and_sources(self) -> LegalRetrievalUnit:
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            raise ValueError("effective_from must not be after effective_to")
        if len(self.source_node_ids) != len(set(self.source_node_ids)):
            raise ValueError("source_node_ids must be unique")
        return self

    @property
    def projection_hash(self) -> str:
        """Identity of every persisted retrieval-unit field.

        ``content_hash`` deliberately covers only the legal text because it is
        exposed as source provenance.  Resume safety needs a stronger identity:
        a changed title, article number, jurisdiction, source-node set, or
        sequence must never silently reuse an older projected row.
        """

        payload = self.model_dump(mode="json")
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    @property
    def embedding_input(self) -> str:
        """The exact text supplied to the embedding provider."""

        return "\n".join(
            part for part in (self.title, self.article_no or "", self.content) if part
        )

    @property
    def embedding_input_hash(self) -> str:
        return hashlib.sha256(self.embedding_input.encode("utf-8")).hexdigest()


class LegalSearchCandidate(StrictModel):
    unit: LegalRetrievalUnit
    score: float = Field(ge=0, le=1)
    channel: Literal["EXACT", "KEYWORD", "VECTOR", "RELATION"]


class LegalRelation(StrictModel):
    relation_id: str = Field(min_length=1, max_length=160)
    release_id: str = Field(min_length=1, max_length=160)
    source_unit_id: str = Field(min_length=1, max_length=160)
    target_unit_id: str = Field(min_length=1, max_length=160)
    relation_type: LegalRelationType
    evidence_text: str = Field(default="", max_length=4000)
    source_node_id: str | None = Field(default=None, max_length=160)
    evidence_start: int | None = Field(default=None, ge=0)
    evidence_end: int | None = Field(default=None, ge=0)
    extractor_version: str = Field(
        default="legal-relation-extractor-v1", min_length=1, max_length=160
    )
    confidence: float = Field(ge=0, le=1)
    verification_status: Literal["VERIFIED", "AUTO_VERIFIED", "CANDIDATE"]

    @model_validator(mode="after")
    def validate_evidence_span(self) -> LegalRelation:
        if (self.evidence_start is None) != (self.evidence_end is None):
            raise ValueError("relation evidence positions must be supplied together")
        if (
            self.evidence_start is not None
            and self.evidence_end is not None
            and self.evidence_start > self.evidence_end
        ):
            raise ValueError("relation evidence_start must not exceed evidence_end")
        return self


class LegalRelationReviewItem(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    release_id: str = Field(min_length=1, max_length=160)
    source_unit_id: str = Field(min_length=1, max_length=160)
    source_node_id: str | None = Field(default=None, max_length=160)
    target_title: str = Field(min_length=1, max_length=1000)
    target_article_no: str | None = Field(default=None, max_length=160)
    proposed_relation_type: LegalRelationType
    evidence_text: str = Field(min_length=1, max_length=4000)
    evidence_start: int = Field(ge=0)
    evidence_end: int = Field(ge=0)
    extractor_version: str = Field(min_length=1, max_length=160)
    review_reason: Literal["AMBIGUOUS_TARGET", "UNMATCHED_TARGET"]

    @model_validator(mode="after")
    def validate_review_span(self) -> LegalRelationReviewItem:
        if self.evidence_start > self.evidence_end:
            raise ValueError("review evidence_start must not exceed evidence_end")
        return self


class LegalEvidenceIssue(StrictModel):
    issue_id: str = Field(pattern=r"^legal-issue-[0-9a-f]{32}$")
    domain: LegalDomain
    query: str = Field(min_length=1, max_length=8000)
    question: str | None = Field(default=None, min_length=1, max_length=4000)
    facts: list[str] = Field(default_factory=list, max_length=20)
    parties: list[str] = Field(default_factory=list, max_length=20)
    contract_object: str | None = Field(default=None, max_length=1000)
    source_domain: LegalDomain | None = None
    evidence_need: list[str] = Field(default_factory=list, max_length=100)
    check_codes: list[str] = Field(default_factory=list, max_length=45)
    required_concepts: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_concepts(self) -> LegalEvidenceIssue:
        normalized = [item.strip() for item in self.required_concepts]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("required_concepts must be non-empty and unique")
        if len(self.check_codes) != len(set(self.check_codes)):
            raise ValueError("check_codes must be unique")
        self.required_concepts = normalized
        self.question = (self.question or self.query).strip()
        self.source_domain = self.source_domain or self.domain
        self.facts = list(dict.fromkeys(item.strip() for item in self.facts if item.strip()))
        self.parties = list(
            dict.fromkeys(item.strip() for item in self.parties if item.strip())
        )
        self.evidence_need = list(
            dict.fromkeys(
                item.strip()
                for item in (self.evidence_need or self.required_concepts)
                if item.strip()
            )
        )
        return self


class LegalEvidencePlanRequest(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    contract_type: str = Field(min_length=1, max_length=160)
    jurisdiction: str | None = Field(default=None, max_length=128)
    contract_date: date | None = None
    review_as_of_date: date
    planner_version: str = Field(
        default="adaptive-legal-evidence-planner-v2", min_length=1, max_length=160
    )
    reranker_version: str | None = Field(default=None, max_length=300)
    issues: list[LegalEvidenceIssue] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_issue_ids(self) -> LegalEvidencePlanRequest:
        issue_ids = [item.issue_id for item in self.issues]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("issue IDs must be unique")
        return self

    @property
    def stable_hash(self) -> str:
        """Content identity used to freeze one review attempt.

        The natural snapshot key alone is not enough: callers must not reuse
        the same review/generation/attempt tuple with different planning
        inputs.  Keeping the canonical hash on the request model gives every
        persistence adapter exactly one definition of request identity.
        """

        payload = self.model_dump(mode="json")
        return "sha256:" + hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()


class LegalEvidence(StrictModel):
    evidence_id: str = Field(pattern=r"^legal-evidence-[0-9a-f]{32}$")
    issue_ids: list[str] = Field(min_length=1)
    check_codes: list[str] = Field(default_factory=list, max_length=45)
    unit: LegalRetrievalUnit
    relevance_score: float = Field(ge=0, le=1)
    rerank_score: float | None = Field(default=None, ge=0, le=1)
    matched_concepts: list[str] = Field(default_factory=list)
    retrieval_channels: list[
        Literal["EXACT", "KEYWORD", "VECTOR", "RELATION"]
    ] = Field(
        min_length=1
    )
    relation_path: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list, max_length=20)


class LegalIssueCoverage(StrictModel):
    issue_id: str = Field(pattern=r"^legal-issue-[0-9a-f]{32}$")
    required_concepts: list[str]
    covered_concepts: list[str]
    evidence_ids: list[str]
    complete: bool
    confidence: float = Field(ge=0, le=1)


class LegalEvidenceConflict(StrictModel):
    conflict_type: Literal["VALIDITY", "APPLICABILITY", "RELATION"]
    evidence_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2000)


class LegalEvidenceRelationPath(StrictModel):
    issue_id: str = Field(pattern=r"^legal-issue-[0-9a-f]{32}$")
    evidence_id: str = Field(pattern=r"^legal-evidence-[0-9a-f]{32}$")
    source_unit_id: str = Field(min_length=1, max_length=160)
    target_unit_id: str = Field(min_length=1, max_length=160)
    relation_ids: list[str] = Field(min_length=1)


class LegalApplicabilityDecision(StrictModel):
    issue_id: str = Field(pattern=r"^legal-issue-[0-9a-f]{32}$")
    evidence_id: str = Field(pattern=r"^legal-evidence-[0-9a-f]{32}$")
    unit_id: str = Field(min_length=1, max_length=160)
    outcome: Literal[
        "APPLICABLE",
        "REVIEW_DATE_ONLY",
        "UNKNOWN_METADATA",
    ]
    jurisdiction_decision: Literal["MATCH", "UNKNOWN"]
    temporal_decision: Literal[
        "MATCH",
        "NOT_EFFECTIVE_AT_CONTRACT_DATE",
        "UNKNOWN",
    ]
    review_as_of_date: date
    contract_date: date | None = None
    reasons: list[str] = Field(default_factory=list, max_length=20)


class LegalEvidenceVersionSnapshot(StrictModel):
    legal_release_id: str = Field(min_length=1, max_length=160)
    legal_projection_version: str = Field(min_length=1, max_length=160)
    relation_extractor_version: str = Field(min_length=1, max_length=160)
    embedding_model_version: str | None = Field(default=None, max_length=300)
    reranker_version: str | None = Field(default=None, max_length=300)
    planner_version: str = Field(min_length=1, max_length=160)
    review_as_of_date: date
    contract_date: date | None = None


class LegalEvidenceBundle(StrictModel):
    bundle_version: Literal["1.0"] = "1.0"
    bundle_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    binding_profile_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    binding_status: Literal["NOT_BOUND", "NONE", "PARTIAL", "COMPLETE"] = "NOT_BOUND"
    mapped_check_codes: list[str] = Field(default_factory=list, max_length=45)
    unmapped_check_codes: list[str] = Field(default_factory=list, max_length=45)
    unmapped_issue_ids: list[str] = Field(default_factory=list, max_length=100)
    unverified_evidence_ids: list[str] = Field(default_factory=list)
    status: Literal[
        "READY",
        "DEGRADED",
        "NO_ACTIVE_RELEASE",
        "NO_RELEVANT_EVIDENCE",
    ]
    release_id: str | None = None
    version_snapshot: LegalEvidenceVersionSnapshot | None = None
    issues: list[LegalEvidenceIssue]
    evidence: list[LegalEvidence]
    relations: list[LegalRelation]
    relation_paths: list[LegalEvidenceRelationPath] = Field(default_factory=list)
    applicability_decisions: list[LegalApplicabilityDecision] = Field(
        default_factory=list
    )
    coverage: list[LegalIssueCoverage]
    unresolved_issue_ids: list[str]
    conflicts: list[LegalEvidenceConflict]
    degraded_channels: list[
        Literal["EXACT", "KEYWORD", "VECTOR", "RELATION", "RERANK"]
    ]
    rerank_applied: bool = False
    rerank_diagnostics: list[str] = Field(default_factory=list, max_length=500)
    stop_reason: Literal[
        "COVERAGE_SATISFIED",
        "CANDIDATES_EXHAUSTED",
        "SAFETY_BUDGET_REACHED",
        "NO_ACTIVE_RELEASE",
    ]
    examined_candidate_count: int = Field(ge=0)
    round_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_binding_diagnostics(self) -> LegalEvidenceBundle:
        for values, label in (
            (self.mapped_check_codes, "mapped_check_codes"),
            (self.unmapped_check_codes, "unmapped_check_codes"),
            (self.unmapped_issue_ids, "unmapped_issue_ids"),
            (self.unverified_evidence_ids, "unverified_evidence_ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if self.binding_status == "NOT_BOUND" and self.binding_profile_version is not None:
            raise ValueError("A bound bundle must declare binding_status")
        if self.binding_status != "NOT_BOUND" and self.binding_profile_version is None:
            raise ValueError("binding_status requires binding_profile_version")
        return self

    @property
    def usable(self) -> bool:
        return (
            self.binding_profile_version is not None
            and any(item.check_codes for item in self.evidence)
            and self.status in {"READY", "DEGRADED"}
        )


class LegalEvidenceSnapshotConflict(RuntimeError):
    """The same natural attempt key was reused for different input."""


class FrozenLegalEvidencePlanningFailure(RuntimeError):
    """A planner failure already won and was frozen for this attempt."""

    def __init__(self, error_type: str) -> None:
        self.error_type = error_type
        super().__init__(f"Legal evidence planning is frozen as failed: {error_type}")


class LegalEvidenceSnapshotCompatibilityError(RuntimeError):
    """A frozen success cannot be reinterpreted by a newer binding profile."""


class LegalEvidencePlanSnapshot(StrictModel):
    """Immutable first-writer decision for one review attempt."""

    request_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    bundle_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal[
        "READY",
        "DEGRADED",
        "NO_ACTIVE_RELEASE",
        "NO_RELEVANT_EVIDENCE",
        "PLANNER_FAILED",
    ]
    bundle: LegalEvidenceBundle | None = None
    error_type: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_decision_shape(self) -> LegalEvidencePlanSnapshot:
        if self.status == "PLANNER_FAILED":
            if self.bundle is not None or self.error_type is None:
                raise ValueError(
                    "PLANNER_FAILED snapshots require error_type and no bundle"
                )
            return self
        if self.bundle is None or self.error_type is not None:
            raise ValueError("Successful snapshots require a bundle and no error_type")
        if self.bundle.status != self.status:
            raise ValueError("Snapshot status must match bundle status")
        if self.bundle.bundle_hash != self.bundle_hash:
            raise ValueError("Snapshot bundle_hash must match the frozen bundle")
        return self
