from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from contract.api.models import ReviewStatus
from contract.application.framework_gateway import FrameworkProtocolError
from contract.persistence.postgres.review_state import ReviewStateRepository
from test_runtime_reliability_integration import _cleanup, _runtime


DATABASE_URL = os.getenv("CONTRACT_TEST_DATABASE_URL", "")


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_create_and_status_are_durable_until_dispatcher_runs(tmp_path: Path) -> None:
    service, gateway, context, request, upload, tenant_id = _runtime(tmp_path)
    try:
        created = service.create_review(upload=upload, request=request, context=context)
        assert created.status == ReviewStatus.CREATED
        assert created.framework_attempt_no is None
        assert gateway.requests == []

        queried = service.get_status(created.review_id, context=context)
        assert queried.status == ReviewStatus.CREATED
        assert gateway.requests == []

        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute(
                """
                SELECT execution_status, dispatch_status, framework_task_id, framework_run_id
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = 1
                """,
                (created.review_id,),
            ).fetchone()
        assert row == ("PENDING", "PENDING_DISPATCH", None, None)

        assert service.dispatch_pending_attempts() == 1
        running = service.get_status(created.review_id, context=context)
        assert running.status == ReviewStatus.RUNNING
        assert running.framework_attempt_no == 1
        assert len(gateway.requests) == 1
    finally:
        _cleanup(tenant_id)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_dispatch_retry_reuses_the_frozen_framework_keys(tmp_path: Path) -> None:
    service, gateway, context, request, upload, tenant_id = _runtime(tmp_path)
    gateway.unavailable_creates = 1
    try:
        created = service.create_review(upload=upload, request=request, context=context)
        assert service.dispatch_pending_attempts() == 0
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                """
                UPDATE contract_framework_attempt
                SET next_dispatch_at = now()
                WHERE review_id = %s AND attempt_no = 1
                """,
                (created.review_id,),
            )
            conn.commit()

        assert service.dispatch_pending_attempts() == 1
        assert len(gateway.requests) == 2
        assert gateway.requests[0].task_idempotency_key == gateway.requests[1].task_idempotency_key
        assert gateway.requests[0].run_idempotency_key == gateway.requests[1].run_idempotency_key
    finally:
        _cleanup(tenant_id)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_non_retryable_dispatch_failure_becomes_terminal(tmp_path: Path) -> None:
    service, gateway, context, request, upload, tenant_id = _runtime(tmp_path)

    def reject_create(_request):
        raise FrameworkProtocolError("idempotency conflict")

    gateway.create_execution = reject_create
    try:
        created = service.create_review(upload=upload, request=request, context=context)

        assert service.dispatch_pending_attempts() == 0
        with psycopg.connect(DATABASE_URL) as conn:
            attempt = conn.execute(
                """
                SELECT execution_status, dispatch_status, lease_owner, lease_until
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = 1
                """,
                (created.review_id,),
            ).fetchone()
            review = conn.execute(
                "SELECT status, error_code FROM contract_review_run WHERE id = %s",
                (created.review_id,),
            ).fetchone()

        assert attempt == ("FAILED", "DISPATCH_FAILED", None, None)
        assert review == ("FAILED", "FRAMEWORK_PROTOCOL_ERROR")
    finally:
        _cleanup(tenant_id)

@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_two_dispatchers_cannot_claim_one_attempt(tmp_path: Path) -> None:
    service, _gateway, context, request, upload, tenant_id = _runtime(tmp_path)
    try:
        created = service.create_review(upload=upload, request=request, context=context)
        repository = ReviewStateRepository(service.settings)

        def claim(owner: str):
            return repository.claim_pending_attempts(
                owner=owner,
                limit=1,
                lease_seconds=120,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(claim, ("dispatcher-a", "dispatcher-b"))
        assert sorted((len(first), len(second))) == [0, 1]
        claimed = first[0] if first else second[0]
        assert claimed.review_id == created.review_id
        assert claimed.model_pack_id == "api-rerank"
        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute(
                """
                SELECT dispatch_status, lease_owner, lease_version, dispatch_attempts
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = 1
                """,
                (created.review_id,),
            ).fetchone()
        assert row[0] == "CLAIMED"
        assert row[1] == claimed.lease_owner
        assert row[2] == claimed.lease_version
        assert row[3] == 1
    finally:
        _cleanup(tenant_id)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_expired_dispatcher_cannot_cancel_a_new_owner_run(tmp_path: Path) -> None:
    service, gateway, context, request, upload, tenant_id = _runtime(tmp_path)
    try:
        service.create_review(upload=upload, request=request, context=context)
        repository = ReviewStateRepository(service.settings)
        reservation = repository.claim_pending_attempts(
            owner="dispatcher-old",
            limit=1,
            lease_seconds=120,
        )[0]
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                """
                UPDATE contract_framework_attempt
                SET lease_owner = 'dispatcher-new',
                    lease_version = lease_version + 1,
                    lease_until = now() + interval '120 seconds'
                WHERE review_id = %s AND attempt_no = %s
                """,
                (reservation.review_id, reservation.attempt_no),
            )
            conn.commit()

        assert service.dispatch_claimed_attempt(reservation) is False
        assert gateway.cancelled_run_ids == []
        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute(
                """
                SELECT dispatch_status, framework_task_id, framework_run_id
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = %s
                """,
                (reservation.review_id, reservation.attempt_no),
            ).fetchone()
        assert row == ("CLAIMED", None, None)
    finally:
        _cleanup(tenant_id)


@pytest.mark.skipif(not DATABASE_URL, reason="CONTRACT_TEST_DATABASE_URL is not configured")
def test_status_query_does_not_repair_or_mutate_attempt(tmp_path: Path) -> None:
    service, _gateway, context, request, upload, tenant_id = _runtime(tmp_path)
    try:
        created = service.create_review(upload=upload, request=request, context=context)
        with psycopg.connect(DATABASE_URL) as conn:
            before = conn.execute(
                """
                SELECT execution_status, dispatch_status, dispatch_attempts, lease_owner
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = 1
                """,
                (created.review_id,),
            ).fetchone()
        status = service.get_status(created.review_id, context=context)
        assert status.status == ReviewStatus.CREATED
        with psycopg.connect(DATABASE_URL) as conn:
            after = conn.execute(
                """
                SELECT execution_status, dispatch_status, dispatch_attempts, lease_owner
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = 1
                """,
                (created.review_id,),
            ).fetchone()
        assert after == before
    finally:
        _cleanup(tenant_id)
