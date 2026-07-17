from __future__ import annotations

import pytest

from proof.application.semantic_audit import SemanticAuditService
from proof.config import Settings
from proof.errors import ProofError


class FakeAuditRepository:
    def __init__(self) -> None:
        self.units = [
            {
                "id": "unit-1",
                "text": "第一条 相关部门应及时处理。",
                "clause_no_raw": "第一条",
                "clause_ordinal": 1,
                "heading_path": ["第一章"],
            },
            {
                "id": "unit-2",
                "text": "第二条 财务部应在三个工作日内完成复核。",
                "clause_no_raw": "第二条",
                "clause_ordinal": 2,
                "heading_path": ["第一章"],
            },
        ]
        self.run = {
            "id": "audit-1",
            "document_id": "document-1",
            "status": "running",
            "framework_task_id": "task-1",
            "framework_run_id": "run-1",
            "error_message": None,
        }
        self.saved_findings = None

    def get_audit_run(self, audit_id):
        return dict(self.run) if self.run and audit_id == self.run["id"] else None

    def get_audit_run_for_document(self, document_id):
        return dict(self.run) if self.run and document_id == self.run["document_id"] else None

    def create_audit_run(self, *, audit_run_id, document_id):
        self.run = {
            "id": audit_run_id,
            "document_id": document_id,
            "status": "pending",
            "framework_task_id": None,
            "framework_run_id": None,
            "error_message": None,
        }
        return dict(self.run)

    def reset_failed_audit_run(self, audit_id):
        if not self.run or self.run["status"] != "failed":
            return False
        self.run.update(status="pending", error_message=None)
        return True

    def get_document_units(self, document_id):
        return list(self.units) if document_id == self.run["document_id"] else []

    def complete_audit(self, audit_id, findings):
        self.saved_findings = findings
        self.run["status"] = "completed"

    def mark_audit_failed(self, audit_id, message):
        self.run["status"] = "failed"
        self.run["error_message"] = message

    def list_audit_findings(self, audit_id):
        return self.saved_findings or []


def callback_payload(*, quote: str = "相关部门应及时处理") -> dict:
    return {
        "audit_id": "audit-1",
        "task_id": "task-1",
        "run_id": "run-1",
        "output": {
            "summary": {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0},
            "items": [
                {
                    "status": "succeeded",
                    "input": {
                        "targets": [
                            {"id": "unit-1"},
                            {"id": "unit-2"},
                        ]
                    },
                    "result": {
                        "result": {
                            "findings": [
                                {
                                    "id": "unit-1",
                                    "quote": quote,
                                    "problem": "责任主体和完成时限不明确。",
                                    "suggestion": "明确责任部门和处理时限。",
                                }
                            ]
                        }
                    },
                }
            ],
        },
    }


def test_semantic_callback_validates_and_atomically_completes() -> None:
    repository = FakeAuditRepository()
    service = SemanticAuditService(Settings(semantic_audit_enabled=True), repository)

    result = service.accept_result(callback_payload())

    assert result == {"audit_id": "audit-1", "status": "completed", "finding_count": 1}
    assert repository.saved_findings == [
        {
            "id": "unit-1",
            "quote": "相关部门应及时处理",
            "problem": "责任主体和完成时限不明确。",
            "suggestion": "明确责任部门和处理时限。",
        }
    ]


def test_semantic_callback_rejects_untraceable_quote_without_partial_results() -> None:
    repository = FakeAuditRepository()
    service = SemanticAuditService(Settings(semantic_audit_enabled=True), repository)

    with pytest.raises(ProofError) as exc_info:
        service.accept_result(callback_payload(quote="模型编造的原文"))

    assert exc_info.value.code == "invalid_audit_result"
    assert repository.run["status"] == "failed"
    assert repository.saved_findings is None


def test_semantic_callback_recovers_exact_quote_across_layout_whitespace() -> None:
    repository = FakeAuditRepository()
    repository.units[0]["text"] = (
        "第十九条 重要担保业务合同的订立，\n"
        " 应当征询法律顾问或专家的意见。"
    )
    service = SemanticAuditService(Settings(semantic_audit_enabled=True), repository)

    service.accept_result(
        callback_payload(quote="重要担保业务合同的订立，应当征询法律顾问或专家的意见")
    )

    assert repository.saved_findings[0]["quote"] == (
        "重要担保业务合同的订立，\n 应当征询法律顾问或专家的意见"
    )


def test_semantic_callback_rejects_ambiguous_whitespace_normalized_quote() -> None:
    repository = FakeAuditRepository()
    repository.units[0]["text"] = "相关 部门应处理；相关\n部门应处理。"
    service = SemanticAuditService(Settings(semantic_audit_enabled=True), repository)

    with pytest.raises(ProofError) as exc_info:
        service.accept_result(callback_payload(quote="相关部门应处理"))

    assert exc_info.value.code == "invalid_audit_result"
    assert repository.run["status"] == "failed"
    assert repository.saved_findings is None


def test_batching_preserves_order_and_places_oversized_normal_batch_alone() -> None:
    repository = FakeAuditRepository()
    settings = Settings(
        semantic_audit_enabled=True,
        audit_batch_max_chars=20,
        audit_batch_max_chunks=8,
        audit_max_chunk_chars=100,
    )
    service = SemanticAuditService(settings, repository)

    batches = service._build_batches("audit-1", repository.units)

    assert [[target["id"] for target in item["targets"]] for item in batches] == [
        ["unit-1"],
        ["unit-2"],
    ]


def test_disabled_audit_has_no_run_and_is_confirmable_by_caller() -> None:
    repository = FakeAuditRepository()
    repository.run = None
    repository.get_audit_run_for_document = lambda document_id: None
    service = SemanticAuditService(Settings(semantic_audit_enabled=False), repository)

    assert service.ensure_dispatched("document-1") == {
        "status": "disabled",
        "error_message": None,
    }


def test_dispatch_failure_is_recorded_without_raising(monkeypatch) -> None:
    repository = FakeAuditRepository()
    repository.run = None
    service = SemanticAuditService(
        Settings(semantic_audit_enabled=True, framework_base_url="http://framework.test"),
        repository,
    )
    monkeypatch.setattr(service, "_dispatch", lambda run: (_ for _ in ()).throw(RuntimeError("down")))

    state = service.ensure_dispatched("document-1")

    assert state["status"] == "failed"
    assert state["error_message"] == "down"
