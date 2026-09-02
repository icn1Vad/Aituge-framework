from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from contract.api.models import (
    ContractProfile,
    LegalEvidenceReference,
    LegalEvidenceVersionSnapshotReference,
    PublicReviewResultData,
    ReviewResultData,
    ReviewSummary,
)
from contract.callback.models import FindingConsolidationArtifact
from contract.errors import ContractError

from services.contract.capabilities.finding_consolidation import (
    FindingRef,
    make_pair_id,
)
from services.contract.capabilities.legacy_compatibility import (
    FindingCompatibilityRouter,
    LegacyCompatibilityContext,
    LegacyRiskArtifactAdapter,
    finalize_legacy_compatible_result,
)
from services.contract.capabilities.risk_review import FindingDraft

BLOCK_TEXT = "Party A must prepay the full price. Party B has unlimited liability."
BLOCK = {
    "block_id": "block-1",
    "block_no": 1,
    "page_number": None,
    "text": BLOCK_TEXT,
}


def test_routes_base_and_horizontal_findings_to_five_strict_legacy_artifacts() -> None:
    bundle = _bundle(
        [
            _finding("1", "PO-001", "performance_obligations", "RIGHTS_OBLIGATIONS_IMBALANCE", "CORE_OBLIGATION_SCOPE_RISK", 0, 14),
            _finding("2", "CF-005", "commercial_financial", "PAYMENT", "ADVANCE_PAYMENT_SECURITY_RISK", 0, 14),
            _finding("3", "ICD-004", "ip_confidentiality_data", "CONFIDENTIALITY", "CONFIDENTIALITY_SCOPE_RISK", 15, 27),
            _finding("4", "MAC-005", "missing_ambiguity_completeness", "MISSING_CLAUSE", "REFERENCED_ATTACHMENT_MISSING", 0, 14),
            _finding("5", "CCC-001", "cross_clause_consistency", "INTERNAL_CONFLICT", "EFFECTIVE_DATE_CHRONOLOGY_CONFLICT", 0, 14),
        ]
    )

    projection = LegacyRiskArtifactAdapter().adapt(bundle)

    assert len(projection.routing_records) == 5
    artifacts = projection.artifacts
    assert len(artifacts.rights_obligations_review_result.findings) == 1
    assert len(artifacts.commercial_terms_review_result.findings) == 1
    assert len(artifacts.liability_termination_review_result.findings) == 1
    assert len(artifacts.missing_ambiguous_clauses_result.findings) == 1
    assert len(artifacts.relation_extraction_result.findings) == 1
    assert artifacts.relation_extraction_result.internal_relationships == []
    assert {item.owner_type for item in projection.routing_records} == {
        "BASE_DOMAIN",
        "HORIZONTAL",
    }


def test_legal_evidence_ids_survive_compatibility_and_public_projection() -> None:
    raw = _finding(
        "1",
        "CF-005",
        "commercial_financial",
        "PAYMENT",
        "ADVANCE_PAYMENT_SECURITY_RISK",
        0,
        14,
    )
    raw["legal_evidence_ids"] = ["legal-evidence-" + "1" * 32]
    projection = LegacyRiskArtifactAdapter().adapt(_bundle([raw]))
    compatible = projection.artifacts.commercial_terms_review_result.findings[0]
    assert compatible.legal_evidence_ids == raw["legal_evidence_ids"]

    consolidation = FindingConsolidationArtifact(
        result_type="FINDING_CONSOLIDATION_V1",
        status="SKIPPED",
        candidate_count=0,
        model_call_count=0,
        decisions=[],
        skip_reason="SINGLE_FINDING",
    )
    finalized = finalize_legacy_compatible_result(
        projection,
        context=_context(),
        consolidation=consolidation,
        blocks=[BLOCK],
    )
    assert finalized.final_findings[0].legal_evidence_ids == raw["legal_evidence_ids"]

    context = _context()
    internal = ReviewResultData(
        review_id=context.review_id,
        business_task_id=context.business_task_id,
        contract_version_id=context.contract_version_id,
        contract_profile=context.contract_profile,
        summary=ReviewSummary(
            overview="完成",
            high_count=1,
            medium_count=0,
            low_count=0,
            info_count=0,
        ),
        findings=finalized.final_findings,
        evidences=finalized.final_evidence,
            legal_evidence_release_id="release-legal-test",
            legal_evidence_bundle_hash="sha256:" + "2" * 64,
            legal_evidence_version_snapshot=LegalEvidenceVersionSnapshotReference(
                legal_release_id="release-legal-test",
                legal_projection_version="legal-evidence-projection-v4",
                relation_extractor_version="legal-relation-extractor-v2",
                embedding_model_version="embedding-test-v1",
                reranker_version="reranker-test-v1",
                planner_version="adaptive-legal-planner-v2",
                review_as_of_date="2026-09-02",
                contract_date="2026-08-01",
            ),
        legal_evidences=[
            LegalEvidenceReference(
                evidence_id=raw["legal_evidence_ids"][0],
                release_id="release-legal-test",
                unit_id="unit-legal-test",
                instrument_id="instrument-legal-test",
                version_id="version-legal-test",
                source_node_ids=["node-legal-test"],
                title="中华人民共和国民法典",
                article_no="第五百零九条",
                heading_path=["第三编 合同"],
                content="当事人应当按照约定全面履行自己的义务。",
                jurisdiction="CN",
                authority_level="LAW",
                issuing_authority="全国人民代表大会",
                validity_status="UNKNOWN",
                metadata_verification_status="UNVERIFIED",
                content_hash="3" * 64,
                check_codes=["CF-005"],
                issue_ids=["legal-issue-" + "4" * 32],
                retrieval_channels=["KEYWORD"],
                relevance_score=0.9,
                applicability_decisions=[
                    {
                        "issue_id": "legal-issue-" + "4" * 32,
                        "outcome": "UNKNOWN_METADATA",
                        "jurisdiction_decision": "MATCH",
                        "temporal_decision": "UNKNOWN",
                        "review_as_of_date": "2026-09-02",
                        "contract_date": "2026-08-01",
                        "reasons": ["法规时效元数据尚未核验"],
                    }
                ],
                cautions=["LEGAL_VALIDITY_UNVERIFIED"],
            )
        ],
        result_hash="sha256:" + "0" * 64,
    )
    public = PublicReviewResultData.from_internal(internal)
    assert public.findings[0].legal_evidence_ids == raw["legal_evidence_ids"]


def test_fva005_is_the_only_other_route() -> None:
    valid = _finding(
        "1",
        "FVA-005",
        "formation_validity_authority",
        "OTHER",
        "MANDATORY_RULE_OR_VALIDITY_RISK",
        0,
        14,
    )
    route = FindingCompatibilityRouter().route(
        FindingDraft.model_validate(valid),
        source_root_id="root-fva005",
        owner_type="BASE_DOMAIN",
    )
    assert route.legacy_artifact_type == "rights_obligations_review_result"
    assert route.routing_rule_id == "compat-fva005-other-v1"

    invalid = dict(valid)
    invalid["check_code"] = "PO-001"
    invalid["source_unit_id"] = invalid["domain"] = "performance_obligations"
    invalid["risk_type"] = "CORE_OBLIGATION_SCOPE_RISK"
    with pytest.raises(ContractError, match="Only FVA-005"):
        FindingCompatibilityRouter().route(
            FindingDraft.model_validate(invalid),
            source_root_id="root-invalid",
            owner_type="BASE_DOMAIN",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("check_code", "ZZ-999", "String should match pattern"),
        ("risk_type", "MADE_UP_RISK", "Unknown risk_type"),
        ("category", "PAYMENT", "incompatible"),
    ],
)
def test_router_has_no_unknown_fallback(field: str, value: str, message: str) -> None:
    raw = _finding(
        "1",
        "PO-001",
        "performance_obligations",
        "RIGHTS_OBLIGATIONS_IMBALANCE",
        "CORE_OBLIGATION_SCOPE_RISK",
        0,
        14,
    )
    raw[field] = value
    with pytest.raises((ContractError, ValueError), match=message):
        FindingCompatibilityRouter().route(
            FindingDraft.model_validate(raw),
            source_root_id="root-1",
            owner_type="BASE_DOMAIN",
        )


def test_adapter_is_stable_for_one_hundred_rebuilds() -> None:
    bundle = _bundle(
        [
            _finding("1", "CF-005", "commercial_financial", "PAYMENT", "ADVANCE_PAYMENT_SECURITY_RISK", 0, 14),
            _finding("2", "PO-001", "performance_obligations", "RIGHTS_OBLIGATIONS_IMBALANCE", "CORE_OBLIGATION_SCOPE_RISK", 15, 27),
        ]
    )
    adapter = LegacyRiskArtifactAdapter()
    hashes = {
        hashlib.sha256(
            projection.artifacts.model_dump_json().encode("utf-8")
        ).hexdigest()
        for projection in (adapter.adapt(bundle) for _ in range(100))
    }
    assert len(hashes) == 1


def test_all_forty_five_frozen_checks_have_one_closed_route() -> None:
    router = FindingCompatibilityRouter()

    assert len(router.registry.checks) == 45
    assert len(router._check_to_artifact) == 45
    assert set(router._check_to_artifact.values()) == {
        "rights_obligations_review_result",
        "commercial_terms_review_result",
        "liability_termination_review_result",
        "missing_ambiguous_clauses_result",
        "relation_extraction_result",
    }


def test_lre002_canonical_unbounded_liability_root_has_legacy_route() -> None:
    finding = FindingDraft.model_validate(
        _finding(
            "1",
            "LRE-002",
            "liability_remedies_exit",
            "LIABILITY",
            "UNBOUNDED_LIABILITY_EXPOSURE",
            0,
            14,
        )
    )

    route = FindingCompatibilityRouter().route(
        finding,
        source_root_id="root-unbounded-liability",
        owner_type="BASE_DOMAIN",
    )

    assert route.legacy_artifact_type == "liability_termination_review_result"


def test_duplicate_source_finding_id_is_a_hard_failure() -> None:
    finding = _finding(
        "1",
        "CF-005",
        "commercial_financial",
        "PAYMENT",
        "ADVANCE_PAYMENT_SECURITY_RISK",
        0,
        14,
    )
    with pytest.raises(ContractError, match="Source Finding ID is duplicated"):
        LegacyRiskArtifactAdapter().adapt(_bundle([finding, finding]))


def test_absence_evidence_survives_adapter_merge_and_verification() -> None:
    finding = _finding(
        "1",
        "CF-005",
        "commercial_financial",
        "PAYMENT",
        "ADVANCE_PAYMENT_SECURITY_RISK",
        0,
        14,
    )
    finding["evidence_candidates"].append(
        {
            "evidence_local_id": "evidence-" + "a" * 32,
            "finding_local_id": finding["finding_local_id"],
            "evidence_type": "ABSENCE",
            "source_ir_item_id": None,
            "anchor_id": None,
            "block_id": None,
            "page_number": None,
            "char_start": None,
            "char_end": None,
            "quoted_text": None,
            "quoted_text_hash": None,
            "checked_scope": "payment and performance-security clauses",
            "verification_note": "No performance security mechanism was found.",
        }
    )
    projection = LegacyRiskArtifactAdapter().adapt(_bundle([finding]))
    result = finalize_legacy_compatible_result(
        projection,
        context=_context(),
        consolidation=FindingConsolidationArtifact(
            result_type="FINDING_CONSOLIDATION_V1",
            status="COMPLETED",
            candidate_count=0,
            model_call_count=0,
            decisions=[],
            skip_reason=None,
        ),
        blocks=[BLOCK],
    )

    assert {item.evidence_type.value for item in result.final_evidence} == {
        "TEXT_QUOTE",
        "ABSENCE",
    }
    assert any(
        item.checked_scope == "payment and performance-security clauses"
        for item in result.final_evidence
    )


def test_skipped_semantic_merge_preserves_findings_and_produces_stable_result_hash() -> None:
    bundle = _bundle(
        [
            _finding("1", "CF-005", "commercial_financial", "PAYMENT", "ADVANCE_PAYMENT_SECURITY_RISK", 0, 14),
            _finding("2", "PO-001", "performance_obligations", "RIGHTS_OBLIGATIONS_IMBALANCE", "CORE_OBLIGATION_SCOPE_RISK", 15, 27),
        ]
    )
    projection = LegacyRiskArtifactAdapter().adapt(bundle)
    consolidation = FindingConsolidationArtifact(
        result_type="FINDING_CONSOLIDATION_V1",
        status="SKIPPED",
        candidate_count=1,
        model_call_count=2,
        decisions=[],
        skip_reason="MODEL_CLASSIFICATION_UNAVAILABLE",
    )

    first = finalize_legacy_compatible_result(
        projection,
        context=_context(),
        consolidation=consolidation,
        blocks=[BLOCK],
    )
    second = finalize_legacy_compatible_result(
        projection,
        context=_context(),
        consolidation=consolidation,
        blocks=[BLOCK],
    )

    assert first.merge_status == "SKIPPED"
    assert len(first.final_findings) == 2
    assert first.result_hash == second.result_hash
    assert first.final_findings == second.final_findings
    assert first.final_evidence == second.final_evidence


def test_existing_semantic_merger_unions_same_category_evidence() -> None:
    bundle = _bundle(
        [
            _finding("1", "CF-002", "commercial_financial", "PAYMENT", "PAYMENT_TIMING_RISK", 0, 14),
            _finding("2", "CF-005", "commercial_financial", "PAYMENT", "ADVANCE_PAYMENT_SECURITY_RISK", 15, 27),
        ]
    )
    projection = LegacyRiskArtifactAdapter().adapt(bundle)
    records = list(projection.routing_records)
    left = FindingRef(
        records[0].legacy_artifact_type,
        records[0].compatible_finding_id,
    )
    right = FindingRef(
        records[1].legacy_artifact_type,
        records[1].compatible_finding_id,
    )
    consolidation = FindingConsolidationArtifact.model_validate(
        {
            "result_type": "FINDING_CONSOLIDATION_V1",
            "status": "COMPLETED",
            "candidate_count": 1,
            "model_call_count": 1,
            "decisions": [
                {
                    "pair_id": make_pair_id(left, right),
                    "left": left.as_dict(),
                    "right": right.as_dict(),
                    "relation": "SAME_RISK",
                }
            ],
            "skip_reason": None,
        }
    )
    result = finalize_legacy_compatible_result(
        projection,
        context=_context(),
        consolidation=consolidation,
        blocks=[BLOCK],
    )
    assert len(result.final_findings) == 1
    assert len(result.final_evidence) == 2
    assert result.metrics.same_risk_count == 1


def test_invalid_evidence_block_is_a_hard_failure() -> None:
    bundle = _bundle(
        [
            _finding("1", "CF-005", "commercial_financial", "PAYMENT", "ADVANCE_PAYMENT_SECURITY_RISK", 0, 14),
        ]
    )
    projection = LegacyRiskArtifactAdapter().adapt(bundle)
    consolidation = FindingConsolidationArtifact(
        result_type="FINDING_CONSOLIDATION_V1",
        status="COMPLETED",
        candidate_count=0,
        model_call_count=0,
        decisions=[],
        skip_reason=None,
    )
    with pytest.raises(ContractError, match="outside this contract generation"):
        finalize_legacy_compatible_result(
            projection,
            context=_context(),
            consolidation=consolidation,
            blocks=[],
        )


def test_multi_artifact_duplicate_route_is_a_hard_failure() -> None:
    projection = LegacyRiskArtifactAdapter().adapt(
        _bundle(
            [
                _finding(
                    "1",
                    "PO-001",
                    "performance_obligations",
                    "RIGHTS_OBLIGATIONS_IMBALANCE",
                    "CORE_OBLIGATION_SCOPE_RISK",
                    0,
                    14,
                )
            ]
        )
    )
    artifacts = projection.artifacts.model_copy(deep=True)
    duplicate = artifacts.rights_obligations_review_result.findings[0]
    artifacts.relation_extraction_result.findings.append(duplicate)

    with pytest.raises(ContractError, match="exactly one compatible Artifact"):
        finalize_legacy_compatible_result(
            replace(projection, artifacts=artifacts),
            context=_context(),
            consolidation=_empty_consolidation(),
            blocks=[BLOCK],
        )


def test_evidence_id_collision_is_a_hard_failure() -> None:
    projection = LegacyRiskArtifactAdapter().adapt(
        _bundle(
            [
                _finding("1", "CF-005", "commercial_financial", "PAYMENT", "ADVANCE_PAYMENT_SECURITY_RISK", 0, 14),
                _finding("2", "CF-007", "commercial_financial", "DELIVERY", "DELIVERY_RISK", 15, 27),
            ]
        )
    )
    artifacts = projection.artifacts.model_copy(deep=True)
    evidence = artifacts.commercial_terms_review_result.evidences
    evidence[1].evidence_id = evidence[0].evidence_id

    with pytest.raises(ContractError, match="Evidence IDs must be globally unique"):
        finalize_legacy_compatible_result(
            replace(projection, artifacts=artifacts),
            context=_context(),
            consolidation=_empty_consolidation(),
            blocks=[BLOCK],
        )


def test_quoted_text_mismatch_is_a_hard_failure() -> None:
    projection = LegacyRiskArtifactAdapter().adapt(
        _bundle(
            [
                _finding("1", "CF-005", "commercial_financial", "PAYMENT", "ADVANCE_PAYMENT_SECURITY_RISK", 0, 14),
            ]
        )
    )
    artifacts = projection.artifacts.model_copy(deep=True)
    evidence = artifacts.commercial_terms_review_result.evidences[0]
    replacement = "X" * len(evidence.quoted_text or "")
    evidence.quoted_text = replacement
    evidence.quoted_text_hash = "sha256:" + hashlib.sha256(
        replacement.encode("utf-8")
    ).hexdigest()

    with pytest.raises(ContractError, match="text does not match its block"):
        finalize_legacy_compatible_result(
            replace(projection, artifacts=artifacts),
            context=_context(),
            consolidation=_empty_consolidation(),
            blocks=[BLOCK],
        )


def _bundle(findings: list[dict]) -> dict:
    roots = []
    horizontal_roots = []
    horizontal_decisions = []
    for index, finding in enumerate(findings):
        root = {
            "root_id": f"root-{index}",
            "finding_local_id": finding["finding_local_id"],
        }
        if finding["source_unit_id"] in {
            "cross_clause_consistency",
            "missing_ambiguity_completeness",
        }:
            candidate_id = f"horizontal-candidate-{index:032x}"
            root["candidate_ids"] = [candidate_id]
            horizontal_roots.append(root)
            horizontal_decisions.append(
                {
                    "candidate_id": candidate_id,
                    "owner_type": "HORIZONTAL",
                    "linked_base_finding_ids": [],
                }
            )
        else:
            roots.append(root)
    return {
        "bundle_id": "extended-bundle-test",
        "findings": findings,
        "base_bundle": {
            "units": [{"unit_id": "base", "canonical_risk_roots": roots}]
        },
        "horizontal_units": [
            {
                "unit_id": "cross_clause_consistency",
                "canonical_roots": horizontal_roots,
                "decisions": horizontal_decisions,
            }
        ],
    }


def _finding(
    suffix: str,
    check_code: str,
    unit_id: str,
    category: str,
    risk_type: str,
    start: int,
    end: int,
) -> dict:
    finding_id = f"finding-{int(suffix):032x}"
    evidence_id = f"evidence-{int(suffix):032x}"
    text = BLOCK_TEXT[start:end]
    return {
        "finding_local_id": finding_id,
        "source_unit_id": unit_id,
        "domain": unit_id,
        "check_code": check_code,
        "category": category,
        "risk_type": risk_type,
        "risk_level": "HIGH" if check_code == "CF-005" else "MEDIUM",
        "title": f"{check_code}风险",
        "issue": "合同安排存在实质风险。",
        "impact_to_our_party": "可能对我方造成不利影响。",
        "suggestion": "建议补充明确且可执行的限制。",
        "perspective": "PARTY_A",
        "our_party": "甲方公司",
        "counterparty": "乙方公司",
        "evidence_candidates": [
            {
                "evidence_local_id": evidence_id,
                "finding_local_id": finding_id,
                "evidence_type": "TEXT_QUOTE",
                "source_ir_item_id": f"ir-{suffix}",
                "anchor_id": f"anchor-{suffix}",
                "block_id": "block-1",
                "page_number": None,
                "char_start": start,
                "char_end": end,
                "quoted_text": text,
                "quoted_text_hash": "sha256:"
                + hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "checked_scope": None,
                "verification_note": None,
            }
        ],
    }


def _context() -> LegacyCompatibilityContext:
    return LegacyCompatibilityContext(
        review_id="review-test",
        business_task_id="business-test",
        contract_version_id="version-test",
        generation_id="generation-test",
        contract_hash="sha256:" + "1" * 64,
        contract_profile=ContractProfile(
            contract_type="AUTO",
            party_a={"name": "甲方公司"},
            party_b={"name": "乙方公司"},
            perspective="PARTY_A",
            our_party="甲方公司",
            counterparty="乙方公司",
            review_attitude="NEUTRAL",
        ),
    )


def _empty_consolidation() -> FindingConsolidationArtifact:
    return FindingConsolidationArtifact(
        result_type="FINDING_CONSOLIDATION_V1",
        status="COMPLETED",
        candidate_count=0,
        model_call_count=0,
        decisions=[],
        skip_reason=None,
    )
