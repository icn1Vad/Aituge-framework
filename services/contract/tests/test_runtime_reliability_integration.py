from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from contract.api.models import CreateReviewRequest, ErrorData, Perspective, ReviewStatus
from contract.application.document_processing import ContractDocumentProcessor
from contract.application.framework_gateway import (
    FrameworkExecutionRequest,
    FrameworkRunSnapshot,
    FrameworkUnavailableError,
)
from contract.application.ports import InternalRequestContext, UploadedContract
from contract.application.runtime_service import RuntimeContractReviewService
from contract.callback.models import FrameworkTaskInput, RunFailedCallback, StageExecuteRequest
from contract.config import Settings
from contract.errors import ContractError
from contract.internal.service import ContractInternalService
from contract.persistence.postgres.callback_repository import FrameworkCallbackRepository
from contract.persistence.postgres.migrate import run_migrations
from contract.persistence.postgres.repository import ContractRepository
from contract.persistence.postgres.review_state import ReviewStateRepository
from pdf_factory import text_pdf_bytes


DATABASE_URL = os.getenv("CONTRACT_TEST_DATABASE_URL", "")


class FakeFrameworkGateway:
    def __init__(self) -> None:
        self.executions: dict[tuple[str, int], FrameworkRunSnapshot] = {}
        self.requests: list[FrameworkExecutionRequest] = []
        self.cancelled_run_ids: list[str] = []
        self.unavailable_creates = 0
        self.on_create = None

    def create_execution(self, request: FrameworkExecutionRequest) -> FrameworkRunSnapshot:
        self.requests.append(request)
        if self.unavailable_creates:
            self.unavailable_creates -= 1
            raise FrameworkUnavailableError()
        key = (request.review_id, request.attempt_no)
        existing = self.executions.get(key)
        if existing is not None:
            return existing
        snapshot = FrameworkRunSnapshot(
            task_id=f"framework-task-{request.review_id}-{request.attempt_no}",
            run_id=f"framework-run-{request.review_id}-{request.attempt_no}",
            status="running",
            current_stage_id="PARSING",
            cancel_requested=False,
            updated_at=datetime.now(timezone.utc),
        )
        self.executions[key] = snapshot
        if self.on_create is not None:
            self.on_create(request)
        return snapshot

    def get_run(
        self,
        task_id: str,
        run_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> FrameworkRunSnapshot:
        return next(value for value in self.executions.values() if value.run_id == run_id)

    def cancel_run(
        self,
        task_id: str,
        run_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> FrameworkRunSnapshot:
        self.cancelled_run_ids.append(run_id)
        key, current = next(
            (item for item in self.executions.items() if item[1].run_id == run_id),
            (
                ("external", len(self.cancelled_run_ids)),
                FrameworkRunSnapshot(
                    task_id=task_id,
                    run_id=run_id,
                    status="running",
                    current_stage_id="PARSING",
                    cancel_requested=False,
                    updated_at=datetime.now(timezone.utc),
                ),
            ),
        )
        cancelled = FrameworkRunSnapshot(
            task_id=task_id,
            run_id=run_id,
            status="cancelled",
            current_stage_id=current.current_stage_id,
            cancel_requested=True,
            updated_at=datetime.now(timezone.utc),
        )
        self.executions[key] = cancelled
        return cancelled

    def make_stale(self, review_id: str, attempt_no: int) -> None:
        key = (review_id, attempt_no)
        current = self.executions[key]
        self.executions[key] = FrameworkRunSnapshot(
            task_id=current.task_id,
            run_id=current.run_id,
            status="running",
            current_stage_id=current.current_stage_id,
            cancel_requested=False,
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_transient_dispatch_is_resumed_with_frozen_framework_keys(tmp_path: Path) -> None:
    service, gateway, context, request, upload, tenant_id = _runtime(tmp_path)
    gateway.unavailable_creates = 1
    try:
        created = service.create_review(upload=upload, request=request, context=context)
        assert created.status == ReviewStatus.CREATED
        assert created.framework_attempt_no is None

        restarted_service = _recreate_service(service.settings, gateway)
        assert restarted_service.reconcile_nonterminal_reviews() == 1
        assert gateway.cancelled_run_ids == []

        resumed = restarted_service.create_review(upload=upload, request=request, context=context)
        assert resumed.review_id == created.review_id
        assert resumed.reused is True
        assert resumed.status == ReviewStatus.RUNNING
        assert resumed.framework_attempt_no == 1
        assert gateway.requests[-1].task_idempotency_key == (
            f"contract-review:{created.review_id}:attempt:1"
        )
        assert gateway.requests[-1].run_idempotency_key == (
            f"contract-review:{created.review_id}:attempt:1:run"
        )

        state = restarted_service.get_status(created.review_id, context=context)
        assert state.status == ReviewStatus.RUNNING
        assert state.framework_task_id == resumed.framework_task_id
        assert len(gateway.executions) == 1
    finally:
        _cleanup(tenant_id)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_orphaned_attempt_is_recovered_once_then_fails_retryably(tmp_path: Path) -> None:
    service, gateway, context, request, upload, tenant_id = _runtime(tmp_path)
    try:
        created = service.create_review(upload=upload, request=request, context=context)
        assert created.framework_attempt_no == 1
        _age_attempt(created.review_id, 1)
        gateway.make_stale(created.review_id, 1)

        recovered = service.get_status(created.review_id, context=context)
        assert recovered.status == ReviewStatus.RUNNING
        assert recovered.framework_attempt_no == 2
        assert created.framework_run_id in gateway.cancelled_run_ids
        with psycopg.connect(DATABASE_URL) as conn:
            rows = conn.execute(
                """
                SELECT attempt_no, status, is_active
                FROM contract_framework_attempt
                WHERE review_id = %s
                ORDER BY attempt_no
                """,
                (created.review_id,),
            ).fetchall()
        assert rows == [(1, "ORPHANED", False), (2, "RUNNING", True)]

        _age_attempt(created.review_id, 2)
        gateway.make_stale(created.review_id, 2)
        failed = service.get_status(created.review_id, context=context)
        assert failed.status == ReviewStatus.FAILED
        assert failed.error is not None
        assert failed.error.code == "FRAMEWORK_RUN_ORPHANED"
        assert failed.error.retryable is True
        assert failed.framework_attempt_no == 2
        with psycopg.connect(DATABASE_URL) as conn:
            attempt_flags = conn.execute(
                """
                SELECT attempt_no, is_active
                FROM contract_framework_attempt
                WHERE review_id = %s
                ORDER BY attempt_no
                """,
                (created.review_id,),
            ).fetchall()
        assert attempt_flags == [(1, False), (2, False)]
    finally:
        _cleanup(tenant_id)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_recovery_attempt_is_claimed_by_stage_before_create_returns(tmp_path: Path) -> None:
    service, gateway, context, request, upload, tenant_id = _runtime(tmp_path)
    internal = ContractInternalService(
        service.repository,
        FrameworkCallbackRepository(service.settings),
    )
    immediate_results = []
    try:
        created = service.create_review(upload=upload, request=request, context=context)
        _age_attempt(created.review_id, 1)
        gateway.make_stale(created.review_id, 1)

        def execute_before_create_returns(execution: FrameworkExecutionRequest) -> None:
            snapshot = gateway.executions[(execution.review_id, execution.attempt_no)]
            stage_request = _stage_request(execution, snapshot)
            immediate_results.append(internal.execute_stage(stage_request))
            # A repeated exact request must observe the same active mapping.
            immediate_results.append(internal.execute_stage(stage_request))

        gateway.on_create = execute_before_create_returns
        recovered = service.get_status(created.review_id, context=context)

        assert recovered.status == ReviewStatus.RUNNING
        assert recovered.framework_attempt_no == 2
        assert len(immediate_results) == 2
        assert all(item.result_type == "PARSE_CONTRACT_STAGE_V1" for item in immediate_results)
        with psycopg.connect(DATABASE_URL) as conn:
            attempts = conn.execute(
                """
                SELECT attempt_no, status, framework_task_id, framework_run_id, is_active
                FROM contract_framework_attempt
                WHERE review_id = %s
                ORDER BY attempt_no
                """,
                (created.review_id,),
            ).fetchall()
        assert attempts == [
            (1, "ORPHANED", created.framework_task_id, created.framework_run_id, False),
            (
                2,
                "RUNNING",
                recovered.framework_task_id,
                recovered.framework_run_id,
                True,
            ),
        ]
        stale = FrameworkCallbackRepository(service.settings).process(
            RunFailedCallback(
                schema_version="1.0",
                review_id=created.review_id,
                attempt_no=1,
                framework_task_id=created.framework_task_id,
                framework_run_id=created.framework_run_id,
                event_sequence=1000,
                callback_id=f"late-attempt-1-{uuid.uuid4().hex}",
                callback_type="RUN_FAILED",
                stage_id="resolve_parties",
                result=None,
                error=ErrorData(
                    code="FRAMEWORK_RUN_FAILED",
                    message="late callback from orphaned Attempt 1",
                    retryable=True,
                    user_action_required=False,
                    details=None,
                ),
            )
        )
        assert stale.accepted is False
        assert stale.ignored_reason == "STALE_ATTEMPT"
        with psycopg.connect(DATABASE_URL) as conn:
            current = conn.execute(
                "SELECT status, active_attempt_no FROM contract_review_run WHERE id = %s",
                (created.review_id,),
            ).fetchone()
        assert current == ("RUNNING", 2)
    finally:
        _cleanup(tenant_id)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_result_query_triggers_orphan_recovery(tmp_path: Path) -> None:
    service, gateway, context, request, upload, tenant_id = _runtime(tmp_path)
    try:
        created = service.create_review(upload=upload, request=request, context=context)
        _age_attempt(created.review_id, 1)
        gateway.make_stale(created.review_id, 1)

        with pytest.raises(ContractError) as captured:
            service.get_result(created.review_id, context=context)

        assert captured.value.code == "REVIEW_NOT_READY"
        assert captured.value.retryable is True
        assert captured.value.details == {"current_status": "RUNNING"}
        recovered = service.get_status(created.review_id, context=context)
        assert recovered.framework_attempt_no == 2
        assert recovered.framework_run_id != created.framework_run_id
    finally:
        _cleanup(tenant_id)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_cancel_uses_two_transactions_and_is_idempotent(tmp_path: Path) -> None:
    service, gateway, context, request, upload, tenant_id = _runtime(tmp_path)
    try:
        created = service.create_review(upload=upload, request=request, context=context)
        cancelled = service.cancel_review(created.review_id, context=context)
        assert cancelled.status == ReviewStatus.CANCELLED
        assert cancelled.already_terminal is False
        assert gateway.cancelled_run_ids == [created.framework_run_id]

        repeated = service.cancel_review(created.review_id, context=context)
        assert repeated.status == ReviewStatus.CANCELLED
        assert repeated.already_terminal is True
        state = service.get_status(created.review_id, context=context)
        assert state.status == ReviewStatus.CANCELLED
    finally:
        _cleanup(tenant_id)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_cancel_after_recovery_waits_only_for_active_attempt(tmp_path: Path) -> None:
    service, gateway, context, request, upload, tenant_id = _runtime(tmp_path)
    try:
        created = service.create_review(upload=upload, request=request, context=context)
        _age_attempt(created.review_id, 1)
        gateway.make_stale(created.review_id, 1)
        recovered = service.get_status(created.review_id, context=context)
        assert recovered.framework_attempt_no == 2

        gateway.cancelled_run_ids.clear()
        cancelled = service.cancel_review(created.review_id, context=context)

        assert cancelled.status == ReviewStatus.CANCELLED
        assert gateway.cancelled_run_ids == [recovered.framework_run_id]
        with psycopg.connect(DATABASE_URL) as conn:
            attempts = conn.execute(
                """
                SELECT attempt_no, status, is_active
                FROM contract_framework_attempt
                WHERE review_id = %s
                ORDER BY attempt_no
                """,
                (created.review_id,),
            ).fetchall()
        assert attempts == [(1, "ORPHANED", False), (2, "CANCELLED", False)]
    finally:
        _cleanup(tenant_id)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_cancel_during_framework_create_prevents_attempt_activation(tmp_path: Path) -> None:
    service, gateway, context, request, upload, tenant_id = _runtime(tmp_path)
    internal = ContractInternalService(
        service.repository,
        FrameworkCallbackRepository(service.settings),
    )
    stage_errors: list[ContractError] = []

    def cancel_while_creating(execution: FrameworkExecutionRequest) -> None:
        plan = service.state_repository.begin_cancel(
            execution.review_id,
            tenant_id=execution.tenant_id,
            user_id=execution.user_id,
        )
        assert service.state_repository.finish_cancel(
            plan,
            tenant_id=execution.tenant_id,
            user_id=execution.user_id,
            snapshots=(),
        )
        snapshot = gateway.executions[(execution.review_id, execution.attempt_no)]
        try:
            internal.execute_stage(_stage_request(execution, snapshot))
        except ContractError as exc:
            stage_errors.append(exc)

    gateway.on_create = cancel_while_creating
    try:
        response = service.create_review(upload=upload, request=request, context=context)
        assert response.status == ReviewStatus.CREATED
        state = service.get_status(response.review_id, context=context)
        assert state.status == ReviewStatus.CANCELLED
        assert state.framework_attempt_no is None
        assert [error.code for error in stage_errors] == ["FRAMEWORK_CALLBACK_MISMATCH"]
        assert gateway.cancelled_run_ids == [f"framework-run-{response.review_id}-1"]
        with psycopg.connect(DATABASE_URL) as conn:
            attempt = conn.execute(
                """
                SELECT status, framework_task_id, framework_run_id, is_active
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = 1
                """,
                (response.review_id,),
            ).fetchone()
        assert attempt == ("CANCELLED", None, None, False)
    finally:
        _cleanup(tenant_id)


def _runtime(tmp_path: Path):
    run_migrations(Settings(database_url=DATABASE_URL))
    marker = uuid.uuid4().hex
    tenant_id = f"tenant-{marker}"
    settings = Settings(
        database_url=DATABASE_URL,
        data_dir=tmp_path,
        framework_stage_timeout_seconds=1,
        framework_recovery_grace_seconds=0,
    )
    gateway = FakeFrameworkGateway()
    service = _recreate_service(settings, gateway)
    context = InternalRequestContext(
        tenant_id=tenant_id,
        user_id=f"user-{marker}",
        request_id=f"request-{marker}",
        idempotency_key=f"idempotency-{marker}",
    )
    request = CreateReviewRequest(
        business_task_id=f"business-{marker}",
        contract_version_id=f"version-{marker}",
        perspective=Perspective.PARTY_B,
        our_party_name="某某单位",
        contract_type="AUTO",
        review_attitude="NEUTRAL",
        schema_version="1.0",
    )
    upload = UploadedContract(
        filename="contract.pdf",
        content_type="application/pdf",
        content=text_pdf_bytes("Party A supplies services. Party B pays."),
    )
    return service, gateway, context, request, upload, tenant_id


def _recreate_service(settings: Settings, gateway: FakeFrameworkGateway) -> RuntimeContractReviewService:
    repository = ContractRepository(settings)
    return RuntimeContractReviewService(
        settings,
        repository,
        ReviewStateRepository(settings),
        ContractDocumentProcessor(settings, repository),
        gateway,
    )


def _age_attempt(review_id: str, attempt_no: int) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            UPDATE contract_framework_attempt
            SET last_activity_at = now() - interval '10 minutes'
            WHERE review_id = %s AND attempt_no = %s
            """,
            (review_id, attempt_no),
        )
        conn.commit()


def _stage_request(
    execution: FrameworkExecutionRequest,
    snapshot: FrameworkRunSnapshot,
) -> StageExecuteRequest:
    return StageExecuteRequest(
        schema_version=execution.schema_version,
        review_id=execution.review_id,
        attempt_no=execution.attempt_no,
        framework_task_id=snapshot.task_id,
        framework_run_id=snapshot.run_id,
        stage_id="parse_contract",
        task_input=FrameworkTaskInput(
            schema_version=execution.schema_version,
            review_id=execution.review_id,
            attempt_no=execution.attempt_no,
            business_task_id=execution.business_task_id,
            contract_version_id=execution.contract_version_id,
            document_id=execution.document_id,
            perspective=execution.perspective,
            our_party_name=execution.our_party_name,
            contract_type=execution.contract_type,
            review_attitude=execution.review_attitude,
        ),
    )


def _cleanup(tenant_id: str) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("DELETE FROM contract_review_run WHERE tenant_id = %s", (tenant_id,))
        conn.execute("DELETE FROM contract_document WHERE tenant_id = %s", (tenant_id,))
        conn.commit()
