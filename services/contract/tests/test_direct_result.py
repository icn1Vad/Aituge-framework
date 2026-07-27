from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from contract.api.models import ContractProfile, ReviewResultData
from contract.application.result_hash import compute_result_hash
from services.contract.capabilities.direct_result import (
    DirectResultError,
    build_direct_final_stage,
)
from services.contract.capabilities.risk_review import (
    EvidenceCandidate as RiskEvidenceCandidate,
    FindingDraft,
)


BLOCK_TEXT = "Party A must prepay the full price."


def _profile() -> ContractProfile:
    return ContractProfile(
        contract_type="AUTO",
        party_a={"name": "Party A"},
        party_b={"name": "Party B"},
        perspective="PARTY_A",
        our_party="Party A",
        counterparty="Party B",
        review_attitude="NEUTRAL",
    )


def _bundle(*, duplicate_evidence: bool = False, status: str = "COMPLETED"):
    finding_id = "finding-" + "1" * 32
    evidence_id = "evidence-" + "1" * 32
    candidate = RiskEvidenceCandidate(
        evidence_local_id=evidence_id,
        finding_local_id=finding_id,
        evidence_type="TEXT_QUOTE",
        source_ir_item_id="ir-item-1",
        anchor_id="anchor-1",
        block_id="block-1",
        page_number=1,
        char_start=0,
        char_end=len(BLOCK_TEXT),
        quoted_text=BLOCK_TEXT,
        quoted_text_hash="sha256:"
        + __import__("hashlib").sha256(BLOCK_TEXT.encode("utf-8")).hexdigest(),
    )
    finding = FindingDraft(
        finding_local_id=finding_id,
        source_unit_id="commercial_financial",
        domain="commercial_financial",
        check_code="CF-005",
        category="PAYMENT",
        risk_type="ADVANCE_PAYMENT_SECURITY_RISK",
        risk_level="HIGH",
        title="Unsecured advance payment",
        issue="The contract requires payment before performance.",
        impact_to_our_party="Our party bears prepayment exposure.",
        suggestion="Use milestone payments or a performance guarantee.",
        perspective="PARTY_A",
        our_party="Party A",
        counterparty="Party B",
        evidence_candidates=[candidate],
    )
    findings = [finding]
    if duplicate_evidence:
        findings.append(
            finding.model_copy(
                update={
                    "finding_local_id": "finding-" + "2" * 32,
                    "evidence_candidates": [
                        candidate.model_copy(
                            update={
                                "finding_local_id": "finding-" + "2" * 32,
                            }
                        )
                    ],
                }
            )
        )
    return SimpleNamespace(
        status=status,
        review_id="review-1",
        findings=findings,
    )


def _blocks():
    return [
        {
            "block_id": "block-1",
            "block_no": 1,
            "page_number": 1,
            "text": BLOCK_TEXT,
        }
    ]


def test_direct_bundle_materializes_public_result_without_internal_fields() -> None:
    result = build_direct_final_stage(
        bundle=_bundle(),
        contract_profile=_profile(),
        business_task_id="business-1",
        contract_version_id="version-1",
        blocks=_blocks(),
    )

    assert result.result_type == "FINAL_REVIEW_STAGE_V1"
    assert result.findings[0].finding_id == "finding-" + "1" * 32
    assert result.findings[0].evidence_ids == ["evidence-" + "1" * 32]
    assert result.evidences[0].quoted_text == BLOCK_TEXT
    assert result.summary.high_count == 1
    payload = result.model_dump_json()
    assert "candidate_id" not in payload
    assert "canonical_root" not in payload
    assert "legacy" not in payload.lower()

    raw = result.model_dump(mode="json")
    raw.pop("result_type")
    result_hash, _ = compute_result_hash(raw)
    validated = ReviewResultData.model_validate({**raw, "result_hash": result_hash})
    assert validated.result_hash == result_hash


def test_direct_bundle_rejects_duplicate_evidence_ids() -> None:
    with pytest.raises(DirectResultError, match="duplicate Evidence IDs"):
        build_direct_final_stage(
            bundle=_bundle(duplicate_evidence=True),
            contract_profile=_profile(),
            business_task_id="business-1",
            contract_version_id="version-1",
            blocks=_blocks(),
        )


def test_direct_bundle_rejects_incomplete_bundle() -> None:
    with pytest.raises(DirectResultError, match="incomplete Extended Bundle"):
        build_direct_final_stage(
            bundle=_bundle(status="PARTIAL_FAILED"),
            contract_profile=_profile(),
            business_task_id="business-1",
            contract_version_id="version-1",
            blocks=_blocks(),
        )


def test_formal_registration_does_not_use_removed_postprocessing() -> None:
    from services.contract.capabilities import register

    source = Path(register.__file__).read_text(encoding="utf-8")
    assert "legacy_compatibility" not in source
    assert "FindingConsolidationEngine" not in source
    assert "contract_risk_stage66_direct_e2e" not in source
