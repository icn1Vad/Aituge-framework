from __future__ import annotations

import hashlib

import pytest

from services.contract.capabilities.grounded_answer import (
    GroundedAnswerDraft,
    GroundedAnswerMaterializationError,
    GroundedAnswerTaskInput,
    materialize_grounded_answer,
)


def _review_result(*, evidence_type: str = "TEXT_QUOTE") -> dict:
    quote = "付款条款"
    evidence = {
        "evidence_id": "ev-019",
        "finding_id": "finding-1",
        "evidence_type": evidence_type,
        "block_id": "block-7",
        "page_number": None,
        "char_start": 10,
        "char_end": 14,
        "quoted_text": quote,
        "quoted_text_hash": "sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        "checked_scope": None,
        "verification_note": None,
        "bounding_boxes": [],
    }
    if evidence_type == "ABSENCE":
        evidence.update(
            {
                "block_id": None,
                "char_start": None,
                "char_end": None,
                "quoted_text": None,
                "quoted_text_hash": None,
                "checked_scope": "全文争议解决条款",
                "verification_note": "未发现约定",
            }
        )
    return {
        "schema_version": "1.0",
        "review_id": "review-1",
        "business_task_id": "task-1",
        "contract_version_id": "version-1",
        "contract_profile": {
            "contract_type": "AUTO",
            "party_a": {"name": "甲方"},
            "party_b": {"name": "乙方"},
            "perspective": "PARTY_A",
            "our_party": "甲方",
            "counterparty": "乙方",
            "review_attitude": "NEUTRAL",
        },
        "summary": {
            "overview": "测试",
            "high_count": 1,
            "medium_count": 0,
            "low_count": 0,
            "info_count": 0,
        },
        "findings": [
            {
                "finding_id": "finding-1",
                "category": "PAYMENT",
                "risk_level": "HIGH",
                "title": "付款风险",
                "perspective": "PARTY_A",
                "our_party": "甲方",
                "counterparty": "乙方",
                "issue": "付款条件不清",
                "impact_to_our_party": "可能提前付款",
                "suggestion": "绑定验收",
                "evidence_ids": ["ev-019"],
            }
        ],
        "evidences": [evidence],
        "relationships": [],
        "result_hash": "sha256:" + "a" * 64,
    }


def _task_input() -> GroundedAnswerTaskInput:
    return GroundedAnswerTaskInput(
        schema_version="1.0",
        mode="REPORT",
        review_id="review-1",
        document_id="document-1",
    )


def test_materializes_authoritative_reference_for_markdown_marker() -> None:
    draft = GroundedAnswerDraft(
        mode="REPORT",
        content_markdown="重点检查[付款安排](#docref-ev-019)。",
        citations=[{"evidence_id": "ev-019", "label": "付款安排"}],
    )

    result = materialize_grounded_answer(
        task_input=_task_input(),
        draft=draft,
        review_result=_review_result(),
    )

    assert result.content_markdown == draft.content_markdown
    assert result.references[0].reference_id == "docref-ev-019"
    assert result.references[0].chunk_id == "block-7"
    assert result.references[0].block_id == "block-7"
    assert result.references[0].char_start == 10
    assert result.references[0].quoted_text == "付款条款"


def test_rejects_dangling_or_unused_citations() -> None:
    draft = GroundedAnswerDraft(
        mode="REPORT",
        content_markdown="重点检查付款安排。",
        citations=[{"evidence_id": "ev-019", "label": "付款安排"}],
    )

    with pytest.raises(GroundedAnswerMaterializationError, match="differ"):
        materialize_grounded_answer(
            task_input=_task_input(),
            draft=draft,
            review_result=_review_result(),
        )


def test_downgrades_absence_evidence_to_plain_text() -> None:
    draft = GroundedAnswerDraft(
        mode="REPORT",
        content_markdown="未约定[争议解决](#docref-ev-019)。",
        citations=[{"evidence_id": "ev-019", "label": "争议解决"}],
    )

    result = materialize_grounded_answer(
        task_input=_task_input(),
        draft=draft,
        review_result=_review_result(evidence_type="ABSENCE"),
    )

    assert result.content_markdown == "未约定争议解决。"
    assert result.references == []


def test_rejects_marker_label_that_differs_from_citation() -> None:
    draft = GroundedAnswerDraft(
        mode="REPORT",
        content_markdown="重点检查[付款条款](#docref-ev-019)。",
        citations=[{"evidence_id": "ev-019", "label": "付款安排"}],
    )

    with pytest.raises(GroundedAnswerMaterializationError, match="label"):
        materialize_grounded_answer(
            task_input=_task_input(),
            draft=draft,
            review_result=_review_result(),
        )
