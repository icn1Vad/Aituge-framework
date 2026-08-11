from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from contract.api.models import (
    CreateReviewData,
    CreateReviewRequest,
    Evidence,
    PartyResolutionStatusData,
    ReviewResultData,
)


def test_created_mapping_must_be_empty() -> None:
    with pytest.raises(ValidationError):
        CreateReviewData(
            review_id="review-1",
            document_id="document-1",
            status="CREATED",
            current_stage="PARSING",
            reused=False,
        )


def test_running_mapping_must_be_complete() -> None:
    with pytest.raises(ValidationError):
        CreateReviewData(
            review_id="review-1",
            document_id="document-1",
            status="RUNNING",
            current_stage="PARSING",
            framework_attempt_no=1,
            reused=False,
        )


def test_confirmed_parties_must_be_a_complete_distinct_pair_for_the_selected_perspective() -> None:
    with pytest.raises(ValidationError):
        CreateReviewRequest(
            business_task_id="10001",
            contract_version_id="20001",
            perspective="PARTY_A",
            confirmed_party_a_name="甲方单位",
            contract_type="AUTO",
            review_attitude="NEUTRAL",
            schema_version="1.0",
        )

    with pytest.raises(ValidationError):
        CreateReviewRequest(
            business_task_id="10001",
            contract_version_id="20001",
            perspective="PARTY_B",
            our_party_name="甲方单位",
            confirmed_party_a_name="甲方单位",
            confirmed_party_b_name="乙方单位",
            contract_type="AUTO",
            review_attitude="NEUTRAL",
            schema_version="1.0",
        )


def test_succeeded_party_resolution_can_leave_names_for_user_input() -> None:
    value = PartyResolutionStatusData(
        resolution_id="resolution-1",
        contract_version_id="20001",
        document_id="document-1",
        status="SUCCEEDED",
        party_a_name="Party A",
        updated_at="2026-07-30T00:00:00Z",
    )
    assert value.party_a_name == "Party A"
    assert value.party_b_name is None


def test_text_evidence_hash_is_validated() -> None:
    quoted_text = "乙方承担责任。"
    evidence = Evidence(
        evidence_id="evidence-1",
        finding_id="finding-1",
        evidence_type="TEXT_QUOTE",
        block_id="block-1",
        page_number=1,
        char_start=0,
        char_end=len(quoted_text),
        quoted_text=quoted_text,
        quoted_text_hash="sha256:" + hashlib.sha256(quoted_text.encode("utf-8")).hexdigest(),
        bounding_boxes=[],
    )
    assert evidence.char_end == len(quoted_text)


def test_text_evidence_range_length_is_validated() -> None:
    quoted_text = "乙方承担责任。"
    with pytest.raises(ValidationError):
        Evidence(
            evidence_id="evidence-1",
            finding_id="finding-1",
            evidence_type="TEXT_QUOTE",
            block_id="block-1",
            char_start=0,
            char_end=len(quoted_text) + 1,
            quoted_text=quoted_text,
            quoted_text_hash="sha256:" + hashlib.sha256(quoted_text.encode("utf-8")).hexdigest(),
        )


def test_absence_evidence_rejects_fake_quote() -> None:
    with pytest.raises(ValidationError):
        Evidence(
            evidence_id="evidence-1",
            finding_id="finding-1",
            evidence_type="ABSENCE",
            block_id="block-1",
            checked_scope="ENTIRE_CONTRACT",
            verification_note="未发现责任上限",
        )


def test_result_summary_must_match_findings() -> None:
    with pytest.raises(ValidationError):
        ReviewResultData(
            schema_version="1.0",
            review_id="review-1",
            business_task_id="10001",
            contract_version_id="20001",
            contract_profile={
                "contract_type": "SERVICE",
                "party_a": {"name": "甲方"},
                "party_b": {"name": "乙方"},
                "perspective": "PARTY_B",
                "our_party": "乙方",
                "counterparty": "甲方",
                "review_attitude": "NEUTRAL",
            },
            summary={
                "overview": "无风险",
                "high_count": 1,
                "medium_count": 0,
                "low_count": 0,
                "info_count": 0,
            },
            findings=[],
            evidences=[],
            relationships=[],
            result_hash="sha256:" + "0" * 64,
        )


def test_protocol_v1_reserved_arrays_must_be_empty() -> None:
    with pytest.raises(ValidationError):
        Evidence(
            evidence_id="evidence-1",
            finding_id="finding-1",
            evidence_type="ABSENCE",
            checked_scope="ENTIRE_CONTRACT",
            verification_note="未发现约定",
            bounding_boxes=[None],
        )
