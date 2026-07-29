from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from contract.application.idempotency import (
    build_dispatch_response_fingerprint,
    build_framework_request_fingerprint,
)
from contract.application.framework_gateway import FrameworkRunSnapshot
from contract.config import Settings
from contract.errors import ConfigurationError, ContractError
from contract.persistence.postgres.connections import open_contract_database_connection

from psycopg.rows import dict_row


TERMINAL_REVIEW_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
TERMINAL_ATTEMPT_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "ORPHANED"})
REVIEW_STAGES = frozenset(
    {
        "PARSING",
        "PARTY_RESOLUTION",
        "IR_EXTRACTION",
        "RIGHTS_OBLIGATIONS",
        "RISK_REVIEW",
        "EVIDENCE_VERIFICATION",
        "FINALIZING",
    }
)


@dataclass(frozen=True, slots=True)
class AttemptReservation:
    review_id: str
    attempt_no: int
    tenant_id: str
    user_id: str
    expected_version: int
    business_task_id: str
    contract_version_id: str
    model_pack_id: str
    document_id: str
    perspective: str
    our_party_name: str | None
    contract_type: str
    review_attitude: str
    schema_version: str
    request_fingerprint: str = ""
    previous_task_id: str | None = None
    previous_run_id: str | None = None
    lease_owner: str | None = None
    lease_version: int = 0


@dataclass(frozen=True, slots=True)
class AttemptMapping:
    attempt_no: int
    task_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class CancellationPlan:
    review_id: str
    expected_version: int
    mappings: tuple[AttemptMapping, ...]
    already_cancelled: bool = False


class ReviewStateRepository:
    """Transactional state changes for review and Framework Attempt records."""

    def __init__(self, settings: Settings) -> None:
        if not settings.database_url:
            raise ConfigurationError("CONTRACT_DATABASE_URL is required")
        self.settings = settings
        self.database_url = settings.database_url

    def connect(self):
        return open_contract_database_connection(self.settings, row_factory=dict_row)

    def find_for_request(
        self,
        *,
        tenant_id: str,
        user_id: str,
        business_task_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM contract_review_run
                WHERE tenant_id = %s
                  AND (
                    business_task_id = %s
                    OR (user_id = %s AND idempotency_key = %s)
                  )
                """,
                (tenant_id, business_task_id, user_id, idempotency_key),
            ).fetchall()
        review_ids = {row["id"] for row in rows}
        if not rows:
            return None
        if len(review_ids) != 1 or rows[0]["request_fingerprint"] != request_fingerprint:
            raise self._idempotency_conflict(rows[0]["id"] if len(review_ids) == 1 else None)
        return self.get_state(rows[0]["id"], tenant_id=tenant_id, user_id=user_id)

    def get_state(self, review_id: str, *, tenant_id: str, user_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            review = self._owned_review(conn, review_id, tenant_id=tenant_id, user_id=user_id)
            attempt = None
            if review["active_attempt_no"] is not None:
                attempt = conn.execute(
                    """
                    SELECT *
                    FROM contract_framework_attempt
                    WHERE review_id = %s AND attempt_no = %s
                    """,
                    (review_id, review["active_attempt_no"]),
                ).fetchone()
            pending = conn.execute(
                """
                SELECT *
                FROM contract_framework_attempt
                WHERE review_id = %s
                  AND is_active = false
                  AND execution_status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'ORPHANED')
                  AND dispatch_status IN (
                    'PENDING_DISPATCH', 'CLAIMED', 'RETRY_WAIT', 'DISPATCH_FAILED'
                  )
                ORDER BY attempt_no DESC
                LIMIT 1
                """,
                (review_id,),
            ).fetchone()
        state = dict(review)
        state["active_attempt"] = dict(attempt) if attempt else None
        state["pending_attempt"] = dict(pending) if pending else None
        return state

    def list_nonterminal_reviews(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, tenant_id, user_id
                FROM contract_review_run
                WHERE status IN ('CREATED', 'RUNNING')
                  AND cancel_requested = false
                ORDER BY created_at, id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def ensure_initial_attempt(
        self,
        review_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        """Create the durable first Attempt without making a Framework call."""
        with self.connect() as conn:
            review = self._lock_owned_review(conn, review_id, tenant_id=tenant_id, user_id=user_id)
            if (
                review["status"] in TERMINAL_REVIEW_STATUSES
                or review["cancel_requested"]
                or review["active_attempt_no"] is not None
            ):
                conn.commit()
                return False
            existing = conn.execute(
                """
                SELECT attempt_no
                FROM contract_framework_attempt
                WHERE review_id = %s
                ORDER BY attempt_no DESC
                LIMIT 1
                FOR UPDATE
                """,
                (review_id,),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return False
            fingerprint = self._framework_request_fingerprint(review, 1)
            conn.execute(
                """
                INSERT INTO contract_framework_attempt (
                  review_id, attempt_no, tenant_id, status, execution_status,
                  dispatch_status, request_fingerprint, next_dispatch_at, is_active
                ) VALUES (%s, 1, %s, 'PENDING', 'PENDING',
                          'PENDING_DISPATCH', %s, now(), false)
                """,
                (review_id, tenant_id, fingerprint),
            )
            conn.commit()
        return True

    def reconcile_nonterminal_attempts(self) -> int:
        """Repair a Review/Attempt gap left by a process crash."""
        repaired = 0
        for review in self.list_nonterminal_reviews():
            try:
                if self.ensure_initial_attempt(
                    review["id"],
                    tenant_id=review["tenant_id"],
                    user_id=review["user_id"],
                ):
                    repaired += 1
            except ContractError as exc:
                if exc.code != "REVIEW_NOT_FOUND":
                    raise
        return repaired

    def reconcile_expired_dispatch_leases(self) -> int:
        with self.connect() as conn:
            rows = conn.execute(
                """
                UPDATE contract_framework_attempt
                SET dispatch_status = 'RETRY_WAIT',
                    next_dispatch_at = now(),
                    lease_owner = NULL,
                    lease_until = NULL,
                    last_heartbeat_at = now()
                WHERE dispatch_status = 'CLAIMED'
                  AND lease_until IS NOT NULL
                  AND lease_until <= now()
                  AND framework_task_id IS NULL
                RETURNING review_id, attempt_no
                """
            ).fetchall()
            conn.commit()
        return len(rows)

    def claim_pending_attempts(
        self,
        *,
        owner: str,
        limit: int,
        lease_seconds: int,
    ) -> list[AttemptReservation]:
        if limit <= 0:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  review.id AS review_id,
                  review.tenant_id AS review_tenant_id,
                  review.user_id AS review_user_id,
                  review.version AS review_version,
                  review.active_attempt_no,
                  review.business_task_id,
                  review.contract_version_id,
                  review.model_pack_id,
                  review.document_id,
                  review.perspective,
                  review.our_party_name,
                  review.contract_type,
                  review.review_attitude,
                  review.schema_version,
                  attempt.attempt_no,
                  attempt.framework_task_id,
                  attempt.framework_run_id,
                  attempt.request_fingerprint,
                  attempt.lease_version,
                  (
                    SELECT previous.framework_task_id
                    FROM contract_framework_attempt previous
                    WHERE previous.review_id = attempt.review_id
                      AND previous.attempt_no = review.active_attempt_no
                  ) AS previous_task_id,
                  (
                    SELECT previous.framework_run_id
                    FROM contract_framework_attempt previous
                    WHERE previous.review_id = attempt.review_id
                      AND previous.attempt_no = review.active_attempt_no
                  ) AS previous_run_id
                FROM contract_framework_attempt attempt
                JOIN contract_review_run review ON review.id = attempt.review_id
                WHERE review.status IN ('CREATED', 'RUNNING')
                  AND review.cancel_requested = false
                  AND attempt.execution_status = 'PENDING'
                  AND attempt.dispatch_status IN ('PENDING_DISPATCH', 'RETRY_WAIT')
                  AND attempt.next_dispatch_at <= now()
                  AND (attempt.lease_until IS NULL OR attempt.lease_until <= now())
                ORDER BY attempt.next_dispatch_at, attempt.created_at, attempt.review_id, attempt.attempt_no
                FOR UPDATE OF attempt SKIP LOCKED
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
            claimed: list[AttemptReservation] = []
            for row in rows:
                updated = conn.execute(
                    """
                    UPDATE contract_framework_attempt
                    SET status = 'PENDING',
                        execution_status = 'PENDING',
                        dispatch_status = 'CLAIMED',
                        dispatch_attempts = dispatch_attempts + 1,
                        lease_owner = %s,
                        lease_until = now() + (%s * interval '1 second'),
                        lease_version = lease_version + 1,
                        last_heartbeat_at = now()
                    WHERE review_id = %s AND attempt_no = %s
                    RETURNING lease_version
                    """,
                    (owner, lease_seconds, row["review_id"], row["attempt_no"]),
                ).fetchone()
                claimed.append(
                    self._reservation(
                        {
                            "id": row["review_id"],
                            "tenant_id": row["review_tenant_id"],
                            "user_id": row["review_user_id"],
                            "version": row["review_version"],
                            "business_task_id": row["business_task_id"],
                            "contract_version_id": row["contract_version_id"],
                            "model_pack_id": row["model_pack_id"],
                            "document_id": row["document_id"],
                            "perspective": row["perspective"],
                            "our_party_name": row["our_party_name"],
                            "contract_type": row["contract_type"],
                            "review_attitude": row["review_attitude"],
                            "schema_version": row["schema_version"],
                        },
                        row["attempt_no"],
                        attempt={
                            "framework_task_id": row["framework_task_id"],
                            "framework_run_id": row["framework_run_id"],
                            "request_fingerprint": row["request_fingerprint"],
                            "lease_owner": owner,
                            "lease_version": updated["lease_version"],
                        },
                        previous=(
                            {"framework_task_id": row["previous_task_id"], "framework_run_id": row["previous_run_id"]}
                            if row["previous_task_id"] and row["previous_run_id"]
                            else None
                        ),
                    )
                )
            conn.commit()
        return claimed

    def mark_dispatch_sent(
        self,
        reservation: AttemptReservation,
        snapshot: FrameworkRunSnapshot,
        *,
        response_fingerprint: str | None = None,
    ) -> bool:
        response_fingerprint = response_fingerprint or build_dispatch_response_fingerprint(
            review_id=reservation.review_id,
            attempt_no=reservation.attempt_no,
            tenant_id=reservation.tenant_id,
            framework_task_id=snapshot.task_id,
            framework_run_id=snapshot.run_id,
        )
        stage = self._stage(snapshot.current_stage_id)
        execution_status = (
            "RUNNING"
            if snapshot.status in {"running", "paused", "waiting_human"}
            else "PENDING"
        )
        with self.connect() as conn:
            attempt = conn.execute(
                """
                SELECT *
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = %s
                FOR UPDATE
                """,
                (reservation.review_id, reservation.attempt_no),
            ).fetchone()
            if attempt is None:
                conn.commit()
                return False
            if attempt["dispatch_status"] == "SENT":
                if (
                    attempt["framework_task_id"] != snapshot.task_id
                    or attempt["framework_run_id"] != snapshot.run_id
                ):
                    conn.commit()
                    return False
                conn.execute(
                    """
                    UPDATE contract_framework_attempt
                    SET dispatch_response_fingerprint = COALESCE(
                          dispatch_response_fingerprint, %s
                        ),
                        lease_owner = NULL, lease_until = NULL,
                        last_heartbeat_at = now()
                    WHERE review_id = %s AND attempt_no = %s
                    """,
                    (response_fingerprint, reservation.review_id, reservation.attempt_no),
                )
                conn.commit()
                return True
            if (
                attempt["dispatch_status"] == "DISPATCH_FAILED"
                and attempt["execution_status"] == "CANCELLED"
                and attempt["framework_task_id"] is None
                and attempt["framework_run_id"] is None
            ):
                conn.execute(
                    """
                    UPDATE contract_framework_attempt
                    SET dispatch_status = 'SENT',
                        framework_task_id = %s, framework_run_id = %s,
                        dispatch_response_fingerprint = %s,
                        lease_owner = NULL, lease_until = NULL,
                        last_heartbeat_at = now()
                    WHERE review_id = %s AND attempt_no = %s
                    """,
                    (
                        snapshot.task_id,
                        snapshot.run_id,
                        response_fingerprint,
                        reservation.review_id,
                        reservation.attempt_no,
                    ),
                )
                conn.commit()
                return False
            if (
                attempt["dispatch_status"] != "CLAIMED"
                or attempt["lease_owner"] != reservation.lease_owner
                or attempt["lease_version"] != reservation.lease_version
            ):
                conn.commit()
                return False
            review = conn.execute(
                """
                SELECT *
                FROM contract_review_run
                WHERE id = %s
                FOR UPDATE
                """,
                (reservation.review_id,),
            ).fetchone()
            if review is None or review["cancel_requested"] or review["status"] in TERMINAL_REVIEW_STATUSES:
                conn.execute(
                    """
                    UPDATE contract_framework_attempt
                    SET dispatch_status = 'SENT',
                        framework_task_id = %s, framework_run_id = %s,
                        dispatch_response_fingerprint = %s,
                        lease_owner = NULL, lease_until = NULL,
                        last_heartbeat_at = now()
                    WHERE review_id = %s AND attempt_no = %s
                    """,
                    (
                        snapshot.task_id,
                        snapshot.run_id,
                        response_fingerprint,
                        reservation.review_id,
                        reservation.attempt_no,
                    ),
                )
                conn.commit()
                return False
            conn.execute(
                """
                UPDATE contract_framework_attempt
                SET framework_task_id = %s, framework_run_id = %s,
                    dispatch_status = 'SENT',
                    dispatch_response_fingerprint = %s,
                    status = %s, execution_status = %s,
                    current_stage = %s,
                    last_activity_at = %s,
                    started_at = CASE WHEN %s = 'RUNNING'
                                     THEN COALESCE(started_at, now())
                                     ELSE started_at END,
                    is_active = %s,
                    lease_owner = NULL, lease_until = NULL,
                    last_heartbeat_at = now()
                WHERE review_id = %s AND attempt_no = %s
                """,
                (
                    snapshot.task_id,
                    snapshot.run_id,
                    response_fingerprint,
                    execution_status,
                    execution_status,
                    stage,
                    snapshot.updated_at,
                    execution_status,
                    execution_status == "RUNNING",
                    reservation.review_id,
                    reservation.attempt_no,
                ),
            )
            if execution_status == "RUNNING":
                conn.execute(
                    """
                    UPDATE contract_review_run
                    SET status = 'RUNNING', current_stage = %s,
                        active_attempt_no = %s,
                        started_at = COALESCE(started_at, now()),
                        version = version + 1
                    WHERE id = %s AND status IN ('CREATED', 'RUNNING')
                    """,
                    (stage, reservation.attempt_no, reservation.review_id),
                )
            conn.commit()
        return True

    def should_cancel_unaccepted_dispatch(
        self,
        reservation: AttemptReservation,
        snapshot: FrameworkRunSnapshot,
    ) -> bool:
        """Cancel only a remote Run explicitly owned by a cancelled local Attempt."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT attempt.dispatch_status, attempt.execution_status,
                       attempt.framework_task_id, attempt.framework_run_id,
                       review.status, review.cancel_requested
                FROM contract_framework_attempt attempt
                JOIN contract_review_run review ON review.id = attempt.review_id
                WHERE attempt.review_id = %s AND attempt.attempt_no = %s
                """,
                (reservation.review_id, reservation.attempt_no),
            ).fetchone()
        if row is None:
            return False
        if (
            row["dispatch_status"] != "SENT"
            or row["framework_task_id"] != snapshot.task_id
            or row["framework_run_id"] != snapshot.run_id
        ):
            return False
        return bool(
            row["cancel_requested"]
            or row["execution_status"] == "CANCELLED"
            or row["status"] in TERMINAL_REVIEW_STATUSES
        )

    def mark_dispatch_retry(
        self,
        reservation: AttemptReservation,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_delay_seconds: int,
    ) -> bool:
        with self.connect() as conn:
            attempt = conn.execute(
                """
                SELECT *
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = %s
                FOR UPDATE
                """,
                (reservation.review_id, reservation.attempt_no),
            ).fetchone()
            if (
                attempt is None
                or attempt["dispatch_status"] != "CLAIMED"
                or attempt["lease_owner"] != reservation.lease_owner
                or attempt["lease_version"] != reservation.lease_version
            ):
                conn.commit()
                return False
            if retryable:
                conn.execute(
                    """
                    UPDATE contract_framework_attempt
                    SET dispatch_status = 'RETRY_WAIT',
                        next_dispatch_at = now() + (%s * interval '1 second'),
                        lease_owner = NULL, lease_until = NULL,
                        last_heartbeat_at = now()
                    WHERE review_id = %s AND attempt_no = %s
                    """,
                    (max(1, retry_delay_seconds), reservation.review_id, reservation.attempt_no),
                )
            else:
                conn.execute(
                    """
                    UPDATE contract_framework_attempt
                    SET dispatch_status = 'DISPATCH_FAILED',
                        status = 'FAILED', execution_status = 'FAILED',
                        is_active = false, finished_at = now(),
                        lease_owner = NULL, lease_until = NULL,
                        last_heartbeat_at = now()
                    WHERE review_id = %s AND attempt_no = %s
                    """,
                    (reservation.review_id, reservation.attempt_no),
                )
                conn.execute(
                    """
                    UPDATE contract_review_run
                    SET status = 'FAILED', current_stage = NULL,
                        active_attempt_no = NULL, error_code = %s,
                        error_message = %s, retryable = false,
                        completed_at = now(), version = version + 1
                    WHERE id = %s AND status IN ('CREATED', 'RUNNING')
                    """,
                    (error_code, error_message, reservation.review_id),
                )
            conn.commit()
        return True

    def list_active_attempts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, tenant_id, user_id
                FROM contract_review_run
                WHERE status = 'RUNNING'
                  AND cancel_requested = false
                  AND active_attempt_no IS NOT NULL
                ORDER BY updated_at, id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def reserve_initial_attempt(
        self,
        review_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> AttemptReservation | None:
        with self.connect() as conn:
            review = self._lock_owned_review(conn, review_id, tenant_id=tenant_id, user_id=user_id)
            if review["status"] in TERMINAL_REVIEW_STATUSES or review["cancel_requested"]:
                conn.commit()
                return None
            pending = conn.execute(
                """
                SELECT attempt_no
                FROM contract_framework_attempt
                WHERE review_id = %s AND status = 'PENDING'
                ORDER BY attempt_no DESC
                LIMIT 1
                FOR UPDATE
                """,
                (review_id,),
            ).fetchone()
            if pending is not None:
                previous = None
                if (
                    review["active_attempt_no"] is not None
                    and pending["attempt_no"] > review["active_attempt_no"]
                ):
                    previous = conn.execute(
                        """
                        SELECT *
                        FROM contract_framework_attempt
                        WHERE review_id = %s AND attempt_no = %s
                        FOR UPDATE
                        """,
                        (review_id, review["active_attempt_no"]),
                    ).fetchone()
                reservation = self._reservation(review, pending["attempt_no"], previous=previous)
                conn.commit()
                return reservation
            if review["active_attempt_no"] is not None or review["status"] != "CREATED":
                conn.commit()
                return None
            conn.execute(
                """
                INSERT INTO contract_framework_attempt (
                  review_id, attempt_no, tenant_id, model_pack_id,
                  status, execution_status,
                  dispatch_status, request_fingerprint, next_dispatch_at, is_active
                ) VALUES (%s, 1, %s, %s, 'PENDING', 'PENDING',
                          'PENDING_DISPATCH', %s, now(), false)
                """,
                (
                    review_id,
                    tenant_id,
                    review["model_pack_id"],
                    self._framework_request_fingerprint(review, 1),
                ),
            )
            reservation = self._reservation(review, 1)
            conn.commit()
        return reservation

    def activate_attempt(
        self,
        reservation: AttemptReservation,
        snapshot: FrameworkRunSnapshot,
    ) -> bool:
        stage = self._stage(snapshot.current_stage_id)
        with self.connect() as conn:
            review = self._lock_owned_review(
                conn,
                reservation.review_id,
                tenant_id=reservation.tenant_id,
                user_id=reservation.user_id,
            )
            attempt = conn.execute(
                """
                SELECT *
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = %s
                FOR UPDATE
                """,
                (reservation.review_id, reservation.attempt_no),
            ).fetchone()
            if (
                review["status"] == "RUNNING"
                and review["active_attempt_no"] == reservation.attempt_no
                and attempt is not None
                and attempt["status"] == "RUNNING"
                and attempt["framework_task_id"] == snapshot.task_id
                and attempt["framework_run_id"] == snapshot.run_id
            ):
                conn.commit()
                return True
            if (
                review["version"] != reservation.expected_version
                or review["status"] not in {"CREATED", "RUNNING"}
                or review["cancel_requested"]
                or attempt is None
                or attempt["status"] != "PENDING"
                or (review["active_attempt_no"] or 0) >= reservation.attempt_no
            ):
                conn.commit()
                return False
            conn.execute(
                """
                UPDATE contract_framework_attempt
                SET is_active = false
                WHERE review_id = %s AND is_active
                """,
                (reservation.review_id,),
            )
            conn.execute(
                """
                UPDATE contract_framework_attempt
                SET framework_task_id = %s, framework_run_id = %s,
                    status = 'RUNNING', current_stage = %s,
                    last_activity_at = %s, started_at = COALESCE(started_at, now()),
                    is_active = true
                WHERE review_id = %s AND attempt_no = %s
                """,
                (
                    snapshot.task_id,
                    snapshot.run_id,
                    stage,
                    snapshot.updated_at,
                    reservation.review_id,
                    reservation.attempt_no,
                ),
            )
            conn.execute(
                """
                UPDATE contract_review_run
                SET status = 'RUNNING', current_stage = %s,
                    active_attempt_no = %s, started_at = COALESCE(started_at, now()),
                    version = version + 1
                WHERE id = %s
                """,
                (stage, reservation.attempt_no, reservation.review_id),
            )
            conn.commit()
        return True

    def mark_dispatch_failed(
        self,
        reservation: AttemptReservation,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> bool:
        with self.connect() as conn:
            review = self._lock_owned_review(
                conn,
                reservation.review_id,
                tenant_id=reservation.tenant_id,
                user_id=reservation.user_id,
            )
            attempt = conn.execute(
                """
                SELECT status
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = %s
                FOR UPDATE
                """,
                (reservation.review_id, reservation.attempt_no),
            ).fetchone()
            if (
                review["version"] != reservation.expected_version
                or review["status"] not in {"CREATED", "RUNNING"}
                or attempt is None
                or attempt["status"] != "PENDING"
            ):
                conn.commit()
                return False
            conn.execute(
                """
                UPDATE contract_framework_attempt
                SET status = 'FAILED', is_active = false, finished_at = now()
                WHERE review_id = %s AND attempt_no = %s
                """,
                (reservation.review_id, reservation.attempt_no),
            )
            conn.execute(
                """
                UPDATE contract_review_run
                SET status = 'FAILED', current_stage = NULL,
                    active_attempt_no = NULL, error_code = %s, error_message = %s,
                    retryable = %s, completed_at = now(), version = version + 1
                WHERE id = %s
                """,
                (error_code, error_message, retryable, reservation.review_id),
            )
            conn.commit()
        return True

    def record_run_snapshot(
        self,
        *,
        review_id: str,
        tenant_id: str,
        user_id: str,
        attempt_no: int,
        snapshot: FrameworkRunSnapshot,
    ) -> bool:
        if snapshot.terminal:
            return False
        stage = self._stage(snapshot.current_stage_id)
        with self.connect() as conn:
            review = self._lock_owned_review(conn, review_id, tenant_id=tenant_id, user_id=user_id)
            attempt = conn.execute(
                """
                SELECT framework_task_id, framework_run_id, status
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = %s
                FOR UPDATE
                """,
                (review_id, attempt_no),
            ).fetchone()
            if (
                review["status"] != "RUNNING"
                or review["active_attempt_no"] != attempt_no
                or attempt is None
                or attempt["framework_task_id"] != snapshot.task_id
                or attempt["framework_run_id"] != snapshot.run_id
                or attempt["status"] != "RUNNING"
            ):
                conn.commit()
                return False
            conn.execute(
                """
                UPDATE contract_framework_attempt
                SET current_stage = %s,
                    last_activity_at = GREATEST(COALESCE(last_activity_at, %s), %s)
                WHERE review_id = %s AND attempt_no = %s
                """,
                (stage, snapshot.updated_at, snapshot.updated_at, review_id, attempt_no),
            )
            conn.execute(
                """
                UPDATE contract_review_run
                SET current_stage = %s
                WHERE id = %s
                """,
                (stage, review_id),
            )
            conn.commit()
        return True

    def apply_framework_terminal(
        self,
        *,
        review_id: str,
        tenant_id: str,
        user_id: str,
        attempt_no: int,
        snapshot: FrameworkRunSnapshot,
    ) -> bool:
        if snapshot.status not in {"failed", "cancelled"}:
            return False
        with self.connect() as conn:
            review = self._lock_owned_review(conn, review_id, tenant_id=tenant_id, user_id=user_id)
            attempt = conn.execute(
                """
                SELECT *
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = %s
                FOR UPDATE
                """,
                (review_id, attempt_no),
            ).fetchone()
            if (
                review["status"] not in {"CREATED", "RUNNING"}
                or review["active_attempt_no"] != attempt_no
                or attempt is None
                or attempt["framework_task_id"] != snapshot.task_id
                or attempt["framework_run_id"] != snapshot.run_id
            ):
                conn.commit()
                return False
            if snapshot.status == "cancelled":
                review_status = "CANCELLED"
                attempt_status = "CANCELLED"
                error_code = None
                error_message = None
                retryable = False
            else:
                review_status = "FAILED"
                attempt_status = "FAILED"
                error_code = "FRAMEWORK_RUN_FAILED"
                error_message = "Framework Run执行失败"
                retryable = True
            conn.execute(
                """
                UPDATE contract_framework_attempt
                SET status = %s, current_stage = %s, is_active = false,
                    last_activity_at = %s, finished_at = now()
                WHERE review_id = %s AND attempt_no = %s
                """,
                (
                    attempt_status,
                    self._stage(snapshot.current_stage_id),
                    snapshot.updated_at,
                    review_id,
                    attempt_no,
                ),
            )
            conn.execute(
                """
                UPDATE contract_review_run
                SET status = %s, current_stage = NULL,
                    error_code = %s, error_message = %s, retryable = %s,
                    completed_at = now(), version = version + 1
                WHERE id = %s
                """,
                (review_status, error_code, error_message, retryable, review_id),
            )
            conn.commit()
        return True

    def get_result_json(
        self,
        review_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            self._owned_review(conn, review_id, tenant_id=tenant_id, user_id=user_id)
            row = conn.execute(
                "SELECT result_json FROM contract_review_result WHERE review_id = %s",
                (review_id,),
            ).fetchone()
        return row["result_json"] if row else None

    def prepare_recovery(
        self,
        *,
        review_id: str,
        tenant_id: str,
        user_id: str,
        attempt_no: int,
        stale_before: datetime,
    ) -> AttemptReservation | None:
        with self.connect() as conn:
            review = self._lock_owned_review(conn, review_id, tenant_id=tenant_id, user_id=user_id)
            current = conn.execute(
                """
                SELECT *
                FROM contract_framework_attempt
                WHERE review_id = %s AND attempt_no = %s
                FOR UPDATE
                """,
                (review_id, attempt_no),
            ).fetchone()
            pending = conn.execute(
                """
                SELECT attempt_no
                FROM contract_framework_attempt
                WHERE review_id = %s AND status = 'PENDING'
                ORDER BY attempt_no DESC
                LIMIT 1
                FOR UPDATE
                """,
                (review_id,),
            ).fetchone()
            if (
                pending is not None
                and pending["attempt_no"] > attempt_no
                and review["status"] == "RUNNING"
                and not review["cancel_requested"]
                and review["active_attempt_no"] == attempt_no
            ):
                reservation = self._reservation(
                    review,
                    pending["attempt_no"],
                    previous=current,
                )
                conn.commit()
                return reservation
            if (
                review["status"] != "RUNNING"
                or review["cancel_requested"]
                or review["active_attempt_no"] != attempt_no
                or current is None
                or current["status"] != "RUNNING"
                or current["framework_task_id"] is None
                or current["framework_run_id"] is None
            ):
                conn.commit()
                return None
            activity_at = current["last_activity_at"] or current["updated_at"] or current["started_at"]
            if activity_at is None or activity_at > stale_before:
                conn.commit()
                return None
            if attempt_no >= 2:
                conn.execute(
                    """
                    UPDATE contract_framework_attempt
                    SET status = 'ORPHANED', orphaned_at = now(),
                        orphan_reason = 'FRAMEWORK_RUN_ORPHANED', finished_at = now(),
                        is_active = false
                    WHERE review_id = %s AND attempt_no = %s
                    """,
                    (review_id, attempt_no),
                )
                conn.execute(
                    """
                    UPDATE contract_review_run
                    SET status = 'FAILED', current_stage = NULL,
                        error_code = 'FRAMEWORK_RUN_ORPHANED',
                        error_message = 'Framework Run重启后失联且自动恢复次数已耗尽',
                        retryable = true, completed_at = now(), version = version + 1
                    WHERE id = %s
                    """,
                    (review_id,),
                )
                conn.commit()
                return None
            conn.execute(
                """
                UPDATE contract_framework_attempt
                SET status = 'ORPHANED', orphaned_at = now(),
                    orphan_reason = 'FRAMEWORK_RUN_ORPHANED', finished_at = now(),
                    is_active = false
                WHERE review_id = %s AND attempt_no = %s
                """,
                (review_id, attempt_no),
            )
            next_attempt_no = attempt_no + 1
            conn.execute(
                """
                INSERT INTO contract_framework_attempt (
                  review_id, attempt_no, tenant_id, model_pack_id,
                  status, execution_status,
                  dispatch_status, request_fingerprint, next_dispatch_at, is_active
                ) VALUES (%s, %s, %s, %s, 'PENDING', 'PENDING',
                          'PENDING_DISPATCH', %s, now(), false)
                """,
                (
                    review_id,
                    next_attempt_no,
                    tenant_id,
                    review["model_pack_id"],
                    self._framework_request_fingerprint(review, next_attempt_no),
                ),
            )
            updated = conn.execute(
                """
                UPDATE contract_review_run
                SET version = version + 1
                WHERE id = %s
                RETURNING *
                """,
                (review_id,),
            ).fetchone()
            reservation = self._reservation(updated, next_attempt_no, previous=current)
            conn.commit()
        return reservation

    def begin_cancel(
        self,
        review_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> CancellationPlan:
        with self.connect() as conn:
            review = self._lock_owned_review(conn, review_id, tenant_id=tenant_id, user_id=user_id)
            if review["status"] == "CANCELLED":
                conn.commit()
                return CancellationPlan(review_id, review["version"], (), already_cancelled=True)
            if review["status"] in {"SUCCEEDED", "FAILED"}:
                raise self._already_terminal(review["status"])
            if not review["cancel_requested"]:
                review = conn.execute(
                    """
                    UPDATE contract_review_run
                    SET cancel_requested = true, version = version + 1
                    WHERE id = %s
                    RETURNING *
                    """,
                    (review_id,),
                ).fetchone()
            attempts = conn.execute(
                """
                SELECT attempt_no, framework_task_id, framework_run_id
                FROM contract_framework_attempt
                WHERE review_id = %s
                  AND execution_status IN ('PENDING', 'RUNNING')
                  AND framework_task_id IS NOT NULL
                  AND dispatch_status = 'SENT'
                ORDER BY attempt_no
                """,
                (review_id,),
            ).fetchall()
            plan = CancellationPlan(
                review_id=review_id,
                expected_version=review["version"],
                mappings=tuple(
                    AttemptMapping(row["attempt_no"], row["framework_task_id"], row["framework_run_id"])
                    for row in attempts
                ),
            )
            conn.commit()
        return plan

    def finish_cancel(
        self,
        plan: CancellationPlan,
        *,
        tenant_id: str,
        user_id: str,
        snapshots: tuple[FrameworkRunSnapshot, ...],
    ) -> bool:
        if len(snapshots) != len(plan.mappings) or any(item.status != "cancelled" for item in snapshots):
            return False
        with self.connect() as conn:
            review = self._lock_owned_review(conn, plan.review_id, tenant_id=tenant_id, user_id=user_id)
            if review["status"] == "CANCELLED":
                conn.commit()
                return True
            if review["status"] in {"SUCCEEDED", "FAILED"}:
                raise self._already_terminal(review["status"])
            if review["version"] != plan.expected_version or not review["cancel_requested"]:
                conn.commit()
                return False
            conn.execute(
                """
                UPDATE contract_framework_attempt
                SET is_active = false
                WHERE review_id = %s AND is_active
                """,
                (plan.review_id,),
            )
            conn.execute(
                """
                UPDATE contract_framework_attempt
                SET status = 'CANCELLED', execution_status = 'CANCELLED',
                    dispatch_status = CASE
                      WHEN framework_task_id IS NULL THEN 'DISPATCH_FAILED'
                      ELSE dispatch_status
                    END,
                    lease_owner = NULL, lease_until = NULL,
                    is_active = false, finished_at = now()
                WHERE review_id = %s
                  AND status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'ORPHANED')
                """,
                (plan.review_id,),
            )
            conn.execute(
                """
                UPDATE contract_review_run
                SET status = 'CANCELLED', current_stage = NULL,
                    completed_at = now(), version = version + 1
                WHERE id = %s
                """,
                (plan.review_id,),
            )
            conn.commit()
        return True

    @staticmethod
    def _reservation(
        review: dict[str, Any],
        attempt_no: int,
        *,
        attempt: dict[str, Any] | None = None,
        previous: dict[str, Any] | None = None,
    ) -> AttemptReservation:
        attempt = attempt or {}
        return AttemptReservation(
            review_id=review["id"],
            attempt_no=attempt_no,
            tenant_id=review["tenant_id"],
            user_id=review["user_id"],
            expected_version=review["version"],
            business_task_id=review["business_task_id"],
            contract_version_id=review["contract_version_id"],
            model_pack_id=review["model_pack_id"],
            document_id=review["document_id"],
            perspective=review["perspective"],
            our_party_name=review["our_party_name"],
            contract_type=review["contract_type"],
            review_attitude=review["review_attitude"],
            schema_version=review["schema_version"],
            request_fingerprint=attempt.get("request_fingerprint")
            or ReviewStateRepository._framework_request_fingerprint(review, attempt_no),
            previous_task_id=previous["framework_task_id"] if previous else None,
            previous_run_id=previous["framework_run_id"] if previous else None,
            lease_owner=attempt.get("lease_owner"),
            lease_version=attempt.get("lease_version", 0),
        )

    @staticmethod
    def _framework_request_fingerprint(review: dict[str, Any], attempt_no: int) -> str:
        return build_framework_request_fingerprint(
            tenant_id=review["tenant_id"],
            user_id=review["user_id"],
            review_id=review["id"],
            attempt_no=attempt_no,
            business_task_id=review["business_task_id"],
            contract_version_id=review["contract_version_id"],
            document_id=review["document_id"],
            perspective=review["perspective"],
            our_party_name=review["our_party_name"],
            contract_type=review["contract_type"],
            review_attitude=review["review_attitude"],
            schema_version=review["schema_version"],
        )

    @staticmethod
    def _stage(value: str | None) -> str:
        return value if value in REVIEW_STAGES else "PARSING"

    @staticmethod
    def _owned_review(
        conn,
        review_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        review = conn.execute(
            "SELECT * FROM contract_review_run WHERE id = %s",
            (review_id,),
        ).fetchone()
        if review is None:
            raise ContractError("REVIEW_NOT_FOUND", "合同审查任务不存在", status_code=404)
        if review["tenant_id"] != tenant_id or review["user_id"] != user_id:
            raise ContractError("ACCESS_DENIED", "无权访问该合同审查任务", status_code=403)
        return review

    @classmethod
    def _lock_owned_review(
        cls,
        conn,
        review_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        review = conn.execute(
            "SELECT * FROM contract_review_run WHERE id = %s FOR UPDATE",
            (review_id,),
        ).fetchone()
        if review is None:
            raise ContractError("REVIEW_NOT_FOUND", "合同审查任务不存在", status_code=404)
        if review["tenant_id"] != tenant_id or review["user_id"] != user_id:
            raise ContractError("ACCESS_DENIED", "无权访问该合同审查任务", status_code=403)
        return review

    @staticmethod
    def _already_terminal(status: str) -> ContractError:
        return ContractError(
            "REVIEW_ALREADY_TERMINAL",
            "审查任务已经结束，不能取消",
            status_code=409,
            details={"current_status": status},
        )

    @staticmethod
    def _idempotency_conflict(review_id: str | None = None) -> ContractError:
        return ContractError(
            "IDEMPOTENCY_CONFLICT",
            "幂等键已被不同的合同审查请求使用",
            status_code=409,
            user_action_required=True,
            details={"review_id": review_id} if review_id else None,
        )
