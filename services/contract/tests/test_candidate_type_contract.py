from __future__ import annotations

from typing import get_args

from services.contract.capabilities.risk_review_bundle import (
    DeterministicRiskCandidate,
    GenericReviewRequest,
    _ICD_ALLOWED_CONTROL_CODES,
    _LRE_CANDIDATE_POLICIES,
    _PO_CANDIDATE_POLICIES,
    _lre_candidate,
)


def _allowed_candidate_types() -> set[str]:
    annotation = DeterministicRiskCandidate.model_fields[
        "candidate_type"
    ].annotation
    return set(get_args(annotation))


def test_all_registered_candidate_types_are_accepted_by_carrier() -> None:
    registered = {
        *_PO_CANDIDATE_POLICIES,
        *_ICD_ALLOWED_CONTROL_CODES,
        *_LRE_CANDIDATE_POLICIES,
    }

    assert registered <= _allowed_candidate_types()


def test_lre006_termination_settlement_absence_candidate_is_valid() -> None:
    generation_id = "generation-lre-absence-1"
    absence_source_id = "risk-as-" + "1" * 32
    request = GenericReviewRequest.model_validate(
        {
            "review_id": "review-lre-absence-1",
            "document_id": "document-lre-absence-1",
            "generation_id": generation_id,
            "attempt_no": 1,
            "plan_id": "risk-plan-" + "2" * 32,
            "context_hash": "sha256:" + "3" * 64,
            "unit_id": "liability_remedies_exit",
            "batch_id": "risk-batch-" + "4" * 32,
            "perspective": "PARTY_A",
            "our_party": "Party A",
            "counterparty": "Party B",
            "contract_type": "SERVICE",
            "review_attitude": "NEUTRAL",
            "assigned_check_specs": [
                {
                    "check_code": "LRE-006",
                    "review_question": (
                        "Review post-termination settlement and exit mechanisms."
                    ),
                    "allowed_categories": ["LIABILITY_AND_REMEDIES"],
                    "allowed_risk_types": ["TERMINATION_SETTLEMENT_REVIEW"],
                    "required_ir_types": ["obligations"],
                    "criticality": "REQUIRED",
                }
            ],
            "definitions": [],
            "projected_ir_items": [],
            "source_excerpts": [
                {
                    "anchor_id": "anchor-lre-context-1",
                    "block_id": "block-lre-context-1",
                    "block_no": 1,
                    "page_number": None,
                    "char_start": 0,
                    "char_end": 22,
                    "quoted_text": "continuing performance",
                    "quoted_text_hash": (
                        "sha256:"
                        "fd62f13f0a9f2009d09434cb3bc414c6e9a11b4b"
                        "ef86af498c136fa1b5cdbd69"
                    ),
                    "heading_path": ["Term"],
                }
            ],
            "evidence_sources": [],
            "absence_evidence_sources": [
                {
                    "source_id": absence_source_id,
                    "generation_id": generation_id,
                    "check_code": "LRE-006",
                    "checked_scope": (
                        "All termination, settlement, return, and exit clauses."
                    ),
                    "present_ir_types": ["obligations"],
                    "missing_target": "termination settlement and exit mechanism",
                    "verification_method": (
                        "Deterministic scan under the LRE-006 policy."
                    ),
                }
            ],
            "check_evidence_policies": [
                {
                    "check_code": "LRE-006",
                    "allowed_evidence_source_ids": [],
                    "allowed_absence_source_ids": [absence_source_id],
                }
            ],
            "present_ir_types": ["obligations"],
            "missing_ir_types": [],
            "estimated_input_tokens": 300,
        },
        context={"allow_absence_only_evidence_catalog": True},
    )

    candidate = _lre_candidate(
        request,
        check_code="LRE-006",
        candidate_type="TERMINATION_SETTLEMENT_ABSENT",
        trigger_reason=(
            "Continuing performance or incurred cost exists without a "
            "termination settlement and exit mechanism."
        ),
        required_ir_types=["obligations"],
        refs=[],
        ir_refs={},
        anchor_ref_by_id={},
        include_absence=True,
    )

    assert candidate.candidate_type == "TERMINATION_SETTLEMENT_ABSENT"
    assert candidate.canonical_root_type == "TERMINATION_SETTLEMENT_REVIEW"
    assert candidate.primary_evidence_source_ids == [absence_source_id]
