from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from contract.api.models import CreateReviewRequest, Perspective, ReviewStatus
from contract.application.document_processing import ContractDocumentProcessor
from contract.application.framework_gateway import (
    FrameworkExecutionRequest,
    FrameworkRunSnapshot,
    FrameworkUnavailableError,
)
from contract.application.ports import InternalRequestContext, UploadedContract
from contract.application.runtime_service import RuntimeContractReviewService
from contract.config import Settings
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

    def get_run(self, task_id: str, run_id: str) -> FrameworkRunSnapshot:
        return next(value for value in self.executions.values() if value.run_id == run_id)

    def cancel_run(self, task_id: str, run_id: str) -> FrameworkRunSnapshot:
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

        first_reservation = service.state_repository.reserve_initial_attempt(
            created.review_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        second_reservation = service.state_repository.reserve_initial_attempt(
            created.review_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        assert first_reservation is not None
        assert second_reservation == first_reservation

        restarted_service = _recreate_service(service.settings, gateway)
        assert restarted_service._dispatch(first_reservation)
        assert restarted_service._dispatch(second_reservation)
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
def test_cancel_during_framework_create_prevents_attempt_activation(tmp_path: Path) -> None:
    service, gateway, context, request, upload, tenant_id = _runtime(tmp_path)

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

    gateway.on_create = cancel_while_creating
    try:
        response = service.create_review(upload=upload, request=request, context=context)
        assert response.status == ReviewStatus.CREATED
        state = service.get_status(response.review_id, context=context)
        assert state.status == ReviewStatus.CANCELLED
        assert state.framework_attempt_no is None
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


def _cleanup(tenant_id: str) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("DELETE FROM contract_review_run WHERE tenant_id = %s", (tenant_id,))
        conn.execute("DELETE FROM contract_document WHERE tenant_id = %s", (tenant_id,))
        conn.commit()
