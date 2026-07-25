from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from services.contract.capabilities.grounded_answer import (
    GroundedAnswerDraft,
    GroundedAnswerTaskInput,
    materialize_grounded_answer,
)


def _task_input() -> GroundedAnswerTaskInput:
    return GroundedAnswerTaskInput(
        schema_version="1.0",
        mode="REPORT",
        review_id="review-1",
        document_id="document-1",
    )


def _review_result() -> dict:
    quote = "甲方应在验收后付款"
    return {
        "review_id": "review-1",
        "contract_version_id": "version-1",
        "result_hash": "sha256:" + "a" * 64,
        "findings": [{"finding_id": "finding-1"}],
        "evidences": [
            {
                "evidence_id": "evidence-1",
                "finding_id": "finding-1",
                "evidence_type": "TEXT_QUOTE",
                "block_id": "block-1",
                "page_number": None,
                "char_start": 0,
                "char_end": len(quote),
                "quoted_text": quote,
                "quoted_text_hash": (
                    "sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest()
                ),
            }
        ],
    }


def test_reuses_one_reference_for_repeated_evidence() -> None:
    draft = GroundedAnswerDraft(
        mode="REPORT",
        content_markdown=(
            "查看[付款条款](#docref-evidence-1)，"
            "并复核[原文位置](#docref-evidence-1)。"
        ),
        citations=[
            {"evidence_id": "evidence-1", "label": "付款条款"},
            {"evidence_id": "evidence-1", "label": "原文位置"},
        ],
    )

    result = materialize_grounded_answer(
        task_input=_task_input(),
        draft=draft,
        review_result=_review_result(),
    )

    assert len(result.references) == 1
    assert result.references[0].evidence_id == "evidence-1"


def test_markdown_label_is_authoritative_when_redundant_citation_label_differs() -> None:
    draft = GroundedAnswerDraft(
        mode="REPORT",
        content_markdown="查看[付款原文](#docref-evidence-1)。",
        citations=[{"evidence_id": "evidence-1", "label": "付款条款"}],
    )

    result = materialize_grounded_answer(
        task_input=_task_input(),
        draft=draft,
        review_result=_review_result(),
    )

    assert result.references[0].label == "付款原文"


def test_unknown_evidence_is_downgraded_to_plain_text() -> None:
    draft = GroundedAnswerDraft(
        mode="REPORT",
        content_markdown="合同缺少[数据删除条款](#docref-evidence-unknown)。",
        citations=[{"evidence_id": "evidence-unknown", "label": "数据删除条款"}],
    )

    result = materialize_grounded_answer(
        task_input=_task_input(),
        draft=draft,
        review_result=_review_result(),
    )

    assert result.content_markdown == "合同缺少数据删除条款。"
    assert result.references == []


def test_chat_requires_question_and_accepts_bounded_history() -> None:
    task_input = GroundedAnswerTaskInput(
        schema_version="1.0",
        mode="CHAT",
        review_id="review-1",
        document_id="document-1",
        question="付款条款对甲方有什么风险？",
        conversation_history=[
            {"role": "USER", "content": "先说明合同金额。"},
            {"role": "ASSISTANT", "content": "合同金额以审查结果为准。"},
        ],
    )

    assert task_input.question == "付款条款对甲方有什么风险？"
    assert len(task_input.conversation_history) == 2

    with pytest.raises(ValidationError, match="requires a non-empty question"):
        GroundedAnswerTaskInput(
            schema_version="1.0",
            mode="CHAT",
            review_id="review-1",
            document_id="document-1",
            question=" ",
        )


def test_chat_materializes_the_same_authoritative_reference_shape() -> None:
    task_input = GroundedAnswerTaskInput(
        schema_version="1.0",
        mode="CHAT",
        review_id="review-1",
        document_id="document-1",
        question="付款证据在哪里？",
    )
    draft = GroundedAnswerDraft(
        mode="CHAT",
        content_markdown="请查看[付款条款](#docref-evidence-1)。",
        citations=[{"evidence_id": "evidence-1", "label": "付款条款"}],
    )

    result = materialize_grounded_answer(
        task_input=task_input,
        draft=draft,
        review_result=_review_result(),
    )

    assert result.mode == "CHAT"
    assert result.references[0].chunk_id == "block-1"
    assert result.references[0].char_start == 0


def test_report_rejects_chat_only_fields() -> None:
    with pytest.raises(ValidationError, match="does not accept"):
        GroundedAnswerTaskInput(
            schema_version="1.0",
            mode="REPORT",
            review_id="review-1",
            document_id="document-1",
            question="不应出现在报告模式",
        )
