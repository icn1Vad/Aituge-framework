from __future__ import annotations

import hashlib

import pytest

from contract.api.models import Finding
from contract.callback.models import (
    CommercialTermsStageResult,
    EvidenceCandidate,
    RightsObligationsStageResult,
)
from contract.errors import ContractError
from contract.review import merge_review_stage_results


def test_merges_semantic_duplicate_findings_and_source_evidence_deterministically() -> None:
    medium = _finding("finding-medium", "MEDIUM", "Payment risk", "Payment is late")
    high = _finding("finding-high", "HIGH", "  Payment   risk ", "Payment is late")
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
