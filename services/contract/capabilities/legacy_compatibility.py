"""Deterministic compatibility projection from the seven-unit bundle to legacy artifacts.

This module is intentionally internal.  It does not register a Framework stage or
change the public Contract API.  The five legacy artifact models remain the only
input accepted by the existing consolidation and evidence-verification chain.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contract.api.models import ContractProfile, Evidence, Finding, ReviewSummary
from contract.application.result_hash import compute_result_hash
from contract.callback.models import (
    CommercialTermsStageResult,
    EvidenceCandidate as LegacyEvidenceCandidate,
    FindingConsolidationArtifact,
    LiabilityTerminationStageResult,
    MissingAmbiguityStageResult,
    RelationExtractionStageResult,
    RightsObligationsStageResult,
)
from contract.errors import ContractError
from contract.evidence import materialize_evidence_set
from contract.review import merge_review_stage_results, namespace_review_stage_result
from contract.risk.playbooks import PlaybookRegistry, build_default_registry
from services.contract.capabilities.risk_review import (
    FindingDraft,
)


COMPATIBILITY_ROUTING_VERSION = "1.0"

LegacyArtifactType = Literal[
    "rights_obligations_review_result",
    "commercial_terms_review_result",
    "liability_termination_review_result",
    "missing_ambiguous_clauses_result",
    "relation_extraction_result",
]

_RISK_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
_ARTIFACT_ORDER: tuple[LegacyArtifactType, ...] = (
    "rights_obligations_review_result",
    "commercial_terms_review_result",
    "liability_termination_review_result",
    "missing_ambiguous_clauses_result",
    "relation_extraction_result",
)
_STAGE_BY_ARTIFACT = {
    "rights_obligations_review_result": "rights_obligations_review",
    "commercial_terms_review_result": "commercial_terms_review",
    "liability_termination_review_result": "liability_termination_review",
    "missing_ambiguous_clauses_result": "missing_ambiguous_clauses",
    "relation_extraction_result": "relation_extraction",
}
_RESULT_TYPE_BY_ARTIFACT = {
    "rights_obligations_review_result": "RIGHTS_OBLIGATIONS_STAGE_V1",
    "commercial_terms_review_result": "COMMERCIAL_TERMS_STAGE_V1",
    "liability_termination_review_result": "LIABILITY_TERMINATION_STAGE_V1",
    "missing_ambiguous_clauses_result": "MISSING_AMBIGUITY_STAGE_V1",
    "relation_extraction_result": "RELATION_EXTRACTION_STAGE_V1",
}
_MODEL_BY_ARTIFACT = {
    "rights_obligations_review_result": RightsObligationsStageResult,
    "commercial_terms_review_result": CommercialTermsStageResult,
    "liability_termination_review_result": LiabilityTerminationStageResult,
    "missing_ambiguous_clauses_result": MissingAmbiguityStageResult,
    "relation_extraction_result": RelationExtractionStageResult,
}
_CATEGORIES_BY_ARTIFACT = {
    "rights_obligations_review_result": {
        "PARTY_IDENTIFICATION",
        "RIGHTS_OBLIGATIONS_IMBALANCE",
        "OTHER",
    },
    "commercial_terms_review_result": {"PAYMENT", "DELIVERY", "ACCEPTANCE"},
    "liability_termination_review_result": {
        "BREACH",
        "LIABILITY",
        "TERMINATION",
        "CONFIDENTIALITY",
        "INTELLECTUAL_PROPERTY",
        "DISPUTE_RESOLUTION",
    },
    "missing_ambiguous_clauses_result": {"MISSING_CLAUSE", "AMBIGUITY"},
    "relation_extraction_result": {"INTERNAL_CONFLICT"},
}

# Canonical roots may be more specific than the frozen CheckSpec risk type.
# The list is deliberately closed: unknown values are not routed by text or by
# an OTHER fallback.
_EXTRA_RISK_TYPES_BY_CHECK: dict[str, set[str]] = {
    "CCC-001": {"EFFECTIVE_DATE_CHRONOLOGY_CONFLICT"},
    "CCC-005": {"PARTY_TERM_IDENTITY_CONFLICT"},
    "MAC-005": {"REFERENCED_ATTACHMENT_MISSING"},
    "LRE-001": {"UNBOUNDED_LIABILITY_EXPOSURE", "BROAD_BREACH_TRIGGER_REVIEW"},
    "LRE-002": {
        "CUMULATIVE_REMEDIES_REVIEW",
        "OVERBROAD_LOSS_SCOPE_REVIEW",
        "UNBOUNDED_LIABILITY_EXPOSURE",
    },
    "LRE-003": {
        "UNBOUNDED_LIABILITY_EXPOSURE",
        "LIABILITY_CAP_ABSENT",
        "LIABILITY_CAP_BYPASS_REVIEW",
    },
    "LRE-004": {"UNBOUNDED_LIABILITY_EXPOSURE", "OVERBROAD_INDEMNITY_REVIEW"},
    "LRE-005": {"TERMINATION_RIGHTS_REVIEW"},
    "LRE-006": {
        "TERMINATION_SETTLEMENT_REVIEW",
        "TERMINATION_SETTLEMENT_ABSENT",
    },
    "LRE-007": {"FORCE_MAJEURE_MECHANISM_ABSENT"},
    "LRE-008": {"DISPUTE_RESOLUTION_ABSENT"},
    "ICD-001": {"FOREGROUND_IP_OWNERSHIP_REVIEW", "FOREGROUND_IP_OWNERSHIP_ABSENT"},
    "ICD-002": {"BACKGROUND_IP_LICENSE_REVIEW", "BACKGROUND_IP_LICENSE_ABSENT"},
    "ICD-003": {
        "THIRD_PARTY_IP_PROTECTION_REVIEW",
        "THIRD_PARTY_IP_PROTECTION_ABSENT",
    },
    "ICD-004": {
        "CONFIDENTIALITY_PROTECTION_REVIEW",
        "CONFIDENTIALITY_COMPLETENESS_ABSENT",
    },
    "ICD-005": {
        "DATA_PROCESSING_SECURITY_REVIEW",
        "DATA_PROCESSING_SECURITY_ABSENT",
    },
    "ICD-006": {"DATA_RETURN_DELETION_REVIEW", "DATA_RETURN_DELETION_ABSENT"},
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompatibilityRoutingRecord(StrictModel):
    source_unit_id: str = Field(min_length=1, max_length=160)
    source_check_code: str = Field(pattern=r"^[A-Z]{2,3}-[0-9]{3}$")
    source_root_id: str = Field(min_length=1, max_length=160)
    source_finding_id: str = Field(pattern=r"^finding-[0-9a-f]{32}$")
    compatible_finding_id: str = Field(pattern=r"^finding-[0-9a-f]{32}$")
    category: str = Field(min_length=1, max_length=80)
    risk_type: str = Field(min_length=1, max_length=160)
    owner_type: Literal["BASE_DOMAIN", "HORIZONTAL"]
    linked_base_finding_ids: list[str] = Field(default_factory=list)
    legacy_stage_id: str = Field(min_length=1, max_length=160)
    legacy_artifact_type: LegacyArtifactType
    routing_rule_id: str = Field(min_length=1, max_length=160)
    routing_reason: str = Field(min_length=1, max_length=1000)
    routing_version: Literal["1.0"] = COMPATIBILITY_ROUTING_VERSION


class LegacyArtifactSet(StrictModel):
    rights_obligations_review_result: RightsObligationsStageResult
    commercial_terms_review_result: CommercialTermsStageResult
    liability_termination_review_result: LiabilityTerminationStageResult
    missing_ambiguous_clauses_result: MissingAmbiguityStageResult
    relation_extraction_result: RelationExtractionStageResult

    def as_artifact_dict(self) -> dict[str, dict[str, Any]]:
        return {
            artifact_type: getattr(self, artifact_type).model_dump(mode="json")
            for artifact_type in _ARTIFACT_ORDER
        }


class CompatibilityMetrics(StrictModel):
    source_finding_count: int = Field(ge=0)
    routed_finding_count: int = Field(ge=0)
    suppressed_horizontal_finding_count: int = Field(ge=0)
    legacy_artifact_count: Literal[5] = 5
    adapter_duration_ms: int = Field(ge=0)
    adapter_model_call_count: Literal[0] = 0
    pre_merge_finding_count: int = Field(ge=0)
    candidate_pair_count: int = Field(ge=0)
    model_pair_call_count: int = Field(ge=0)
    same_risk_count: int = Field(ge=0)
    related_distinct_count: int = Field(ge=0)
    distinct_count: int = Field(ge=0)
    post_merge_finding_count: int = Field(ge=0)


class LegacyCompatibilityContext(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    review_id: str = Field(min_length=1, max_length=160)
    business_task_id: str = Field(min_length=1, max_length=160)
    contract_version_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    contract_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    contract_profile: ContractProfile


class LegacyCompatibleRiskReviewResult(StrictModel):
    result_type: Literal["LEGACY_COMPATIBLE_RISK_REVIEW_V1"] = (
        "LEGACY_COMPATIBLE_RISK_REVIEW_V1"
    )
    source_extended_bundle_id: str = Field(min_length=1, max_length=160)
    compatibility_routing_version: Literal["1.0"] = COMPATIBILITY_ROUTING_VERSION
    legacy_artifacts: LegacyArtifactSet
    routing_records: list[CompatibilityRoutingRecord]
    merge_status: Literal["COMPLETED", "SKIPPED"]
    merge_artifact: FindingConsolidationArtifact
    metrics: CompatibilityMetrics
    evidence_verification_status: Literal["VERIFIED"]
    final_findings: list[Finding]
    final_evidence: list[Evidence]
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class CompatibilityProjection:
    bundle_id: str
    artifacts: LegacyArtifactSet
    routing_records: tuple[CompatibilityRoutingRecord, ...]
    adapter_duration_ms: int
    source_finding_count: int
    suppressed_horizontal_finding_count: int


class FindingCompatibilityRouter:
    """Closed, deterministic Finding-to-legacy-artifact router."""

    def __init__(self, registry: PlaybookRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()
        self._check_to_artifact = {
            item.check_code: item.legacy_artifact_type for item in self.registry.checks
        }
        self._allowed_risk_types = {
            item.check_code: {
                *item.allowed_risk_types,
                *_EXTRA_RISK_TYPES_BY_CHECK.get(item.check_code, set()),
            }
            for item in self.registry.checks
        }

    def route(
        self,
        finding: FindingDraft,
        *,
        source_root_id: str,
        owner_type: Literal["BASE_DOMAIN", "HORIZONTAL"],
        linked_base_finding_ids: Sequence[str] = (),
    ) -> CompatibilityRoutingRecord:
        artifact_type = self._check_to_artifact.get(finding.check_code)
        if artifact_type is None:
            raise _error(
                "RISK_LEGACY_ARTIFACT_ROUTE_NOT_FOUND",
                f"No compatibility route for check {finding.check_code}",
            )
        if finding.risk_type not in self._allowed_risk_types[finding.check_code]:
            raise _error(
                "RISK_LEGACY_ARTIFACT_ROUTE_NOT_FOUND",
                f"Unknown risk_type for {finding.check_code}: {finding.risk_type}",
            )
        if finding.category == "OTHER":
            if not (
                finding.source_unit_id == "formation_validity_authority"
                and finding.check_code == "FVA-005"
                and finding.risk_type == "MANDATORY_RULE_OR_VALIDITY_RISK"
                and artifact_type == "rights_obligations_review_result"
            ):
                raise _error(
                    "RISK_INVALID_OTHER_CATEGORY",
                    "Only FVA-005 may use category OTHER",
                )
        elif finding.check_code == "FVA-005":
            raise _error(
                "RISK_FVA005_COMPATIBILITY_ROUTE_INVALID",
                "FVA-005 must use the frozen OTHER compatibility route",
            )
        if finding.category not in _CATEGORIES_BY_ARTIFACT[artifact_type]:
            raise _error(
                "RISK_LEGACY_ARTIFACT_ROUTE_NOT_FOUND",
                f"Category {finding.category} is incompatible with {artifact_type}",
            )
        expected_domain = self.registry.check(finding.check_code).domain
        if finding.source_unit_id != expected_domain or finding.domain != expected_domain:
            raise _error(
                "RISK_LEGACY_ARTIFACT_ROUTE_NOT_FOUND",
                f"Finding domain does not match {finding.check_code}",
            )
        rule_id = (
            "compat-fva005-other-v1"
            if finding.check_code == "FVA-005"
            else (
                f"compat-horizontal-{finding.check_code.lower()}-v1"
                if owner_type == "HORIZONTAL"
                else f"compat-check-{finding.check_code.lower()}-v1"
            )
        )
        compatible_finding_id = _stable_id(
            "finding",
            {
                "routing_version": COMPATIBILITY_ROUTING_VERSION,
                "source_unit_id": finding.source_unit_id,
                "check_code": finding.check_code,
                "root_id": source_root_id,
                "source_finding_id": finding.finding_local_id,
            },
        )
        return CompatibilityRoutingRecord(
            source_unit_id=finding.source_unit_id,
            source_check_code=finding.check_code,
            source_root_id=source_root_id,
            source_finding_id=finding.finding_local_id,
            compatible_finding_id=compatible_finding_id,
            category=finding.category,
            risk_type=finding.risk_type,
            owner_type=owner_type,
            linked_base_finding_ids=sorted(set(linked_base_finding_ids)),
            legacy_stage_id=_STAGE_BY_ARTIFACT[artifact_type],
            legacy_artifact_type=artifact_type,
            routing_rule_id=rule_id,
            routing_reason=(
                f"{finding.check_code}/{finding.risk_type} is owned by "
                f"{finding.source_unit_id} and maps to the frozen legacy artifact"
            ),
        )


class LegacyRiskArtifactAdapter:
    def __init__(self, router: FindingCompatibilityRouter | None = None) -> None:
        self.router = router or FindingCompatibilityRouter()

    def adapt(self, extended_bundle: Mapping[str, Any]) -> CompatibilityProjection:
        started = time.perf_counter()
        bundle_id = _required_str(extended_bundle, "bundle_id")
        raw_findings = extended_bundle.get("findings")
        if not isinstance(raw_findings, list):
            raise _error("RISK_LEGACY_ARTIFACT_SCHEMA_INVALID", "Bundle findings are missing")
        root_by_finding, ownership_by_finding, suppressed = _bundle_trace(extended_bundle)
        findings_by_artifact: dict[str, list[Finding]] = {
            key: [] for key in _ARTIFACT_ORDER
        }
        evidence_by_artifact: dict[str, list[LegacyEvidenceCandidate]] = {
            key: [] for key in _ARTIFACT_ORDER
        }
        records: list[CompatibilityRoutingRecord] = []
        seen_source_ids: set[str] = set()
        seen_compatible_ids: set[str] = set()
        for raw in sorted(raw_findings, key=_source_finding_sort_key):
            finding = FindingDraft.model_validate(raw)
            if finding.finding_local_id in seen_source_ids:
                raise _error(
                    "RISK_LEGACY_FINDING_ID_CONFLICT",
                    "Source Finding ID is duplicated",
                )
            seen_source_ids.add(finding.finding_local_id)
            root_id = root_by_finding.get(finding.finding_local_id, finding.finding_local_id)
            owner, linked = ownership_by_finding.get(
                finding.finding_local_id,
                (
                    "HORIZONTAL"
                    if finding.source_unit_id
                    in {"cross_clause_consistency", "missing_ambiguity_completeness"}
                    else "BASE_DOMAIN",
                    (),
                ),
            )
            record = self.router.route(
                finding,
                source_root_id=root_id,
                owner_type=owner,
                linked_base_finding_ids=linked,
            )
            if record.compatible_finding_id in seen_compatible_ids:
                raise _error(
                    "RISK_LEGACY_FINDING_ID_CONFLICT",
                    "Compatible Finding ID is duplicated",
                )
            seen_compatible_ids.add(record.compatible_finding_id)
            legacy_evidence = _legacy_evidence(finding, record.compatible_finding_id)
            compatible_finding = Finding(
                finding_id=record.compatible_finding_id,
                category=finding.category,
                risk_level=finding.risk_level,
                title=finding.title,
                perspective=finding.perspective,
                our_party=finding.our_party,
                counterparty=finding.counterparty,
                issue=finding.issue,
                impact_to_our_party=finding.impact_to_our_party,
                suggestion=finding.suggestion,
                evidence_ids=[item.evidence_id for item in legacy_evidence],
            )
            artifact_type = record.legacy_artifact_type
            findings_by_artifact[artifact_type].append(compatible_finding)
            evidence_by_artifact[artifact_type].extend(legacy_evidence)
            records.append(record)

        artifacts: dict[str, Any] = {}
        for artifact_type in _ARTIFACT_ORDER:
            findings = sorted(
                findings_by_artifact[artifact_type],
                key=lambda item: (
                    _RISK_RANK[item.risk_level.value],
                    _record_for(records, item.finding_id).source_check_code,
                    _record_for(records, item.finding_id).risk_type,
                    _record_for(records, item.finding_id).source_root_id,
                    item.finding_id,
                ),
            )
            evidence = sorted(
                evidence_by_artifact[artifact_type],
                key=lambda item: (item.finding_id, item.evidence_id),
            )
            payload: dict[str, Any] = {
                "result_type": _RESULT_TYPE_BY_ARTIFACT[artifact_type],
                "findings": [item.model_dump(mode="json") for item in findings],
                "evidences": [item.model_dump(mode="json") for item in evidence],
            }
            if artifact_type == "relation_extraction_result":
                payload["internal_relationships"] = []
            artifacts[artifact_type] = _MODEL_BY_ARTIFACT[artifact_type].model_validate(
                payload
            )

        artifact_set = LegacyArtifactSet(**artifacts)
        _validate_projection_conservation(
            source_finding_count=len(raw_findings),
            artifact_set=artifact_set,
            routing_records=records,
            suppressed_count=suppressed,
        )
        return CompatibilityProjection(
            bundle_id=bundle_id,
            artifacts=artifact_set,
            routing_records=tuple(
                sorted(records, key=lambda item: item.compatible_finding_id)
            ),
            adapter_duration_ms=round((time.perf_counter() - started) * 1000),
            source_finding_count=len(raw_findings),
            suppressed_horizontal_finding_count=suppressed,
        )


def finalize_legacy_compatible_result(
    projection: CompatibilityProjection,
    *,
    context: LegacyCompatibilityContext,
    consolidation: FindingConsolidationArtifact,
    blocks: Sequence[Mapping[str, Any]],
) -> LegacyCompatibleRiskReviewResult:
    """Run the existing deterministic merge, evidence materialization and hash."""

    _validate_projection_integrity(projection)
    stages = []
    finding_reference_ids: dict[tuple[str, str], str] = {}
    for artifact_type in _ARTIFACT_ORDER:
        source_stage = getattr(projection.artifacts, artifact_type)
        namespaced = namespace_review_stage_result(artifact_type, source_stage)
        stages.append(namespaced)
        finding_reference_ids.update(
            {
                (artifact_type, source.finding_id): target.finding_id
                for source, target in zip(
                    source_stage.findings,
                    namespaced.findings,
                    strict=True,
                )
            }
        )
    findings, candidates = merge_review_stage_results(
        stages,
        consolidation=consolidation,
        finding_reference_ids=finding_reference_ids,
    )
    evidence = materialize_evidence_set(
        findings,
        candidates,
        blocks,
        context.contract_profile,
    )
    counts = Counter(item.risk_level.value for item in findings)
    final_hash, _ = compute_result_hash(
        {
            "schema_version": "1.0",
            "review_id": context.review_id,
            "business_task_id": context.business_task_id,
            "contract_version_id": context.contract_version_id,
            "contract_profile": context.contract_profile.model_dump(mode="json"),
            "summary": ReviewSummary(
                overview=(
                    f"发现{len(findings)}项需要人工复核的合同事项。"
                    if findings
                    else "未发现需要人工复核的实质合同风险。"
                ),
                high_count=counts["HIGH"],
                medium_count=counts["MEDIUM"],
                low_count=counts["LOW"],
                info_count=counts["INFO"],
            ).model_dump(mode="json"),
            "findings": [item.model_dump(mode="json") for item in findings],
            "evidences": [item.model_dump(mode="json") for item in evidence],
            "relationships": [],
        }
    )
    relations = Counter(item.relation for item in consolidation.decisions)
    metrics = CompatibilityMetrics(
        source_finding_count=projection.source_finding_count,
        routed_finding_count=sum(
            len(getattr(projection.artifacts, key).findings)
            for key in _ARTIFACT_ORDER
        ),
        suppressed_horizontal_finding_count=projection.suppressed_horizontal_finding_count,
        adapter_duration_ms=projection.adapter_duration_ms,
        pre_merge_finding_count=sum(
            len(getattr(projection.artifacts, key).findings)
            for key in _ARTIFACT_ORDER
        ),
        candidate_pair_count=consolidation.candidate_count,
        model_pair_call_count=consolidation.model_call_count,
        same_risk_count=relations["SAME_RISK"],
        related_distinct_count=relations["RELATED_DISTINCT"],
        distinct_count=relations["DISTINCT"],
        post_merge_finding_count=len(findings),
    )
    return LegacyCompatibleRiskReviewResult(
        source_extended_bundle_id=projection.bundle_id,
        legacy_artifacts=projection.artifacts,
        routing_records=list(projection.routing_records),
        merge_status=consolidation.status,
        merge_artifact=consolidation,
        metrics=metrics,
        evidence_verification_status="VERIFIED",
        final_findings=findings,
        final_evidence=evidence,
        result_hash=final_hash,
    )


def _validate_projection_integrity(projection: CompatibilityProjection) -> None:
    records = list(projection.routing_records)
    if len({item.source_finding_id for item in records}) != len(records):
        raise _error(
            "RISK_LEGACY_FINDING_ID_CONFLICT",
            "A source Finding is routed more than once",
        )
    if len({item.compatible_finding_id for item in records}) != len(records):
        raise _error(
            "RISK_LEGACY_FINDING_ID_CONFLICT",
            "A compatible Finding is routed more than once",
        )
    record_by_id = {item.compatible_finding_id: item for item in records}
    finding_locations: dict[str, list[str]] = {}
    evidence_ids: list[str] = []
    for artifact_type in _ARTIFACT_ORDER:
        artifact = getattr(projection.artifacts, artifact_type)
        for finding in artifact.findings:
            finding_locations.setdefault(finding.finding_id, []).append(artifact_type)
        evidence_ids.extend(item.evidence_id for item in artifact.evidences)
    if len(evidence_ids) != len(set(evidence_ids)):
        raise _error(
            "RISK_LEGACY_EVIDENCE_ID_CONFLICT",
            "Compatible Evidence IDs must be globally unique",
        )
    if set(finding_locations) != set(record_by_id):
        raise _error(
            "RISK_LEGACY_ARTIFACT_SCHEMA_INVALID",
            "Compatibility records and Artifact Findings differ",
        )
    for finding_id, locations in finding_locations.items():
        record = record_by_id[finding_id]
        if locations != [record.legacy_artifact_type]:
            raise _error(
                "RISK_LEGACY_MULTI_ARTIFACT_ROUTE",
                "A Finding must have exactly one compatible Artifact location",
            )


def _bundle_trace(
    bundle: Mapping[str, Any],
) -> tuple[
    dict[str, str],
    dict[str, tuple[Literal["BASE_DOMAIN", "HORIZONTAL"], tuple[str, ...]]],
    int,
]:
    root_by_finding: dict[str, str] = {}
    ownership: dict[
        str, tuple[Literal["BASE_DOMAIN", "HORIZONTAL"], tuple[str, ...]]
    ] = {}
    base_bundle = bundle.get("base_bundle")
    if not isinstance(base_bundle, Mapping):
        raise _error("RISK_LEGACY_ARTIFACT_SCHEMA_INVALID", "Base bundle is missing")
    units = base_bundle.get("units")
    if not isinstance(units, list):
        raise _error("RISK_LEGACY_ARTIFACT_SCHEMA_INVALID", "Base units are missing")
    for unit in units:
        if not isinstance(unit, Mapping):
            continue
        for root in unit.get("canonical_risk_roots", []):
            if isinstance(root, Mapping):
                finding_id = root.get("finding_local_id")
                root_id = root.get("root_id")
                if isinstance(finding_id, str) and isinstance(root_id, str):
                    root_by_finding[finding_id] = root_id
                    ownership[finding_id] = ("BASE_DOMAIN", ())

    suppressed = 0
    horizontal_units = bundle.get("horizontal_units")
    if not isinstance(horizontal_units, list):
        raise _error("RISK_LEGACY_ARTIFACT_SCHEMA_INVALID", "Horizontal units are missing")
    for unit in horizontal_units:
        if not isinstance(unit, Mapping):
            continue
        decisions = {
            item.get("candidate_id"): item
            for item in unit.get("decisions", [])
            if isinstance(item, Mapping) and isinstance(item.get("candidate_id"), str)
        }
        for decision in decisions.values():
            if decision.get("owner_type") in {"BASE_DOMAIN", "SHARED_CONTEXT_ONLY"}:
                suppressed += 1
        for root in unit.get("canonical_roots", []):
            if not isinstance(root, Mapping):
                continue
            finding_id = root.get("finding_local_id")
            root_id = root.get("root_id")
            candidate_ids = root.get("candidate_ids")
            if not (
                isinstance(finding_id, str)
                and isinstance(root_id, str)
                and isinstance(candidate_ids, list)
            ):
                continue
            root_by_finding[finding_id] = root_id
            linked: set[str] = set()
            for candidate_id in candidate_ids:
                decision = decisions.get(candidate_id)
                if not isinstance(decision, Mapping):
                    continue
                owner = decision.get("owner_type")
                if owner != "HORIZONTAL":
                    raise _error(
                        "RISK_HORIZONTAL_OWNERSHIP_INVALID",
                        "A materialized horizontal Finding is not HORIZONTAL-owned",
                    )
                linked.update(
                    item
                    for item in decision.get("linked_base_finding_ids", [])
                    if isinstance(item, str)
                )
            ownership[finding_id] = ("HORIZONTAL", tuple(sorted(linked)))
    return root_by_finding, ownership, suppressed


def _legacy_evidence(
    finding: FindingDraft,
    compatible_finding_id: str,
) -> list[LegacyEvidenceCandidate]:
    values: list[LegacyEvidenceCandidate] = []
    seen: set[str] = set()
    for source in sorted(
        finding.evidence_candidates,
        key=lambda item: (
            item.evidence_type,
            item.block_id or "",
            item.char_start if item.char_start is not None else -1,
            item.char_end if item.char_end is not None else -1,
            item.evidence_local_id,
        ),
    ):
        evidence_id = _stable_id(
            "evidence",
            {
                "routing_version": COMPATIBILITY_ROUTING_VERSION,
                "compatible_finding_id": compatible_finding_id,
                "source_evidence_id": source.evidence_local_id,
                "source_shape": source.model_dump(mode="json"),
            },
        )
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        values.append(
            LegacyEvidenceCandidate(
                evidence_id=evidence_id,
                finding_id=compatible_finding_id,
                evidence_type=source.evidence_type,
                block_id=source.block_id,
                page_number=source.page_number,
                char_start=source.char_start,
                char_end=source.char_end,
                quoted_text=source.quoted_text,
                quoted_text_hash=source.quoted_text_hash,
                checked_scope=source.checked_scope,
                verification_note=source.verification_note,
                bounding_boxes=[],
            )
        )
    if not values:
        raise _error(
            "RISK_LEGACY_EVIDENCE_INVALID",
            "A compatible Finding has no Evidence",
        )
    return values


def _validate_projection_conservation(
    *,
    source_finding_count: int,
    artifact_set: LegacyArtifactSet,
    routing_records: Sequence[CompatibilityRoutingRecord],
    suppressed_count: int,
) -> None:
    routed = sum(
        len(getattr(artifact_set, artifact_type).findings)
        for artifact_type in _ARTIFACT_ORDER
    )
    if routed != len(routing_records) or routed != source_finding_count:
        raise _error(
            "RISK_LEGACY_ARTIFACT_CONSERVATION_FAILED",
            "Compatibility routing did not preserve every materialized Finding exactly once",
        )
    finding_ids = [
        item.finding_id
        for artifact_type in _ARTIFACT_ORDER
        for item in getattr(artifact_set, artifact_type).findings
    ]
    if len(finding_ids) != len(set(finding_ids)):
        raise _error(
            "RISK_LEGACY_FINDING_ID_CONFLICT",
            "A Finding was routed to more than one legacy Artifact",
        )
    if suppressed_count < 0:
        raise _error("RISK_HORIZONTAL_OWNERSHIP_INVALID", "Invalid suppression count")


def _source_finding_sort_key(value: Any) -> tuple[str, str, str, str]:
    if not isinstance(value, Mapping):
        return "", "", "", ""
    return (
        str(value.get("source_unit_id") or ""),
        str(value.get("check_code") or ""),
        str(value.get("risk_type") or ""),
        str(value.get("finding_local_id") or ""),
    )


def _record_for(
    records: Sequence[CompatibilityRoutingRecord],
    finding_id: str,
) -> CompatibilityRoutingRecord:
    for record in records:
        if record.compatible_finding_id == finding_id:
            return record
    raise _error(
        "RISK_LEGACY_ARTIFACT_CONSERVATION_FAILED",
        "A compatible Finding has no routing record",
    )


def _stable_id(prefix: str, value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _required_str(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise _error("RISK_LEGACY_ARTIFACT_SCHEMA_INVALID", f"Missing {key}")
    return result


def _error(code: str, message: str) -> ContractError:
    return ContractError(code, message, status_code=422)
