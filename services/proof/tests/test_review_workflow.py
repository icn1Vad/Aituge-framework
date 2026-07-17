from __future__ import annotations

import pytest

from proof.application.service import ProofService
from proof.config import Settings
from proof.errors import ProofError


class ReviewRepository:
    def __init__(self, status: str = "draft") -> None:
        self.policy = {"id": "policy-1", "document_id": "document-1", "status": status}

    def get_policy(self, policy_id):
        return dict(self.policy) if policy_id == self.policy["id"] else None

    def confirm_policy(self, policy_id):
        self.policy["status"] = "effective"
        return dict(self.policy)

    def get_policy_by_document_id(self, document_id):
        return dict(self.policy) if document_id == self.policy["document_id"] else None


class AuditState:
    def __init__(self, status: str) -> None:
        self.status = status

    def get_state(self, document_id):
        return {"status": self.status, "error_message": None}


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


def test_draft_document_cannot_be_indexed() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")

    with pytest.raises(ProofError) as exc_info:
        service.index_document("document-1")

    assert exc_info.value.code == "policy_not_effective"
