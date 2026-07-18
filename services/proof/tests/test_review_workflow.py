from __future__ import annotations

import pytest

from proof.application.service import ProofService, _audit_task_view
from proof.config import Settings
from proof.errors import ProofError


class ReviewRepository:
    def __init__(self, status: str = "draft") -> None:
        self.policy = {
            "id": "policy-1",
            "title": "Policy",
            "document_id": "document-1",
            "status": status,
            "structure_profile": "article",
        }

    def get_policy(self, policy_id):
        return dict(self.policy) if policy_id == self.policy["id"] else None

    def confirm_policy(self, policy_id):
        self.policy["status"] = "effective"
        return dict(self.policy)

    def get_policy_by_document_id(self, document_id):
        return dict(self.policy) if document_id == self.policy["document_id"] else None

    def list_clauses(self, policy_id, *, include_text=False):
        return []


class AuditState:
    def __init__(self, status: str, conflict_status: str = "completed", summary_status: str = "completed") -> None:
        self.status = status
        self.conflict_status = conflict_status
        self.summary_status = summary_status

    def get_state(self, document_id):
        return {
            "id": "audit-1",
            "status": self.status,
            "error_message": None,
            "framework_task_id": "task-1",
            "framework_run_id": "run-1",
        }

    def summary_state(self, document_id):
        return {"status": self.summary_status, "error_message": None, "content": None}

    def conflict_state(self, document_id):
        return {"status": self.conflict_status, "error_message": None}

    def findings(self, document_id):
        return []

    def conflict_findings(self, document_id):
        return []


def service_for(*, audit_enabled: bool, audit_status: str, policy_status: str = "draft") -> ProofService:
    service = ProofService.__new__(ProofService)
    service.settings = Settings(semantic_audit_enabled=audit_enabled)
    service.repository = ReviewRepository(policy_status)
    service.semantic_audit_service = AuditState(audit_status)
    return service


def test_confirm_blocks_until_enabled_semantic_audit_completes() -> None:
    service = service_for(audit_enabled=True, audit_status="running")

    with pytest.raises(ProofError) as exc_info:
        service.confirm_policy("policy-1")

    assert exc_info.value.code == "semantic_audit_incomplete"
    service.semantic_audit_service.status = "completed"
    assert service.confirm_policy("policy-1")["status"] == "effective"
    assert service.confirm_policy("policy-1")["status"] == "effective"


def test_disabled_semantic_audit_allows_explicit_confirmation() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")

    assert service.confirm_policy("policy-1")["status"] == "effective"


def test_confirm_blocks_until_conflict_audit_completes() -> None:
    service = service_for(audit_enabled=True, audit_status="completed")
    service.semantic_audit_service.conflict_status = "failed"

    with pytest.raises(ProofError) as exc_info:
        service.confirm_policy("policy-1")

    assert exc_info.value.code == "conflict_audit_incomplete"


def test_audit_status_is_lightweight_and_keeps_running_after_one_stage_fails() -> None:
    service = service_for(audit_enabled=True, audit_status="running")
    service.semantic_audit_service.summary_status = "failed"

    status = service.get_audit_status("policy-1")

    assert status == {
        "id": "audit-1",
        "policy_id": "policy-1",
        "framework_task_id": "task-1",
        "framework_run_id": "run-1",
        "status": "running",
        "can_confirm": False,
        "stages": {
            "policy_summary": {"status": "failed", "error_message": None},
            "semantic_audit": {"status": "running", "error_message": None},
            "conflict_audit": {"status": "completed", "error_message": None},
        },
        "counts": {
            "clause_total": 0,
            "semantic_ambiguity": None,
            "executability_gap": None,
            "duplicate_number": 0,
            "missing_number": 0,
            "mixed_structure": 0,
            "conflict_total": 0,
            "numeric_conflict": 0,
            "authority_conflict": 0,
            "process_conflict": 0,
            "rule_reversal": 0,
        },
    }


def test_upload_audit_task_view_never_contains_stage_content() -> None:
    view = _audit_task_view(
        {
            "id": "audit-1",
            "status": "completed",
            "framework_task_id": "task-1",
            "framework_run_id": "run-1",
            "policy_summary": {"status": "completed", "content": {"plain_summary": "long report"}},
            "conflict_audit": {"status": "running"},
        }
    )

    assert view == {
        "id": "audit-1",
        "framework_task_id": "task-1",
        "framework_run_id": "run-1",
        "status": "running",
    }


def test_draft_document_cannot_be_indexed() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")

    with pytest.raises(ProofError) as exc_info:
        service.index_document("document-1")

    assert exc_info.value.code == "policy_not_effective"
