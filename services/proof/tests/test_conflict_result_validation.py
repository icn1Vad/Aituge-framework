from __future__ import annotations

import pytest

from proof.application.service import ProofService
from proof.application.semantic_audit import PolicyAuditService
from proof.config import Settings
from proof.errors import ProofError


class _Repository:
    def __init__(self) -> None:
        self.units = {
            "source-unit": {
                "id": "source-unit",
                "policy_id": "policy-a",
                "document_id": "document-a",
                "policy_title": "费用报销管理办法",
                "policy_version": "1.0",
                "original_name": "a.docx",
                "clause_no_raw": "第一条",
                "clause_ordinal": 1,
                "heading_path": [],
                "text": "报销申请应当在三十日内提交。",
            },
            "candidate-unit": {
                "id": "candidate-unit",
                "policy_id": "policy-b",
                "document_id": "document-b",
                "policy_title": "财务管理制度",
                "policy_version": "1.0",
                "original_name": "b.docx",
                "clause_no_raw": "第二条",
                "clause_ordinal": 2,
                "heading_path": [],
                "text": "报销申请应当在十五日内提交。",
            },
        }

    def get_conflict_source_unit(self, unit_id: str):
        return self.units.get(unit_id)


def _service() -> ProofService:
    service = object.__new__(ProofService)
    service.repository = _Repository()
    return service


def _payload() -> dict:
    return {
        "task_type": "proof.conflict.audit",
        "audit_id": "audit-1",
        "output": {
            "items": [
                {
                    "status": "succeeded",
                    "input": {"targets": [{"id": "source-unit", "unit_id": "source-unit"}]},
                    "result": {
                        "result": {
                            "findings": [
                                {
                                    "id": "source-unit",
                                    "candidate_ids": ["candidate-unit"],
                                    "conflict_type": "numeric_conflict",
                                    "problem": "同一报销申请不能同时按三十日和十五日执行。",
                                    "suggestion": "统一报销申请提交期限。",
                                }
                            ]
                        }
                    },
                }
            ]
        },
    }


def test_conflict_result_sink_validates_chunk_ids() -> None:
    result = _service().accept_conflict_audit_result(_payload())

    assert result == {"audit_id": "audit-1", "status": "validated", "finding_count": 1}


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        (lambda finding: finding.update(id="wrong-unit"), "conflict_target_mismatch"),
        (lambda finding: finding.update(candidate_ids=["missing-unit"]), "conflict_unit_not_found"),
    ],
)
def test_conflict_result_sink_rejects_invalid_chunk_ids(mutation, error_code) -> None:
    payload = _payload()
    finding = payload["output"]["items"][0]["result"]["result"]["findings"][0]
    mutation(finding)

    with pytest.raises(ProofError) as exc_info:
        _service().accept_conflict_audit_result(payload)

    assert exc_info.value.code == error_code


class _IntegratedRepository(_Repository):
    def __init__(self) -> None:
        super().__init__()
        self.units["source-unit"].update(policy_status="draft")
        self.units["candidate-unit"].update(policy_status="effective")
        self.run = {
            "id": "audit-1",
            "document_id": "document-a",
            "status": "completed",
            "conflict_status": "running",
            "framework_task_id": "task-1",
            "framework_run_id": "run-1",
        }
        self.saved_conflicts = None
        self.conflict_error = None

    def get_audit_run(self, audit_id):
        return dict(self.run) if audit_id == self.run["id"] else None

    def get_audit_run_for_document(self, document_id):
        return dict(self.run) if document_id == self.run["document_id"] else None

    def get_document_units(self, document_id):
        if document_id != self.run["document_id"]:
            return []
        return [{"id": "source-unit", "text": self.units["source-unit"]["text"]}]

    def complete_conflict_audit(self, audit_id, findings):
        self.saved_conflicts = findings
        self.run["conflict_status"] = "completed"

    def mark_conflict_audit_failed(self, audit_id, message):
        self.conflict_error = message
        self.run["conflict_status"] = "failed"


def _integrated_payload() -> dict:
    payload = _payload()
    payload.update(
        task_type="proof.audit.run",
        task_id="task-1",
        run_id="run-1",
        stage_id="conflict_audit",
        status="completed",
    )
    finding = payload["output"]["items"][0]["result"]["result"]["findings"][0]
    return payload


def test_integrated_conflict_callback_persists_validated_findings() -> None:
    repository = _IntegratedRepository()
    semantic = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    proof = object.__new__(ProofService)
    proof.repository = repository

    result = semantic.accept_result(
        _integrated_payload(),
        conflict_output_validator=proof._validate_conflict_output,
    )

    assert result == {
        "audit_id": "audit-1",
        "stage_id": "conflict_audit",
        "status": "completed",
        "finding_count": 1,
    }
    assert repository.saved_conflicts[0]["conflict_type"] == "numeric_conflict"
    assert repository.saved_conflicts[0]["candidate_ids"] == ["candidate-unit"]


def test_integrated_conflict_callback_rejects_non_effective_candidate() -> None:
    repository = _IntegratedRepository()
    repository.units["candidate-unit"]["policy_status"] = "draft"
    semantic = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    proof = object.__new__(ProofService)
    proof.repository = repository

    with pytest.raises(ProofError) as exc_info:
        semantic.accept_result(
            _integrated_payload(),
            conflict_output_validator=proof._validate_conflict_output,
        )

    assert exc_info.value.code == "conflict_candidate_not_effective"
    assert repository.saved_conflicts is None
    assert repository.run["conflict_status"] == "failed"


def test_integrated_conflict_stage_failure_is_recorded_separately() -> None:
    repository = _IntegratedRepository()
    semantic = PolicyAuditService(Settings(semantic_audit_enabled=True), repository)
    payload = _integrated_payload()
    payload.update(status="failed", output=None, error_message="conflict agent timeout")

    result = semantic.accept_result(payload, conflict_output_validator=lambda value: [])

    assert result["status"] == "failed"
    assert repository.run["status"] == "completed"
    assert repository.run["conflict_status"] == "failed"
    assert repository.conflict_error == "conflict agent timeout"
