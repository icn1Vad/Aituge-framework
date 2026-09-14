from __future__ import annotations

import hashlib
from datetime import date
from enum import Enum
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

from contract.evidence_planning.review_result import RuleReviewResult

SCHEMA_VERSION = "1.0"
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
HashValue = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Perspective(str, Enum):
    PARTY_A = "PARTY_A"
    PARTY_B = "PARTY_B"


class ReviewStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ReviewStage(str, Enum):
    PARSING = "PARSING"
    PARTY_RESOLUTION = "PARTY_RESOLUTION"
    IR_EXTRACTION = "IR_EXTRACTION"
    RIGHTS_OBLIGATIONS = "RIGHTS_OBLIGATIONS"
    RISK_REVIEW = "RISK_REVIEW"
    EVIDENCE_VERIFICATION = "EVIDENCE_VERIFICATION"
    FINALIZING = "FINALIZING"


class RiskLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class FindingCategory(str, Enum):
    PARTY_IDENTIFICATION = "PARTY_IDENTIFICATION"
    RIGHTS_OBLIGATIONS_IMBALANCE = "RIGHTS_OBLIGATIONS_IMBALANCE"
    PAYMENT = "PAYMENT"
    DELIVERY = "DELIVERY"
    ACCEPTANCE = "ACCEPTANCE"
    BREACH = "BREACH"
    LIABILITY = "LIABILITY"
    TERMINATION = "TERMINATION"
    CONFIDENTIALITY = "CONFIDENTIALITY"
    INTELLECTUAL_PROPERTY = "INTELLECTUAL_PROPERTY"
    DISPUTE_RESOLUTION = "DISPUTE_RESOLUTION"
    MISSING_CLAUSE = "MISSING_CLAUSE"
    AMBIGUITY = "AMBIGUITY"
    INTERNAL_CONFLICT = "INTERNAL_CONFLICT"
    OTHER = "OTHER"


class EvidenceType(str, Enum):
    TEXT_QUOTE = "TEXT_QUOTE"
    CONTEXT = "CONTEXT"
    ABSENCE = "ABSENCE"


class ErrorData(StrictModel):
    code: Identifier
    message: Annotated[str, StringConstraints(min_length=1)]
    retryable: bool
    user_action_required: bool
    details: dict[str, JsonValue] | None = None


class ErrorResponse(StrictModel):
    success: Literal[False] = False
    error: ErrorData
    request_id: Identifier


DataT = TypeVar("DataT")


class SuccessResponse(StrictModel, Generic[DataT]):
    success: Literal[True] = True
    data: DataT
    request_id: Identifier


class HealthData(StrictModel):
    status: Literal["UP"] = "UP"
    service: Literal["contract"] = "contract"
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["mock", "runtime"]


class CreateReviewRequest(StrictModel):
    business_task_id: Identifier
    contract_version_id: Identifier
    party_resolution_id: Identifier | None = None
    model_pack_id: Identifier | None = None
    perspective: Perspective
    our_party_name: Annotated[str, StringConstraints()] | None = None
    confirmed_party_a_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ] | None = None
    confirmed_party_b_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ] | None = None
    contract_type: Literal["AUTO"]
    rule_review_standard: Literal["neutral", "strong", "weak"] = "neutral"
    review_attitude: Literal["NEUTRAL"]
    schema_version: Literal["1.0"]

    @model_validator(mode="after")
    def validate_confirmed_parties(self) -> "CreateReviewRequest":
        if (self.confirmed_party_a_name is None) != (self.confirmed_party_b_name is None):
            raise ValueError("confirmed_party_a_name and confirmed_party_b_name must be supplied together")
        if (
            self.confirmed_party_a_name is not None
            and self.confirmed_party_a_name == self.confirmed_party_b_name
        ):
            raise ValueError("confirmed contract parties must be distinct")
        if self.confirmed_party_a_name is not None and self.our_party_name is not None:
            expected_our_party = (
                self.confirmed_party_a_name
                if self.perspective == Perspective.PARTY_A
                else self.confirmed_party_b_name
            )
            if self.our_party_name.strip() != expected_our_party:
                raise ValueError("our_party_name must match the confirmed party for the selected perspective")
        return self


class PartyResolutionCreateRequest(StrictModel):
    """Pre-review party identification request; it intentionally has no perspective."""

    contract_version_id: Identifier
    model_pack_id: Identifier | None = None
    schema_version: Literal["1.0"]


class FrameworkMappingModel(StrictModel):
    model_pack_id: Identifier = "api-rerank"
    current_stage: ReviewStage | None = None
    framework_task_id: Identifier | None = None
    framework_run_id: Identifier | None = None
    framework_attempt_no: int | None = Field(default=None, ge=1)

    def mapping_values(self) -> tuple[object, ...]:
        return (
            self.current_stage,
            self.framework_attempt_no,
            self.framework_task_id,
            self.framework_run_id,
        )


class CreateReviewData(FrameworkMappingModel):
    review_id: Identifier
    document_id: Identifier
    status: Literal[ReviewStatus.CREATED, ReviewStatus.RUNNING]
    reused: bool
    schema_version: Literal["1.0"] = "1.0"

    @model_validator(mode="after")
    def validate_mapping(self) -> "CreateReviewData":
        values = self.mapping_values()
        if self.status == ReviewStatus.CREATED and any(value is not None for value in values):
            raise ValueError("CREATED reviews cannot have a Framework mapping")
        if self.status == ReviewStatus.RUNNING and any(value is None for value in values):
            raise ValueError("RUNNING reviews require a complete Framework mapping")
        return self


class PartyResolutionCreateData(FrameworkMappingModel):
    resolution_id: Identifier
    contract_version_id: Identifier
    document_id: Identifier
    status: Literal[ReviewStatus.CREATED, ReviewStatus.RUNNING]
    reused: bool
    schema_version: Literal["1.0"] = "1.0"

    @model_validator(mode="after")
    def validate_mapping(self) -> "PartyResolutionCreateData":
        values = self.mapping_values()
        if self.status == ReviewStatus.CREATED and any(value is not None for value in values):
            raise ValueError("CREATED party resolutions cannot have a Framework mapping")
        if self.status == ReviewStatus.RUNNING and any(value is None for value in values):
            raise ValueError("RUNNING party resolutions require a complete Framework mapping")
        return self


class PartyProfile(StrictModel):
    name: Annotated[str, StringConstraints(min_length=1)]
    name_status: Literal["EXTRACTED", "USER_CONFIRMED", "NOT_STATED"] = "EXTRACTED"

    @model_validator(mode="before")
    @classmethod
    def discard_internal_resolution_marker(cls, value: object) -> object:
        """Accept Framework-only party metadata without exposing it publicly."""
        if not isinstance(value, dict) or "name_resolved" not in value:
            return value
        normalized = dict(value)
        marker = normalized.pop("name_resolved")
        if not isinstance(marker, bool):
            raise ValueError("name_resolved must be a boolean")
        return normalized


class PartyResolutionData(StrictModel):
    party_a: PartyProfile
    party_b: PartyProfile
    perspective: Perspective
    our_party: Annotated[str, StringConstraints(min_length=1)]
    counterparty: Annotated[str, StringConstraints(min_length=1)]

    @model_validator(mode="after")
    def validate_perspective(self) -> "PartyResolutionData":
        expected_our_party = self.party_a.name if self.perspective == Perspective.PARTY_A else self.party_b.name
        expected_counterparty = self.party_b.name if self.perspective == Perspective.PARTY_A else self.party_a.name
        if self.our_party != expected_our_party or self.counterparty != expected_counterparty:
            raise ValueError("our_party and counterparty must match the selected perspective")
        return self


class ReviewStatusData(FrameworkMappingModel):
    review_id: Identifier
    business_task_id: Identifier
    contract_version_id: Identifier
    status: ReviewStatus
    document_id: Identifier
    error: ErrorData | None = None
    schema_version: Literal["1.0"] = "1.0"
    party_resolution: PartyResolutionData | None = None
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_status_shape(self) -> "ReviewStatusData":
        values = self.mapping_values()
        if self.status == ReviewStatus.CREATED and any(value is not None for value in values):
            raise ValueError("CREATED reviews cannot have a Framework mapping")
        if self.status == ReviewStatus.RUNNING and any(value is None for value in values):
            raise ValueError("RUNNING reviews require a complete Framework mapping")
        if self.status == ReviewStatus.FAILED and self.error is None:
            raise ValueError("FAILED reviews require error details")
        if self.status != ReviewStatus.FAILED and self.error is not None:
            raise ValueError("Only FAILED reviews can expose an error")
        return self


class PartyResolutionStatusData(FrameworkMappingModel):
    resolution_id: Identifier
    contract_version_id: Identifier
    status: ReviewStatus
    document_id: Identifier
    error: ErrorData | None = None
    party_a_name: Annotated[str, StringConstraints(min_length=1)] | None = None
    party_b_name: Annotated[str, StringConstraints(min_length=1)] | None = None
    party_a_name_status: Literal["EXTRACTED", "USER_CONFIRMED", "NOT_STATED"] | None = None
    party_b_name_status: Literal["EXTRACTED", "USER_CONFIRMED", "NOT_STATED"] | None = None
    schema_version: Literal["1.0"] = "1.0"
    updated_at: AwareDatetime

    @model_validator(mode="before")
    @classmethod
    def default_legacy_name_statuses(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for role in ("a", "b"):
            name_key = f"party_{role}_name"
            status_key = f"party_{role}_name_status"
            if normalized.get(name_key) is not None and normalized.get(status_key) is None:
                normalized[status_key] = "EXTRACTED"
        return normalized

    @model_validator(mode="after")
    def validate_status_shape(self) -> "PartyResolutionStatusData":
        values = self.mapping_values()
        if self.status == ReviewStatus.CREATED and any(value is not None for value in values):
            raise ValueError("CREATED party resolutions cannot have a Framework mapping")
        if self.status == ReviewStatus.RUNNING and any(value is None for value in values):
            raise ValueError("RUNNING party resolutions require a complete Framework mapping")
        if self.status == ReviewStatus.FAILED and self.error is None:
            raise ValueError("FAILED party resolutions require error details")
        if self.status != ReviewStatus.FAILED and self.error is not None:
            raise ValueError("Only FAILED party resolutions can expose an error")
        if self.party_a_name is not None and self.party_a_name == self.party_b_name:
            raise ValueError("resolved contract parties must be distinct")
        if (self.party_a_name is None) != (self.party_a_name_status is None):
            raise ValueError("party_a_name and party_a_name_status must be returned together")
        if (self.party_b_name is None) != (self.party_b_name_status is None):
            raise ValueError("party_b_name and party_b_name_status must be returned together")
        return self


class CancelReviewData(StrictModel):
    review_id: Identifier
    status: Literal[ReviewStatus.CANCELLED]
    already_terminal: bool


class ContractProfile(StrictModel):
    contract_type: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=80)]
    party_a: PartyProfile
    party_b: PartyProfile
    perspective: Perspective
    our_party: Annotated[str, StringConstraints(min_length=1)]
    counterparty: Annotated[str, StringConstraints(min_length=1)]
    review_attitude: Literal["NEUTRAL"] = "NEUTRAL"

    @model_validator(mode="after")
    def validate_perspective(self) -> "ContractProfile":
        expected_our_party = self.party_a.name if self.perspective == Perspective.PARTY_A else self.party_b.name
        expected_counterparty = self.party_b.name if self.perspective == Perspective.PARTY_A else self.party_a.name
        if self.our_party != expected_our_party or self.counterparty != expected_counterparty:
            raise ValueError("our_party and counterparty must match the selected perspective")
        return self


class ReviewSummary(StrictModel):
    overview: Annotated[str, StringConstraints(min_length=1)]
    high_count: int = Field(ge=0)
    medium_count: int = Field(ge=0)
    low_count: int = Field(ge=0)
    info_count: int = Field(ge=0)


class Finding(StrictModel):
    finding_id: Identifier
    category: FindingCategory
    risk_level: RiskLevel
    title: Annotated[str, StringConstraints(min_length=1)]
    perspective: Perspective
    our_party: Annotated[str, StringConstraints(min_length=1)]
    counterparty: Annotated[str, StringConstraints(min_length=1)]
    issue: Annotated[str, StringConstraints(min_length=1)]
    impact_to_our_party: Annotated[str, StringConstraints(min_length=1)]
    suggestion: Annotated[str, StringConstraints(min_length=1)]
    evidence_ids: list[Identifier] = Field(min_length=1)
    legal_evidence_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_evidence_ids(self) -> "Finding":
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence_ids must be unique")
        if len(self.legal_evidence_ids) != len(set(self.legal_evidence_ids)):
            raise ValueError("legal_evidence_ids must be unique")
        return self


class Evidence(StrictModel):
    evidence_id: Identifier
    finding_id: Identifier
    evidence_type: EvidenceType
    block_id: Identifier | None = None
    page_number: int | None = Field(default=None, ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=1)
    quoted_text: str | None = None
    quoted_text_hash: HashValue | None = None
    checked_scope: Annotated[str, StringConstraints(min_length=1)] | None = None
    verification_note: Annotated[str, StringConstraints(min_length=1)] | None = None
    bounding_boxes: list[None] = Field(default_factory=list, max_length=0)

    @model_validator(mode="after")
    def validate_evidence_shape(self) -> "Evidence":
        text_fields = (
            self.block_id,
            self.char_start,
            self.char_end,
            self.quoted_text,
            self.quoted_text_hash,
        )
        if self.evidence_type in {EvidenceType.TEXT_QUOTE, EvidenceType.CONTEXT}:
            if any(value is None for value in text_fields):
                raise ValueError("Text evidence requires block, range, text, and hash")
            assert self.char_start is not None and self.char_end is not None
            assert self.quoted_text is not None and self.quoted_text_hash is not None
            if self.char_end <= self.char_start:
                raise ValueError("char_end must be greater than char_start")
            if self.char_end - self.char_start != len(self.quoted_text):
                raise ValueError("Text evidence range length must match quoted_text")
            expected_hash = "sha256:" + hashlib.sha256(self.quoted_text.encode("utf-8")).hexdigest()
            if self.quoted_text_hash != expected_hash:
                raise ValueError("quoted_text_hash does not match quoted_text")
        else:
            if self.checked_scope is None or self.verification_note is None:
                raise ValueError("ABSENCE evidence requires checked_scope and verification_note")
            if any(value is not None for value in text_fields) or self.page_number is not None:
                raise ValueError("ABSENCE evidence cannot contain text positioning fields")
        return self


class LegalEvidenceRelationPathReference(StrictModel):
    source_unit_id: Identifier
    target_unit_id: Identifier
    relation_ids: list[Identifier] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_relation_ids(self) -> "LegalEvidenceRelationPathReference":
        if len(self.relation_ids) != len(set(self.relation_ids)):
            raise ValueError("relation_ids must be unique")
        return self


class LegalApplicabilityDecisionReference(StrictModel):
    issue_id: Identifier
    outcome: Literal["APPLICABLE", "REVIEW_DATE_ONLY", "UNKNOWN_METADATA"]
    jurisdiction_decision: Literal["MATCH", "UNKNOWN"]
    temporal_decision: Literal[
        "MATCH",
        "NOT_EFFECTIVE_AT_CONTRACT_DATE",
        "UNKNOWN",
    ]
    review_as_of_date: date
    contract_date: date | None = None
    reasons: list[str] = Field(default_factory=list)


class LegalEvidenceReference(StrictModel):
    """Durable, self-contained legal source referenced by a Finding."""

    evidence_id: Annotated[
        str,
        StringConstraints(pattern=r"^legal-evidence-[0-9a-f]{32}$"),
    ]
    release_id: Identifier
    unit_id: Identifier
    instrument_id: Identifier
    version_id: Identifier
    source_node_ids: list[Identifier] = Field(min_length=1)
    title: Annotated[str, StringConstraints(min_length=1)]
    article_no: Annotated[str, StringConstraints(max_length=160)] | None = None
    heading_path: list[str] = Field(default_factory=list)
    content: Annotated[str, StringConstraints(min_length=1)]
    jurisdiction: Annotated[str, StringConstraints(max_length=128)] | None = None
    authority_level: Annotated[str, StringConstraints(max_length=128)] | None = None
    issuing_authority: Annotated[str, StringConstraints()] | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    validity_status: Literal[
        "ACTIVE",
        "NOT_YET_EFFECTIVE",
        "EXPIRED",
        "REPEALED",
        "UNKNOWN",
    ] | None = None
    metadata_verification_status: Literal["VERIFIED", "UNVERIFIED", "REJECTED"]
    official_source_url: Annotated[str, StringConstraints(max_length=4000)] | None = None
    content_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    check_codes: list[Identifier] = Field(min_length=1)
    issue_ids: list[Identifier] = Field(min_length=1)
    retrieval_channels: list[
        Literal["EXACT", "KEYWORD", "VECTOR", "RELATION"]
    ] = Field(min_length=1)
    relevance_score: float = Field(ge=0, le=1)
    rerank_score: float | None = Field(default=None, ge=0, le=1)
    relation_paths: list[LegalEvidenceRelationPathReference] = Field(
        default_factory=list
    )
    applicability_decisions: list[LegalApplicabilityDecisionReference] = Field(
        min_length=1
    )
    cautions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_reference_identity(self) -> "LegalEvidenceReference":
        if len(self.source_node_ids) != len(set(self.source_node_ids)):
            raise ValueError("source_node_ids must be unique")
        if len(self.check_codes) != len(set(self.check_codes)):
            raise ValueError("check_codes must be unique")
        if len(self.issue_ids) != len(set(self.issue_ids)):
            raise ValueError("issue_ids must be unique")
        if len(self.retrieval_channels) != len(set(self.retrieval_channels)):
            raise ValueError("retrieval_channels must be unique")
        decision_issue_ids = [item.issue_id for item in self.applicability_decisions]
        if len(decision_issue_ids) != len(set(decision_issue_ids)):
            raise ValueError("applicability decisions must be unique per issue")
        if set(decision_issue_ids) != set(self.issue_ids):
            raise ValueError("applicability decisions must cover every evidence issue")
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            raise ValueError("effective_from must not be after effective_to")
        return self


class LegalEvidenceVersionSnapshotReference(StrictModel):
    legal_release_id: Identifier
    legal_projection_version: Identifier
    relation_extractor_version: Identifier
    embedding_model_version: Annotated[str, StringConstraints(max_length=300)] | None = None
    reranker_version: Annotated[str, StringConstraints(max_length=300)] | None = None
    planner_version: Identifier
    review_as_of_date: date
    contract_date: date | None = None


class ReviewResultData(StrictModel):
    rule_review: RuleReviewResult | None = None
    schema_version: Literal["1.0"] = "1.0"
    review_id: Identifier
    business_task_id: Identifier
    contract_version_id: Identifier
    contract_profile: ContractProfile
    summary: ReviewSummary
    findings: list[Finding]
    evidences: list[Evidence]
    legal_evidence_release_id: Identifier | None = None
    legal_evidence_bundle_hash: HashValue | None = None
    legal_evidence_version_snapshot: LegalEvidenceVersionSnapshotReference | None = None
    legal_evidences: list[LegalEvidenceReference] = Field(default_factory=list)
    relationships: list[None] = Field(default_factory=list, max_length=0)
    result_hash: HashValue

    @model_validator(mode="after")
    def validate_result_links_and_counts(self) -> "ReviewResultData":
        if self.rule_review is not None:
            if self.rule_review.review_id != self.review_id:
                raise ValueError("Rule review identity must match the contract review")
            if self.rule_review.perspective != self.contract_profile.perspective.value:
                raise ValueError("Rule review perspective must match the contract review")
        finding_by_id = {finding.finding_id: finding for finding in self.findings}
        if self.rule_review is not None:
            for decision in self.rule_review.decisions:
                if decision.finding_id is not None and decision.finding_id not in finding_by_id:
                    raise ValueError('Rule decision Finding link must resolve in the unified result')
        evidence_by_id = {evidence.evidence_id: evidence for evidence in self.evidences}
        legal_evidence_by_id = {
            evidence.evidence_id: evidence for evidence in self.legal_evidences
        }
        if len(finding_by_id) != len(self.findings):
            raise ValueError("finding_id values must be unique")
        if len(evidence_by_id) != len(self.evidences):
            raise ValueError("evidence_id values must be unique")
        if len(legal_evidence_by_id) != len(self.legal_evidences):
            raise ValueError("legal evidence_id values must be unique")

        referenced_legal_evidence_ids: set[str] = set()

        for finding in self.findings:
            for evidence_id in finding.evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None or evidence.finding_id != finding.finding_id:
                    raise ValueError("Every finding evidence reference must resolve to the same finding")
            for evidence_id in finding.legal_evidence_ids:
                if evidence_id not in legal_evidence_by_id:
                    raise ValueError("Every legal evidence reference must resolve in legal_evidences")
                referenced_legal_evidence_ids.add(evidence_id)
        for evidence in self.evidences:
            finding = finding_by_id.get(evidence.finding_id)
            if finding is None or evidence.evidence_id not in finding.evidence_ids:
                raise ValueError("Every evidence must be referenced by its finding")
        if referenced_legal_evidence_ids != set(legal_evidence_by_id):
            raise ValueError("Every legal evidence must be referenced by at least one finding")
        if self.legal_evidences:
            if self.legal_evidence_release_id is None or self.legal_evidence_bundle_hash is None:
                raise ValueError("Legal evidence catalog requires release and bundle identity")
            if self.legal_evidence_version_snapshot is None:
                raise ValueError("Legal evidence catalog requires a reproducibility version snapshot")
            if (
                self.legal_evidence_version_snapshot.legal_release_id
                != self.legal_evidence_release_id
            ):
                raise ValueError("Legal evidence version snapshot release must match the catalog")
            if any(
                evidence.release_id != self.legal_evidence_release_id
                for evidence in self.legal_evidences
            ):
                raise ValueError("Every legal evidence must belong to the declared release")
        elif (
            self.legal_evidence_release_id is not None
            or self.legal_evidence_bundle_hash is not None
            or self.legal_evidence_version_snapshot is not None
        ):
            raise ValueError("Legal evidence identity cannot exist without cited legal evidence")

        expected_counts = {
            RiskLevel.HIGH: self.summary.high_count,
            RiskLevel.MEDIUM: self.summary.medium_count,
            RiskLevel.LOW: self.summary.low_count,
            RiskLevel.INFO: self.summary.info_count,
        }
        actual_counts = {level: 0 for level in RiskLevel}
        for finding in self.findings:
            actual_counts[finding.risk_level] += 1
            if finding.perspective != self.contract_profile.perspective:
                raise ValueError("Finding perspective must match contract_profile")
            if finding.our_party != self.contract_profile.our_party:
                raise ValueError("Finding our_party must match contract_profile")
            if finding.counterparty != self.contract_profile.counterparty:
                raise ValueError("Finding counterparty must match contract_profile")
        if actual_counts != expected_counts:
            raise ValueError("Summary counts must match findings")
        return self


class PublicReviewSummary(StrictModel):
    overview: Annotated[str, StringConstraints(min_length=1)]
    finding_count: int = Field(ge=0)


class PublicFinding(StrictModel):
    finding_id: Identifier
    category: FindingCategory
    title: Annotated[str, StringConstraints(min_length=1)]
    perspective: Perspective
    our_party: Annotated[str, StringConstraints(min_length=1)]
    counterparty: Annotated[str, StringConstraints(min_length=1)]
    issue: Annotated[str, StringConstraints(min_length=1)]
    impact_to_our_party: Annotated[str, StringConstraints(min_length=1)]
    suggestion: Annotated[str, StringConstraints(min_length=1)]
    evidence_ids: list[Identifier] = Field(min_length=1)
    legal_evidence_ids: list[Identifier] = Field(default_factory=list)


class PublicReviewResultData(StrictModel):
    """Result projection exposed to Java and browsers without risk classification."""
    rule_review: RuleReviewResult | None = None

    schema_version: Literal["1.0"] = "1.0"
    review_id: Identifier
    business_task_id: Identifier
    contract_version_id: Identifier
    contract_profile: ContractProfile
    summary: PublicReviewSummary
    findings: list[PublicFinding]
    evidences: list[Evidence]
    legal_evidence_release_id: Identifier | None = None
    legal_evidence_bundle_hash: HashValue | None = None
    legal_evidence_version_snapshot: LegalEvidenceVersionSnapshotReference | None = None
    legal_evidences: list[LegalEvidenceReference] = Field(default_factory=list)
    relationships: list[None] = Field(default_factory=list, max_length=0)
    result_hash: HashValue

    @classmethod
    def from_internal(cls, value: ReviewResultData) -> "PublicReviewResultData":
        return cls(
            rule_review=value.rule_review,
            schema_version=value.schema_version,
            review_id=value.review_id,
            business_task_id=value.business_task_id,
            contract_version_id=value.contract_version_id,
            contract_profile=value.contract_profile,
            summary=PublicReviewSummary(
                overview=value.summary.overview,
                finding_count=len(value.findings),
            ),
            findings=[
                PublicFinding.model_validate(finding.model_dump(mode="json", exclude={"risk_level"}))
                for finding in value.findings
            ],
            evidences=value.evidences,
            legal_evidence_release_id=value.legal_evidence_release_id,
            legal_evidence_bundle_hash=value.legal_evidence_bundle_hash,
            legal_evidence_version_snapshot=value.legal_evidence_version_snapshot,
            legal_evidences=value.legal_evidences,
            relationships=value.relationships,
            # Keep the durable internal hash so revision-draft and report lookups remain compatible.
            result_hash=value.result_hash,
        )
