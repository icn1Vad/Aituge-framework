from __future__ import annotations

import hashlib
import json

import pytest

from contract.api.models import Finding, FindingCategory
from contract.callback.models import (
    CommercialTermsStageResult,
    EvidenceCandidate,
    FindingConsolidationArtifact,
    RightsObligationsStageResult,
)
from contract.errors import ContractError
from contract.review import merge_review_stage_results, namespace_review_stage_result


def test_merges_semantic_duplicate_findings_and_source_evidence_deterministically() -> None:
    medium = _finding("finding-medium", "MEDIUM", "Payment risk", "Payment is late").model_copy(
        update={"legal_evidence_ids": ["legal-evidence-medium"]}
    )
    high = _finding("finding-high", "HIGH", "  Payment   risk ", "Payment is late").model_copy(
        update={"legal_evidence_ids": ["legal-evidence-high"]}
    )
    first = RightsObligationsStageResult(
        result_type="RIGHTS_OBLIGATIONS_STAGE_V1",
        findings=[medium],
        evidences=[_quote("evidence-a", medium.finding_id, "Payment")],
    )
    second = CommercialTermsStageResult(
        result_type="COMMERCIAL_TERMS_STAGE_V1",
        findings=[high],
        evidences=[_quote("evidence-b", high.finding_id, "Payment", include_quote=True)],
    )

    findings, evidences = merge_review_stage_results([first, second])
    reversed_findings, reversed_evidences = merge_review_stage_results([second, first])

    assert findings == reversed_findings
    assert evidences == reversed_evidences
    assert len(findings) == 1
    assert findings[0].finding_id == "finding-high"
    assert findings[0].risk_level.value == "HIGH"
    assert findings[0].evidence_ids == ["evidence-a"]
    assert findings[0].legal_evidence_ids == [
        "legal-evidence-high",
        "legal-evidence-medium",
    ]
    assert len(evidences) == 1
    assert evidences[0].finding_id == "finding-high"


def test_rejects_finding_id_reused_for_different_content() -> None:
    first = _finding("finding-same", "LOW", "First", "First issue")
    second = _finding("finding-same", "LOW", "Second", "Second issue")
    stages = [
        RightsObligationsStageResult(
            result_type="RIGHTS_OBLIGATIONS_STAGE_V1",
            findings=[first],
            evidences=[_quote("evidence-1", first.finding_id, "Payment")],
        ),
        CommercialTermsStageResult(
            result_type="COMMERCIAL_TERMS_STAGE_V1",
            findings=[second],
            evidences=[_quote("evidence-2", second.finding_id, "Payment")],
        ),
    ]

    with pytest.raises(ContractError, match="Finding ID is duplicated"):
        merge_review_stage_results(stages)


def test_rejects_evidence_id_reused_for_different_content() -> None:
    finding = _finding("finding-1", "LOW", "Payment risk", "Payment is late")
    stages = [
        RightsObligationsStageResult(
            result_type="RIGHTS_OBLIGATIONS_STAGE_V1",
            findings=[finding],
            evidences=[_quote("evidence-same", finding.finding_id, "Payment")],
        ),
        CommercialTermsStageResult(
            result_type="COMMERCIAL_TERMS_STAGE_V1",
            findings=[finding],
            evidences=[_quote("evidence-same", finding.finding_id, "Paymen")],
        ),
    ]

    with pytest.raises(ContractError, match="Evidence ID is duplicated"):
        merge_review_stage_results(stages)


def test_namespaces_model_local_ids_before_parallel_stage_merge() -> None:
    first_finding = _finding("finding-001", "HIGH", "Payment risk", "Payment is late")
    second_finding = _finding("finding-001", "LOW", "Delivery risk", "Delivery is unclear")
    first = namespace_review_stage_result(
        "commercial_terms_review_result",
        CommercialTermsStageResult(
            result_type="COMMERCIAL_TERMS_STAGE_V1",
            findings=[first_finding],
            evidences=[_quote("evidence-001", first_finding.finding_id, "Payment")],
        ),
    )
    second = namespace_review_stage_result(
        "rights_obligations_review_result",
        RightsObligationsStageResult(
            result_type="RIGHTS_OBLIGATIONS_STAGE_V1",
            findings=[second_finding],
            evidences=[_quote("evidence-001", second_finding.finding_id, "Delivery")],
        ),
    )

    findings, evidences = merge_review_stage_results([first, second])

    assert len(findings) == 2
    assert len({item.finding_id for item in findings}) == 2
    assert len({item.evidence_id for item in evidences}) == 2
    assert all(item.finding_id.startswith("finding-") for item in findings)
    assert all(item.evidence_id.startswith("evidence-") for item in evidences)


def test_model_decision_merges_paraphrased_same_risk_and_unions_evidence() -> None:
    first_source = _finding(
        "finding-acceptance-a",
        "MEDIUM",
        "Acceptance procedure is missing",
        "The contract does not define a formal acceptance procedure.",
    )
    second_source = _finding(
        "finding-acceptance-b",
        "HIGH",
        "No enforceable acceptance mechanism",
        "There is no standard, deadline, or consequence for failed acceptance.",
    )
    first_raw = RightsObligationsStageResult(
        result_type="RIGHTS_OBLIGATIONS_STAGE_V1",
        findings=[first_source],
        evidences=[_quote("evidence-a", first_source.finding_id, "Payment")],
    )
    second_raw = CommercialTermsStageResult(
        result_type="COMMERCIAL_TERMS_STAGE_V1",
        findings=[second_source],
        evidences=[
            EvidenceCandidate(
                evidence_id="evidence-b",
                finding_id=second_source.finding_id,
                evidence_type="TEXT_QUOTE",
                block_id="block-2",
                page_number=1,
                char_start=2,
                char_end=10,
            )
        ],
    )
    first = namespace_review_stage_result("rights_obligations_review_result", first_raw)
    second = namespace_review_stage_result("commercial_terms_review_result", second_raw)
    left = ("rights_obligations_review_result", first_source.finding_id)
    right = ("commercial_terms_review_result", second_source.finding_id)
    pair_id = _pair_id(left, right)
    consolidation = FindingConsolidationArtifact.model_validate(
        {
            "result_type": "FINDING_CONSOLIDATION_V1",
            "status": "COMPLETED",
            "candidate_count": 1,
            "model_call_count": 1,
            "decisions": [
                {
                    "pair_id": pair_id,
                    "left": {"artifact_type": left[0], "finding_id": left[1]},
                    "right": {"artifact_type": right[0], "finding_id": right[1]},
                    "relation": "SAME_RISK",
                }
            ],
            "skip_reason": None,
        }
    )

    findings, evidences = merge_review_stage_results(
        [first, second],
        consolidation=consolidation,
        finding_reference_ids={
            left: first.findings[0].finding_id,
            right: second.findings[0].finding_id,
        },
    )

    assert len(findings) == 1
    assert findings[0].risk_level.value == "HIGH"
    assert len(findings[0].evidence_ids) == 2
    assert len(evidences) == 2
    assert {item.finding_id for item in evidences} == {findings[0].finding_id}


def test_model_decision_never_merges_cross_category_findings() -> None:
    first_source = _finding("finding-payment", "HIGH", "Payment", "Payment is risky")
    second_source = _finding("finding-termination", "HIGH", "Termination", "Termination is risky")
    second_source = second_source.model_copy(update={"category": FindingCategory.TERMINATION})
    first_raw = RightsObligationsStageResult(
        result_type="RIGHTS_OBLIGATIONS_STAGE_V1",
        findings=[first_source],
        evidences=[_quote("evidence-a", first_source.finding_id, "Payment")],
    )
    second_raw = CommercialTermsStageResult(
        result_type="COMMERCIAL_TERMS_STAGE_V1",
        findings=[second_source],
        evidences=[_quote("evidence-b", second_source.finding_id, "Payment")],
    )
    first = namespace_review_stage_result("rights_obligations_review_result", first_raw)
    second = namespace_review_stage_result("commercial_terms_review_result", second_raw)
    left = ("rights_obligations_review_result", first_source.finding_id)
    right = ("commercial_terms_review_result", second_source.finding_id)
    consolidation = FindingConsolidationArtifact.model_validate(
        {
            "result_type": "FINDING_CONSOLIDATION_V1",
            "status": "COMPLETED",
            "candidate_count": 1,
            "model_call_count": 1,
            "decisions": [
                {
                    "pair_id": _pair_id(left, right),
                    "left": {"artifact_type": left[0], "finding_id": left[1]},
                    "right": {"artifact_type": right[0], "finding_id": right[1]},
                    "relation": "SAME_RISK",
                }
            ],
            "skip_reason": None,
        }
    )

    findings, _ = merge_review_stage_results(
        [first, second],
        consolidation=consolidation,
        finding_reference_ids={
            left: first.findings[0].finding_id,
            right: second.findings[0].finding_id,
        },
    )

    assert len(findings) == 2


def test_rejects_forged_consolidation_pair_id() -> None:
    source = _finding("finding-a", "HIGH", "Payment A", "Payment risk A")
    other = _finding("finding-b", "HIGH", "Payment B", "Payment risk B")
    first_raw = RightsObligationsStageResult(
        result_type="RIGHTS_OBLIGATIONS_STAGE_V1",
        findings=[source],
        evidences=[_quote("evidence-a", source.finding_id, "Payment")],
    )
    second_raw = CommercialTermsStageResult(
        result_type="COMMERCIAL_TERMS_STAGE_V1",
        findings=[other],
        evidences=[_quote("evidence-b", other.finding_id, "Payment")],
    )
    first = namespace_review_stage_result("rights_obligations_review_result", first_raw)
    second = namespace_review_stage_result("commercial_terms_review_result", second_raw)
    left = ("rights_obligations_review_result", source.finding_id)
    right = ("commercial_terms_review_result", other.finding_id)
    consolidation = FindingConsolidationArtifact.model_validate(
        {
            "result_type": "FINDING_CONSOLIDATION_V1",
            "status": "COMPLETED",
            "candidate_count": 1,
            "model_call_count": 1,
            "decisions": [
                {
                    "pair_id": "pair-" + "0" * 32,
                    "left": {"artifact_type": left[0], "finding_id": left[1]},
                    "right": {"artifact_type": right[0], "finding_id": right[1]},
                    "relation": "SAME_RISK",
                }
            ],
            "skip_reason": None,
        }
    )

    with pytest.raises(ContractError, match="pair ID does not match"):
        merge_review_stage_results(
            [first, second],
            consolidation=consolidation,
            finding_reference_ids={
                left: first.findings[0].finding_id,
                right: second.findings[0].finding_id,
            },
        )


def _finding(finding_id: str, risk_level: str, title: str, issue: str) -> Finding:
    return Finding(
        finding_id=finding_id,
        category="PAYMENT",
        risk_level=risk_level,
        title=title,
        perspective="PARTY_B",
        our_party="Beta Company",
        counterparty="Acme Company",
        issue=issue,
        impact_to_our_party="Cash flow may be affected.",
        suggestion="Add a clear payment deadline.",
        evidence_ids=[f"evidence-for-{finding_id}"],
    )


def _quote(
    evidence_id: str,
    finding_id: str,
    text: str,
    *,
    include_quote: bool = False,
) -> EvidenceCandidate:
    values = {
        "quoted_text": text,
        "quoted_text_hash": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    } if include_quote else {}
    return EvidenceCandidate(
        evidence_id=evidence_id,
        finding_id=finding_id,
        evidence_type="TEXT_QUOTE",
        block_id="block-1",
        page_number=1,
        char_start=0,
        char_end=len(text),
        **values,
    )


def _pair_id(left: tuple[str, str], right: tuple[str, str]) -> str:
    canonical = json.dumps(sorted((left, right)), ensure_ascii=False, separators=(",", ":"))
    return "pair-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
