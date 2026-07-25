from __future__ import annotations

import asyncio
import hashlib

import pytest

from services.contract.capabilities.grounded_answer import (
    GroundedAnswerDraft,
    GroundedAnswerMaterializationError,
    GroundedAnswerTaskInput,
    materialize_grounded_answer,
)
from services.contract.capabilities import register as contract_capability


class _Settings:
    def get(self, name: str, default: str = "") -> str:
        return {
            "CONTRACT_SERVICE_BASE_URL": "http://contract:18120",
            "CONTRACT_RESULT_SINK_INTERNAL_TOKEN": "test-token",
            "CONTRACT_MODEL_ID": "deepseek-v4-pro",
            "CONTRACT_IR_ENGINE": "window",
        }.get(name, default)

    def require(self, name: str) -> str:
        value = self.get(name).strip()
        if not value:
            raise ValueError(name)
        return value


class _Registry:
    def __init__(self) -> None:
        self.calls: dict[str, list] = {}

    def register_skill_root(self, path) -> None:
        self.calls.setdefault("skill_root", []).append(path)

    def __getattr__(self, name: str):
        if not name.startswith("register_"):
            raise AttributeError(name)

        def capture(**kwargs) -> None:
            self.calls.setdefault(name.removeprefix("register_"), []).append(kwargs)

        return capture


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


def test_rejects_absence_evidence_as_clickable_reference() -> None:
    draft = GroundedAnswerDraft(
        mode="REPORT",
        content_markdown="未约定[争议解决](#docref-ev-019)。",
        citations=[{"evidence_id": "ev-019", "label": "争议解决"}],
    )

    with pytest.raises(GroundedAnswerMaterializationError, match="not locatable"):
        materialize_grounded_answer(
            task_input=_task_input(),
            draft=draft,
            review_result=_review_result(evidence_type="ABSENCE"),
        )


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


def test_contract_capability_registers_grounded_report_pipeline() -> None:
    registry = _Registry()

    asyncio.run(contract_capability.register(registry, _Settings()))

    tasks = {item["task_type"]: item for item in registry.calls["task"]}
    pipelines = {item["pipeline_id"]: item for item in registry.calls["pipeline"]}
    tools = {item["tool_name"]: item for item in registry.calls["http_tool"]}
    agents = {item["agent_id"]: item for item in registry.calls["agent"]}
    report_task = tasks["contract.grounded.answer"]
    report_pipeline = pipelines["contract-grounded-answer-pipeline-v1"]
    stages = {item["stage_id"]: item for item in report_pipeline["stages"]}

    assert report_task["handler"] == "pipeline"
    assert report_task["input_model"] is GroundedAnswerTaskInput
    assert report_task["output_model"].__name__ == "GroundedAnswerResult"
    assert tools["contract_get_review_result"]["path"] == (
        "/v1/internal/contract-tools/review-result"
    )
    assert "contract_get_review_result" in agents["contract-grounded-answer-v1"][
        "default_tools"
    ]
    assert stages["generate_grounded_answer"]["output_model"] is GroundedAnswerDraft
    assert stages["finalize_grounded_answer"]["service_handler"] == (
        "contract_grounded_answer_finalize_v1"
    )
