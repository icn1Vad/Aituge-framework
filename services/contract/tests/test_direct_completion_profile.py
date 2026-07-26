from datetime import datetime, timezone
from types import SimpleNamespace

from contract.application.framework_gateway import FrameworkRunSnapshot
from contract.application.ports import InternalRequestContext
from contract.application.runtime_service import RuntimeContractReviewService
from contract.persistence.postgres.callback_repository import (
    DIRECT_REQUIRED_STAGE_IDS,
    LEGACY_REQUIRED_STAGE_IDS,
    _matches_required_stage_profile,
)


def test_direct_and_legacy_callback_profiles_are_both_complete() -> None:
    assert _matches_required_stage_profile(set(DIRECT_REQUIRED_STAGE_IDS))
    assert _matches_required_stage_profile(set(LEGACY_REQUIRED_STAGE_IDS))


def test_partial_or_mixed_callback_profiles_are_not_complete() -> None:
    assert not _matches_required_stage_profile(
        set(DIRECT_REQUIRED_STAGE_IDS) - {"finalize_review"}
    )
    assert not _matches_required_stage_profile(
        set(DIRECT_REQUIRED_STAGE_IDS) | {"commercial_terms_review"}
    )


def test_succeeded_framework_run_reconciles_persisted_final_result() -> None:
    state = {
        "id": "review-1",
        "active_attempt": {
            "attempt_no": 1,
            "framework_task_id": "task-1",
            "framework_run_id": "run-1",
        },
    }
    completed_state = {"id": "review-1", "status": "SUCCEEDED"}
    snapshot = FrameworkRunSnapshot(
        task_id="task-1",
        run_id="run-1",
        status="succeeded",
        current_stage_id="FINALIZING",
        cancel_requested=False,
        updated_at=datetime.now(timezone.utc),
    )

    class Gateway:
        def get_run(self, *args, **kwargs):
            return snapshot

    class StateRepository:
        def get_state(self, *args, **kwargs):
            return completed_state

    class CompletionRepository:
        def __init__(self) -> None:
            self.calls = []

        def finish_if_ready(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return True

    completion_repository = CompletionRepository()
    service = RuntimeContractReviewService(
        settings=SimpleNamespace(),
        repository=SimpleNamespace(),
        state_repository=StateRepository(),
        document_processor=SimpleNamespace(),
        framework_gateway=Gateway(),
        completion_repository=completion_repository,
    )

    result = service._reconcile_running(
        state,
        context=InternalRequestContext(
            tenant_id="tenant-1",
            user_id="user-1",
            request_id="request-1",
            idempotency_key=None,
        ),
    )

    assert result == completed_state
    assert completion_repository.calls == [
        (
            ("review-1",),
            {
                "tenant_id": "tenant-1",
                "user_id": "user-1",
                "attempt_no": 1,
            },
        )
    ]
