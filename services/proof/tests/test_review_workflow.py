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
            "version": "v1.0.0",
            "version_seq": 0,
        }
        self.effective_family_policy = None
        self.operations = {}

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

    def get_policy_operation(self, operation_id):
        return self.operations.get(operation_id)

    def get_policy_operation_by_idempotency_key(self, idempotency_key):
        return next(
            (
                {"operation_id": operation_id, **operation}
                for operation_id, operation in self.operations.items()
                if operation.get("idempotency_key") == idempotency_key
            ),
            None,
        )

    def get_effective_family_policy(self, policy_id):
        return self.effective_family_policy

    def record_policy_operation(self, *, operation_id, policy_id, action, status, **values):
        self.operations[operation_id] = {
            "policy_id": policy_id,
            "action": action,
            "status": status,
            **values,
        }

    def complete_policy_operation(self, operation_id, *, status, error_message=None):
        self.operations[operation_id].update(
            {"status": status, "error_message": error_message}
        )

    def claim_policy_operation(
        self,
        *,
        operation_id,
        policy_id,
        action,
        idempotency_key,
        request_fingerprint,
    ):
        existing = self.get_policy_operation_by_idempotency_key(idempotency_key)
        if existing is None and operation_id in self.operations:
            existing = {"operation_id": operation_id, **self.operations[operation_id]}
        if existing is not None:
            if (
                existing["policy_id"] != policy_id
                or existing["action"] != action
                or (
                    existing.get("request_fingerprint")
                    and existing["request_fingerprint"] != request_fingerprint
                )
            ):
                raise ProofError(
                    "idempotency_conflict",
                    "The lifecycle operation was already used with different parameters.",
                    status_code=409,
                )
            if existing["status"] != "FAILED":
                return False, existing
            operation_id = existing["operation_id"]
            self.operations[operation_id].update(
                {
                    "status": "ACCEPTED",
                    "error_message": None,
                    "attempt_count": int(existing.get("attempt_count") or 1) + 1,
                }
            )
            return True, {"operation_id": operation_id, **self.operations[operation_id]}
        for active_id, active in self.operations.items():
            if active["policy_id"] == policy_id and active["status"] in {
                "ACCEPTED",
                "RUNNING",
            }:
                raise ProofError(
                    "policy_operation_in_progress",
                    "Another lifecycle operation is already running for this policy.",
                    status_code=409,
                )
        operation = {
            "policy_id": policy_id,
            "action": action,
            "status": "ACCEPTED",
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "attempt_count": 1,
        }
        self.operations[operation_id] = operation
        return True, {"operation_id": operation_id, **operation}

    def activate_policy_version(self, policy_id, *, replace_existing=False):
        if policy_id != self.policy["id"]:
            return None
        self.policy["status"] = "effective"
        return dict(self.policy)


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
        service.execute_policy_action(
            "policy-1", action="activate", idempotency_key="confirm-running"
        )

    assert exc_info.value.code == "semantic_audit_incomplete"
    service.policy_audit_service.status = "completed"
    assert service.execute_policy_action(
        "policy-1", action="activate", idempotency_key="confirm-1"
    )["status"] == "accepted"
    assert service.repository.policy["status"] == "draft"


def test_disabled_semantic_audit_allows_explicit_confirmation() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")

    assert service.execute_policy_action(
        "policy-1", action="activate", idempotency_key="confirm-1"
    )["status"] == "accepted"
    assert service.repository.policy["status"] == "draft"


def test_running_idempotent_activation_replay_stays_accepted() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")

    first = service.execute_policy_action(
        "policy-1", action="activate", idempotency_key="confirm-replay"
    )
    service.repository.operations[first["operation_id"]] = {
        "policy_id": "policy-1",
        "action": "activate",
        "status": "RUNNING",
        "framework_task_id": "mutation-task-1",
        "framework_run_id": "mutation-run-1",
    }

    replay = service.execute_policy_action(
        "policy-1", action="activate", idempotency_key="confirm-replay"
    )

    assert replay["status"] == "accepted"
    assert replay["policy_status"] == "draft"
    assert replay["operation_status"] == "RUNNING"
    assert replay["operation_id"] == first["operation_id"]


def test_different_key_cannot_start_a_second_active_policy_operation() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")

    first = service.execute_policy_action(
        "policy-1", action="activate", idempotency_key="confirm-first"
    )

    with pytest.raises(ProofError) as exc_info:
        service.execute_policy_action(
            "policy-1", action="activate", idempotency_key="confirm-second"
        )

    assert first["status"] == "accepted"
    assert exc_info.value.code == "policy_operation_in_progress"
    assert len(service.repository.operations) == 1


def test_lifecycle_idempotency_key_cannot_be_reused_for_different_action() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")

    service.execute_policy_action(
        "policy-1", action="activate", idempotency_key="shared-request"
    )

    with pytest.raises(ProofError) as exc_info:
        service.execute_policy_action(
            "policy-1", action="discard", idempotency_key="shared-request"
        )

    assert exc_info.value.code == "idempotency_conflict"


def test_failed_lifecycle_operation_retries_with_next_attempt() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")
    dispatched = []
    service._dispatch_policy_action = lambda **values: dispatched.append(values) or {
        "id": values["policy_id"],
        "status": "accepted",
        "operation_id": values["operation_id"],
    }

    first = service.execute_policy_action(
        "policy-1", action="activate", idempotency_key="retry-request"
    )
    operation_id = first["operation_id"]
    service.repository.complete_policy_operation(
        operation_id,
        status="FAILED",
        error_message="temporary failure",
    )

    retried = service.execute_policy_action(
        "policy-1", action="activate", idempotency_key="retry-request"
    )

    assert retried["operation_id"] == operation_id
    assert [item["attempt_count"] for item in dispatched] == [1, 2]
    assert len(service.repository.operations) == 1


def test_confirm_requires_explicit_replacement_for_higher_effective_version() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")
    service.repository.effective_family_policy = {
        "id": "policy-2",
        "title": "Policy",
        "version": "v1.0.2",
        "version_seq": 2,
    }
    dispatched = []
    service._dispatch_policy_action = lambda **values: dispatched.append(values) or {
        "id": values["policy_id"],
        "status": "accepted",
    }

    with pytest.raises(ProofError) as exc_info:
        service.execute_policy_action(
            "policy-1", action="activate", idempotency_key="confirm-standard"
        )

    assert exc_info.value.code == "higher_version_effective"
    assert service.execute_policy_action(
        "policy-1",
        action="activate",
        idempotency_key="confirm-replace",
        replace_existing=True,
    )["status"] == "accepted"
    assert dispatched[0]["replace_existing"] is True


def test_policy_lifecycle_action_requires_idempotency_key() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")

    with pytest.raises(ProofError) as exc_info:
        service.execute_policy_action(
            "policy-1", action="activate", idempotency_key=None
        )

    assert exc_info.value.code == "invalid_idempotency_key"


def test_confirm_blocks_until_conflict_audit_completes() -> None:
    service = service_for(audit_enabled=True, audit_status="completed")
    service.policy_audit_service.conflict_status = "failed"

    with pytest.raises(ProofError) as exc_info:
        service.execute_policy_action(
            "policy-1", action="activate", idempotency_key="confirm-conflict"
        )

    assert exc_info.value.code == "conflict_audit_incomplete"



def test_confirm_blocks_until_intra_conflict_audit_completes() -> None:
    service = service_for(audit_enabled=True, audit_status="completed")
    service.policy_audit_service.intra_conflict_status = "failed"

    with pytest.raises(ProofError) as exc_info:
        service.execute_policy_action(
            "policy-1", action="activate", idempotency_key="confirm-intra"
        )

    assert exc_info.value.code == "intra_conflict_audit_incomplete"


def test_audit_status_is_lightweight_and_keeps_running_after_one_stage_fails() -> None:
    service = service_for(audit_enabled=True, audit_status="running")
    service.policy_audit_service.summary_status = "failed"

    status = service.get_review_status("policy-1")

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


def test_reused_effective_policy_is_rejected_as_exact_duplicate() -> None:
    service = object.__new__(ProofService)
    service.ingestion_pipeline = ReusedPolicyPipeline()
    service.policy_audit_service = DispatchingAuditState()
    service.repository = ReviewRepository()
    service.repository.reserve_policy_create_request = lambda **values: {"state": "created"}
    service.repository.fail_policy_create_request = lambda **values: None
    service.preview_policy_similarity = lambda **values: {"status": "clear"}

    with pytest.raises(ProofError) as exc_info:
        service.ingest_policy(
            content=b"same",
            filename="same.txt",
            idempotency_key="create-same",
        )

    assert exc_info.value.code == "policy_exact_duplicate"
    assert service.policy_audit_service.dispatched_documents == []


def test_similarity_conflict_does_not_create_draft_before_decision() -> None:
    service = object.__new__(ProofService)
    service.ingestion_pipeline = SimilarDraftPipeline()
    service.policy_audit_service = DispatchingAuditState()
    service.repository = ReviewRepository()
    service.repository.reserve_policy_create_request = lambda **values: {"state": "created"}
    service.repository.fail_policy_create_request = lambda **values: None
    service.preview_policy_similarity = lambda **values: {
        "status": "decision_required",
        "candidates": [{"source_id": "policy-old"}],
    }

    with pytest.raises(ProofError) as exc_info:
        service.ingest_policy(
            content=b"similar",
            filename="similar.txt",
            idempotency_key="create-similar",
        )

    assert exc_info.value.code == "similarity_decision_required"
    assert service.policy_audit_service.dispatched_documents == []


def test_create_failure_discards_the_incomplete_draft() -> None:
    service = object.__new__(ProofService)
    service.ingestion_pipeline = SimilarDraftPipeline()
    service.repository = ReviewRepository()
    service.repository.reserve_policy_create_request = lambda **values: {"state": "reserved"}
    service.repository.fail_policy_create_request = lambda **values: None
    service.preview_policy_similarity = lambda **values: {"status": "clear"}
    service._finalize_ingestion_result = lambda *args, **kwargs: (_ for _ in ()).throw(
        ProofError("policy_version_conflict", "Version conflict.", status_code=409)
    )
    discarded = []
    service._discard_policy = discarded.append

    with pytest.raises(ProofError) as exc_info:
        service.ingest_policy(
            content=b"concurrent conflict",
            filename="conflict.txt",
            idempotency_key="create-conflict",
        )

    assert exc_info.value.code == "policy_version_conflict"
    assert discarded == ["policy-new"]


def test_new_version_embedding_failure_does_not_activate_policy() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")
    service.repository.policy["supersedes_policy_id"] = "policy-old"
    service.model_runtime = type("Runtime", (), {"embedding_configured": True})()
    service._index_document = lambda *args, **kwargs: (_ for _ in ()).throw(
        ProofError("embedding_failed", "Embedding failed.", status_code=502)
    )

    with pytest.raises(ProofError) as exc_info:
        service.apply_policy_action(
            policy_id="policy-1",
            action="activate",
            operation_id="activate-failed",
        )

    assert exc_info.value.code == "embedding_failed"
    assert service.repository.policy["status"] == "draft"
    assert service.repository.operations["activate-failed"]["status"] == "FAILED"


def test_expired_policy_can_be_reindexed_for_reactivation(monkeypatch) -> None:
    service = service_for(
        audit_enabled=False,
        audit_status="disabled",
        policy_status="expired",
    )
    service.model_runtime = type(
        "Runtime",
        (),
        {
            "embedding_configured": True,
            "embedding": object(),
        },
    )()
    service.repository.get_document_units = lambda document_id: [
        {"id": "unit-1", "text": "Policy clause"}
    ]
    replaced_embeddings = []
    service.repository.replace_embeddings = (
        lambda document_id, units, vectors, profile, *, too_long_unit_ids: (
            replaced_embeddings.append(
                {
                    "document_id": document_id,
                    "units": units,
                    "vectors": vectors,
                    "profile": profile,
                    "too_long_unit_ids": too_long_unit_ids,
                }
            )
        )
    )

    class FakeEmbeddingClient:
        profile = type(
            "Profile",
            (),
            {
                "id": "profile-1",
                "provider": "test",
                "model": "test-model",
                "dimensions": 3,
            },
        )()

        def __init__(self, config) -> None:
            self.config = config

        def embed(self, texts):
            return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(
        "proof.application.service.OpenAICompatibleEmbeddingClient",
        FakeEmbeddingClient,
    )

    result = service.apply_policy_action(
        policy_id="policy-1",
        action="activate",
        operation_id="reactivate-expired-policy",
    )

    assert result["status"] == "effective"
    assert service.repository.operations["reactivate-expired-policy"]["status"] == "SUCCEEDED"
    assert replaced_embeddings == [
        {
            "document_id": "document-1",
            "units": [{"id": "unit-1", "text": "Policy clause"}],
            "vectors": [[1.0, 0.0, 0.0]],
            "profile": FakeEmbeddingClient.profile,
            "too_long_unit_ids": [],
        }
    ]


def test_draft_document_cannot_be_indexed() -> None:
    service = service_for(audit_enabled=False, audit_status="disabled")

    with pytest.raises(ProofError) as exc_info:
        service._index_document("document-1", require_effective=True)

    assert exc_info.value.code == "policy_not_effective"


def test_policy_delete_commits_metadata_before_moving_source_file(tmp_path) -> None:
    source = tmp_path / "tenant" / "policy.txt"
    source.parent.mkdir(parents=True)
    source.write_text("policy", encoding="utf-8")
    calls = []

    class DeleteRepository:
        def get_delete_tombstone(self, operation_id):
            return None

        def get_policy(self, policy_id):
            return {"id": policy_id, "storage_path": "tenant/policy.txt"}

        def delete_policy_version(self, policy_id, **values):
            calls.append("database")
            assert source.is_file()
            return {"policy_id": policy_id, **values}

        def mark_delete_cleaned(self, operation_id):
            calls.append("cleanup")

        def mark_delete_cleanup_failed(self, operation_id, message):
            raise AssertionError(message)

    service = object.__new__(ProofService)
    service.repository = DeleteRepository()
    service.storage_root = tmp_path.resolve()

    result = service._delete_policy_version("policy-1", operation_id="delete-1")

    assert result["status"] == "deleted"
    assert calls == ["database", "cleanup"]
    assert not source.exists()
