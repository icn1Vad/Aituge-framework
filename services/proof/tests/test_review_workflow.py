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

    def get_latest_policy_operation(self, policy_id):
        return None


class AuditState:
    def __init__(self, status: str, conflict_status: str = "completed", summary_status: str = "completed") -> None:
        self.status = status
        self.conflict_status = conflict_status
        self.intra_conflict_status = "completed"
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
    def intra_conflict_state(self, document_id):
        return {"status": self.intra_conflict_status, "error_message": None}


    def findings(self, document_id):
        return []

    def conflict_findings(self, document_id):
        return []

    def intra_conflict_findings(self, document_id):
        return []


class ReusedPolicyPipeline:
    def ingest_policy(self, **values):
        return {
            "policy": {"id": "policy-1", "status": "effective"},
            "document": {"id": "document-1"},
            "clauses": [],
            "reused": True,
            "ingestion_run_id": "ingestion-2",
        }


class SimilarDraftPipeline:
    def ingest_policy(self, **values):
        return {
            "policy": {
                "id": "policy-new",
                "status": "draft",
                "similarity_state": "decision_required",
            },
            "document": {"id": "document-new"},
            "clauses": [],
            "similarity": {
                "status": "decision_required",
                "candidates": [{"policy_id": "policy-old"}],
            },
            "reused": False,
            "ingestion_run_id": "ingestion-new",
        }


class DispatchingAuditState(AuditState):
    def __init__(self) -> None:
        super().__init__("completed", conflict_status="pending", summary_status="pending")
        self.dispatched_documents = []

    def ensure_dispatched(self, document_id):
        self.dispatched_documents.append(document_id)
        return {
            **self.get_state(document_id),
            "policy_summary": self.summary_state(document_id),
            "conflict_audit": self.conflict_state(document_id),
        }


def service_for(*, audit_enabled: bool, audit_status: str, policy_status: str = "draft") -> ProofService:
    service = ProofService.__new__(ProofService)
    service.settings = Settings(semantic_audit_enabled=audit_enabled)
    service.repository = ReviewRepository(policy_status)
    service.policy_audit_service = AuditState(audit_status)
    service._dispatch_policy_action = lambda **values: {
        "id": values["policy_id"],
        "status": "accepted",
        "operation_id": values["operation_id"],
        "operation": values["action"],
        "operation_status": "ACCEPTED",
        "framework_task_id": "mutation-task-1",
        "framework_run_id": "mutation-run-1",
    }
    return service


def test_confirm_blocks_until_enabled_semantic_audit_completes() -> None:
    service = service_for(audit_enabled=True, audit_status="running")

    with pytest.raises(ProofError) as exc_info:
        service.confirm_policy("policy-1")

    assert exc_info.value.code == "semantic_audit_incomplete"
    service.policy_audit_service.status = "completed"
    assert service.confirm_policy("policy-1", idempotency_key="confirm-1")["status"] == "accepted"
    assert service.repository.policy["status"] == "draft"


def test_disabled_semantic_audit_allows_explicit_confirmation() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")

    assert service.confirm_policy("policy-1", idempotency_key="confirm-1")["status"] == "accepted"
    assert service.repository.policy["status"] == "draft"


def test_policy_lifecycle_action_requires_idempotency_key() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")

    with pytest.raises(ProofError) as exc_info:
        service.confirm_policy("policy-1")

    assert exc_info.value.code == "invalid_idempotency_key"


def test_confirm_blocks_until_conflict_audit_completes() -> None:
    service = service_for(audit_enabled=True, audit_status="completed")
    service.policy_audit_service.conflict_status = "failed"

    with pytest.raises(ProofError) as exc_info:
        service.confirm_policy("policy-1")

    assert exc_info.value.code == "conflict_audit_incomplete"



def test_confirm_blocks_until_intra_conflict_audit_completes() -> None:
    service = service_for(audit_enabled=True, audit_status="completed")
    service.policy_audit_service.intra_conflict_status = "failed"

    with pytest.raises(ProofError) as exc_info:
        service.confirm_policy("policy-1")

    assert exc_info.value.code == "intra_conflict_audit_incomplete"


def test_audit_status_is_lightweight_and_keeps_running_after_one_stage_fails() -> None:
    service = service_for(audit_enabled=True, audit_status="running")
    service.policy_audit_service.summary_status = "failed"

    status = service.get_audit_status("policy-1")

    assert status == {
        "id": "audit-1",
        "policy_id": "policy-1",
        "framework_task_id": "task-1",
        "framework_run_id": "run-1",
        "status": "running",
        "policy_status": "draft",
        "policy_operation": None,
        "can_confirm": False,
        "stages": {
            "policy_summary": {"status": "failed", "error_message": None},
            "semantic_audit": {"status": "running", "error_message": None},
            "conflict_audit": {"status": "completed", "error_message": None},
            "intra_conflict_audit": {"status": "completed", "error_message": None},
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
            "intra_conflict_total": 0,
            "intra_numeric_conflict": 0,
            "intra_authority_conflict": 0,
            "intra_process_conflict": 0,
            "intra_rule_reversal": 0,
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


def test_reused_effective_policy_redispatches_missing_summary_and_conflict_stages() -> None:
    service = object.__new__(ProofService)
    service.ingestion_pipeline = ReusedPolicyPipeline()
    service.policy_audit_service = DispatchingAuditState()

    result = service.ingest_policy(content=b"same", filename="same.txt")

    assert service.policy_audit_service.dispatched_documents == ["document-1"]
    assert result["reused"] is True
    assert result["audit_task"]["status"] == "pending"


def test_similarity_draft_does_not_dispatch_review_before_decision() -> None:
    service = object.__new__(ProofService)
    service.ingestion_pipeline = SimilarDraftPipeline()
    service.policy_audit_service = DispatchingAuditState()

    result = service.ingest_policy(content=b"similar", filename="similar.txt")

    assert service.policy_audit_service.dispatched_documents == []
    assert result["audit_task"] == {
        "id": None,
        "framework_task_id": None,
        "framework_run_id": None,
        "status": "not_started",
        "reason": "similarity_decision_required",
    }


def test_new_version_embedding_failure_does_not_activate_policy() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")
    service.repository.policy["supersedes_policy_id"] = "policy-old"
    service.model_runtime = type("Runtime", (), {"embedding_configured": True})()
    service._index_document = lambda *args, **kwargs: (_ for _ in ()).throw(
        ProofError("embedding_failed", "Embedding failed.", status_code=502)
    )

    with pytest.raises(ProofError) as exc_info:
        service.confirm_policy("policy-1")

    assert exc_info.value.code == "embedding_failed"
    assert service.repository.policy["status"] == "draft"


def test_draft_document_cannot_be_indexed() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")

    with pytest.raises(ProofError) as exc_info:
        service.index_document("document-1")

    assert exc_info.value.code == "policy_not_effective"
