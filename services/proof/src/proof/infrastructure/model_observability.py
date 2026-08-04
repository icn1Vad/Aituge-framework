from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from proof.config import Settings, get_settings
from proof.errors import ProofError
from proof.model_pack import current_model_pack_id
from proof.tenant import current_tenant_id


logger = logging.getLogger(__name__)
_IDENTITY_CONFLICT_CODES = frozenset(
    {
        "PROOF_MODEL_DUPLICATE_ATTEMPT",
        "PROOF_MODEL_DUPLICATE_ATTEMPT_CONFLICT",
        "PROOF_MODEL_FALLBACK_INVALID",
        "PROOF_MODEL_DISPATCH_CONFLICT",
        "PROOF_MODEL_TERMINAL_CONFLICT",
        "PROOF_MODEL_TERMINAL_REQUEST_CONFLICT",
    }
)


class ProofAttemptObserver(Protocol):
    invocation_id: str

    def dispatched(self, response: Any) -> None: ...

    def transport_failed(
        self,
        error_code: str,
        *,
        retry_reason: str | None = None,
    ) -> None: ...

    def failed(
        self,
        error_code: str,
        *,
        retry_reason: str | None = None,
    ) -> None: ...

    def validation_failed(self, error_code: str) -> None: ...

    def succeeded(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None: ...


class ProofInvocationObserver(Protocol):
    def begin(
        self,
        *,
        feature_code: str,
        provider: str,
        model_name: str,
        mode: str,
        registration_id: str | None,
        logical_call_id: str,
        attempt_no: int,
        fallback_from_invocation_id: str | None,
    ) -> ProofAttemptObserver: ...


def new_logical_call_id() -> str:
    return f"logical_{uuid.uuid4().hex}"


class ProofModelInvocationObserver:
    """Synchronous Proof adapter for the shared append-only model ledger."""

    def __init__(
        self,
        *,
        enabled: bool,
        database_url: str,
        fallback_tenant_id: str,
        service_version: str | None = None,
        offline_mode: bool = False,
        offline_run_id: str = "",
    ) -> None:
        self.enabled = enabled
        self.database_url = _normalize_database_url(database_url) if enabled else ""
        self.fallback_tenant_id = fallback_tenant_id.strip()
        self.service_version = service_version
        self.offline_mode = offline_mode
        self.offline_run_id = offline_run_id.strip()

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
    ) -> "ProofModelInvocationObserver":
        settings = settings or get_settings()
        return cls(
            enabled=settings.model_invocation_ledger_enabled,
            database_url=settings.model_observability_database_url,
            fallback_tenant_id=settings.model_observability_tenant_id,
            service_version=settings.model_observability_service_version or None,
            offline_mode=settings.model_observability_offline_mode,
            offline_run_id=settings.model_observability_offline_run_id,
        )

    def begin(
        self,
        *,
        feature_code: str,
        provider: str,
        model_name: str,
        mode: str,
        registration_id: str | None,
        logical_call_id: str,
        attempt_no: int,
        fallback_from_invocation_id: str | None,
    ) -> ProofAttemptObserver:
        if attempt_no < 1:
            raise ValueError("attempt_no must be >= 1")
        if not self.enabled or (
            fallback_from_invocation_id is not None
            and fallback_from_invocation_id.startswith("noop_")
        ):
            return NullProofAttempt(invocation_id=f"noop_{uuid.uuid4().hex}")

        try:
            tenant_id = current_tenant_id()
            run_id = None
        except ProofError:
            if (
                not self.offline_mode
                or not self.fallback_tenant_id
                or not self.offline_run_id
            ):
                raise ProofError(
                    "model_observability_tenant_context_missing",
                    "An authoritative tenant context is required for model observation.",
                    status_code=500,
                ) from None
            tenant_id = self.fallback_tenant_id
            run_id = self.offline_run_id

        attempt = ProofModelAttempt(
            observer=self,
            invocation_id=f"inv_{uuid.uuid4().hex}",
            logical_call_id=logical_call_id,
            attempt_no=attempt_no,
            fallback_from_invocation_id=fallback_from_invocation_id,
            tenant_id=tenant_id,
            feature_code=feature_code,
            run_id=run_id,
            provider=provider or "unknown",
            model_name=model_name,
            model_pack_id=current_model_pack_id() or None,
            registration_id=registration_id,
            privacy_mode="PRIVATE" if mode == "local" else "STANDARD",
            route_type="LOCAL" if mode == "local" else "EXTERNAL",
        )
        try:
            self._start(attempt)
        except Exception as exc:
            if _is_identity_conflict(exc):
                raise
            _warn_telemetry_degraded("begin", exc)
            return NullProofAttempt(invocation_id=f"noop_{uuid.uuid4().hex}")
        return attempt

    def _connect(self):
        return psycopg.connect(
            self.database_url,
            connect_timeout=5,
            row_factory=dict_row,
        )

    def _start(self, attempt: "ProofModelAttempt") -> None:
        with self._connect() as connection:
            _lock_logical_attempt(
                connection,
                attempt.logical_call_id,
                attempt.attempt_no,
            )
            existing = connection.execute(
                """
                SELECT
                  invocation_id,
                  logical_call_id,
                  attempt_no,
                  fallback_from_invocation_id,
                  tenant_id,
                  feature_code,
                  run_id,
                  provider,
                  model_pack_id,
                  model_name,
                  privacy_mode,
                  route_type,
                  model_config_id,
                  service_version,
                  lifecycle_status,
                  dispatch_status,
                  provider_request_id_hash
                FROM tuge_model_invocation_projection
                WHERE logical_call_id = %(logical_call_id)s
                  AND attempt_no = %(attempt_no)s
                FOR UPDATE
                """,
                {
                    "logical_call_id": attempt.logical_call_id,
                    "attempt_no": attempt.attempt_no,
                },
            ).fetchone()
            if existing is not None:
                _validate_existing_attempt(
                    existing,
                    attempt,
                    service_version=self.service_version,
                )
                raise RuntimeError("PROOF_MODEL_DUPLICATE_ATTEMPT")

            if attempt.fallback_from_invocation_id is not None:
                previous = connection.execute(
                    """
                    SELECT tenant_id, logical_call_id, attempt_no
                    FROM tuge_model_invocation_projection
                    WHERE invocation_id = %(invocation_id)s
                    FOR SHARE
                    """,
                    {"invocation_id": attempt.fallback_from_invocation_id},
                ).fetchone()
                if (
                    previous is None
                    or previous["tenant_id"] != attempt.tenant_id
                    or previous["logical_call_id"] != attempt.logical_call_id
                    or int(previous["attempt_no"]) >= attempt.attempt_no
                ):
                    raise RuntimeError("PROOF_MODEL_FALLBACK_INVALID")

            sequence = _next_sequence(connection)
            now = _now()
            _append_event(
                connection,
                attempt,
                sequence=sequence,
                event_type="MODEL_INVOCATION_STARTED",
                dispatch_status="NOT_DISPATCHED",
                occurred_at=now,
                outcome=None,
                error_code=None,
                retry_reason=None,
                provider_request_id_hash=None,
                latency_ms=None,
                input_tokens=None,
                output_tokens=None,
                service_version=self.service_version,
            )
            cursor = connection.execute(
                """
                INSERT INTO tuge_model_invocation_projection (
                  invocation_id,
                  projection_version,
                  started_sequence,
                  data_as_of,
                  logical_call_id,
                  attempt_no,
                  fallback_from_invocation_id,
                  tenant_id,
                  feature_code,
                  run_id,
                  started_at,
                  ingested_at,
                  lifecycle_status,
                  dispatch_status,
                  provider,
                  model_pack_id,
                  model_name,
                  privacy_mode,
                  route_type,
                  model_config_id,
                  service_version
                ) VALUES (
                  %(invocation_id)s,
                  1,
                  %(started_sequence)s,
                  %(now)s,
                  %(logical_call_id)s,
                  %(attempt_no)s,
                  %(fallback_from_invocation_id)s,
                  %(tenant_id)s,
                  %(feature_code)s,
                  %(run_id)s,
                  %(now)s,
                  %(now)s,
                  'RUNNING',
                  'NOT_DISPATCHED',
                  %(provider)s,
                  %(model_pack_id)s,
                  %(model_name)s,
                  %(privacy_mode)s,
                  %(route_type)s,
                  %(registration_id)s,
                  %(service_version)s
                )
                """,
                {
                    **attempt._facts(),
                    "started_sequence": sequence,
                    "now": now,
                    "service_version": self.service_version,
                },
            )
            _require_one_row(cursor, "PROOF_MODEL_START_PROJECTION_MISSING")

    def _mark_dispatched(
        self,
        attempt: "ProofModelAttempt",
        provider_request_id_hash: str | None,
    ) -> None:
        if not self.enabled:
            return
        with self._connect() as connection:
            projection = _lock_projection(connection, attempt.invocation_id)
            if projection["lifecycle_status"] != "RUNNING":
                raise RuntimeError("PROOF_MODEL_ATTEMPT_ALREADY_TERMINAL")
            if projection["dispatch_status"] == "DISPATCHED":
                if (
                    projection["provider_request_id_hash"]
                    == provider_request_id_hash
                ):
                    _hydrate_attempt(attempt, projection)
                    return
                raise RuntimeError("PROOF_MODEL_DISPATCH_CONFLICT")
            if projection["dispatch_status"] != "NOT_DISPATCHED":
                raise RuntimeError("PROOF_MODEL_DISPATCH_CONFLICT")

            sequence = _next_sequence(connection)
            now = _now()
            _append_event(
                connection,
                attempt,
                sequence=sequence,
                event_type="MODEL_INVOCATION_DISPATCHED",
                dispatch_status="DISPATCHED",
                occurred_at=now,
                outcome=None,
                error_code=None,
                retry_reason=None,
                provider_request_id_hash=provider_request_id_hash,
                latency_ms=None,
                input_tokens=None,
                output_tokens=None,
                service_version=self.service_version,
            )
            cursor = connection.execute(
                """
                UPDATE tuge_model_invocation_projection
                SET
                  dispatch_sequence = %(sequence)s,
                  dispatch_status = 'DISPATCHED',
                  provider_request_id_hash = %(provider_request_id_hash)s,
                  projection_version = projection_version + 1,
                  data_as_of = %(now)s,
                  ingested_at = %(now)s
                WHERE invocation_id = %(invocation_id)s
                  AND lifecycle_status = 'RUNNING'
                  AND dispatch_status = 'NOT_DISPATCHED'
                """,
                {
                    "sequence": sequence,
                    "provider_request_id_hash": provider_request_id_hash,
                    "now": now,
                    "invocation_id": attempt.invocation_id,
                },
            )
            _require_one_row(cursor, "PROOF_MODEL_DISPATCH_PROJECTION_MISSING")

    def _transport_failed(
        self,
        attempt: "ProofModelAttempt",
        *,
        error_code: str,
        retry_reason: str | None,
        latency_ms: int,
    ) -> None:
        if not self.enabled:
            return
        with self._connect() as connection:
            projection = _lock_projection(connection, attempt.invocation_id)
            if (
                projection["lifecycle_status"] != "RUNNING"
                or projection["dispatch_status"] != "NOT_DISPATCHED"
            ):
                raise RuntimeError("PROOF_MODEL_TRANSPORT_TERMINAL_CONFLICT")

            dispatch_sequence = _next_sequence(connection)
            dispatch_time = _now()
            _append_event(
                connection,
                attempt,
                sequence=dispatch_sequence,
                event_type="MODEL_INVOCATION_DISPATCH_UNKNOWN",
                dispatch_status="DISPATCH_UNKNOWN",
                occurred_at=dispatch_time,
                outcome=None,
                error_code=None,
                retry_reason=None,
                provider_request_id_hash=None,
                latency_ms=None,
                input_tokens=None,
                output_tokens=None,
                service_version=self.service_version,
            )
            cursor = connection.execute(
                """
                UPDATE tuge_model_invocation_projection
                SET
                  dispatch_sequence = %(sequence)s,
                  dispatch_status = 'DISPATCH_UNKNOWN',
                  projection_version = projection_version + 1,
                  data_as_of = %(now)s,
                  ingested_at = %(now)s
                WHERE invocation_id = %(invocation_id)s
                  AND lifecycle_status = 'RUNNING'
                  AND dispatch_status = 'NOT_DISPATCHED'
                """,
                {
                    "sequence": dispatch_sequence,
                    "now": dispatch_time,
                    "invocation_id": attempt.invocation_id,
                },
            )
            _require_one_row(
                cursor,
                "PROOF_MODEL_TRANSPORT_PROJECTION_MISSING",
            )
            self._append_terminal(
                connection,
                attempt,
                event_type="MODEL_INVOCATION_FAILED",
                dispatch_status="DISPATCH_UNKNOWN",
                outcome="FAILURE",
                error_code=error_code,
                retry_reason=retry_reason,
                latency_ms=latency_ms,
                input_tokens=None,
                output_tokens=None,
            )

    def _terminal(
        self,
        attempt: "ProofModelAttempt",
        *,
        event_type: str,
        outcome: str,
        error_code: str | None,
        retry_reason: str | None,
        latency_ms: int,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        if not self.enabled:
            return
        with self._connect() as connection:
            projection = _lock_projection(connection, attempt.invocation_id)
            if projection["lifecycle_status"] == "TERMINAL":
                if _terminal_matches(
                    connection,
                    projection,
                    event_type=event_type,
                    dispatch_status="DISPATCHED",
                    outcome=outcome,
                    error_code=error_code,
                    retry_reason=retry_reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ):
                    _hydrate_attempt(attempt, projection)
                    return
                raise RuntimeError("PROOF_MODEL_TERMINAL_CONFLICT")
            if (
                projection["lifecycle_status"] != "RUNNING"
                or projection["dispatch_status"] != "DISPATCHED"
                or attempt.dispatch_status != "DISPATCHED"
            ):
                raise RuntimeError("PROOF_MODEL_TERMINAL_STATE_INVALID")
            if (
                attempt.provider_request_id_hash is not None
                and projection["provider_request_id_hash"]
                != attempt.provider_request_id_hash
            ):
                raise RuntimeError("PROOF_MODEL_TERMINAL_REQUEST_CONFLICT")
            _hydrate_attempt(attempt, projection)
            self._append_terminal(
                connection,
                attempt,
                event_type=event_type,
                dispatch_status="DISPATCHED",
                outcome=outcome,
                error_code=error_code,
                retry_reason=retry_reason,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

    def _append_terminal(
        self,
        connection,
        attempt: "ProofModelAttempt",
        *,
        event_type: str,
        dispatch_status: str,
        outcome: str,
        error_code: str | None,
        retry_reason: str | None,
        latency_ms: int,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        sequence = _next_sequence(connection)
        now = _now()
        _append_event(
            connection,
            attempt,
            sequence=sequence,
            event_type=event_type,
            dispatch_status=dispatch_status,
            occurred_at=now,
            outcome=outcome,
            error_code=error_code,
            retry_reason=retry_reason,
            provider_request_id_hash=attempt.provider_request_id_hash,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            service_version=self.service_version,
        )
        cursor = connection.execute(
            """
            UPDATE tuge_model_invocation_projection
            SET
              terminal_sequence = %(sequence)s,
              lifecycle_status = 'TERMINAL',
              dispatch_status = %(dispatch_status)s,
              outcome = %(outcome)s,
              finished_at = %(now)s,
              error_code = %(error_code)s,
              retry_reason = %(retry_reason)s,
              input_token_count = %(input_tokens)s,
              output_token_count = %(output_tokens)s,
              latency_ms = %(latency_ms)s,
              projection_version = projection_version + 1,
              data_as_of = %(now)s,
              ingested_at = %(now)s
            WHERE invocation_id = %(invocation_id)s
              AND lifecycle_status = 'RUNNING'
              AND dispatch_status = %(dispatch_status)s
            """,
            {
                "sequence": sequence,
                "dispatch_status": dispatch_status,
                "outcome": outcome,
                "now": now,
                "error_code": error_code,
                "retry_reason": retry_reason,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
                "invocation_id": attempt.invocation_id,
            },
        )
        _require_one_row(cursor, "PROOF_MODEL_TERMINAL_PROJECTION_MISSING")


class NullProofAttempt:
    """No-op attempt used only when telemetry is disabled or unavailable."""

    def __init__(self, *, invocation_id: str) -> None:
        self.invocation_id = invocation_id
        self.dispatch_status = "NOT_DISPATCHED"
        self._terminal = False

    def dispatched(self, response: Any) -> None:
        del response
        self._require_open()
        if self.dispatch_status != "NOT_DISPATCHED":
            raise RuntimeError("model attempt dispatch was already concluded")
        self.dispatch_status = "DISPATCHED"

    def transport_failed(
        self,
        error_code: str,
        *,
        retry_reason: str | None = None,
    ) -> None:
        del error_code, retry_reason
        self._require_open()
        if self.dispatch_status != "NOT_DISPATCHED":
            raise RuntimeError("transport failure must precede a dispatch conclusion")
        self.dispatch_status = "DISPATCH_UNKNOWN"
        self._terminal = True

    def failed(
        self,
        error_code: str,
        *,
        retry_reason: str | None = None,
    ) -> None:
        del error_code, retry_reason
        self._finish_dispatched()

    def validation_failed(self, error_code: str) -> None:
        del error_code
        self._finish_dispatched()

    def succeeded(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        del input_tokens, output_tokens
        self._finish_dispatched()

    def _finish_dispatched(self) -> None:
        self._require_open()
        if self.dispatch_status != "DISPATCHED":
            raise RuntimeError("provider response terminal requires DISPATCHED")
        self._terminal = True

    def _require_open(self) -> None:
        if self._terminal:
            raise RuntimeError("model attempt is already terminal")


class ProofModelAttempt:
    def __init__(
        self,
        *,
        observer: ProofModelInvocationObserver,
        invocation_id: str,
        logical_call_id: str,
        attempt_no: int,
        fallback_from_invocation_id: str | None,
        tenant_id: str,
        feature_code: str,
        run_id: str | None,
        provider: str,
        model_name: str,
        model_pack_id: str | None,
        registration_id: str | None,
        privacy_mode: str,
        route_type: str,
    ) -> None:
        self.observer = observer
        self.invocation_id = invocation_id
        self.logical_call_id = logical_call_id
        self.attempt_no = attempt_no
        self.fallback_from_invocation_id = fallback_from_invocation_id
        self.tenant_id = tenant_id
        self.feature_code = feature_code
        self.run_id = run_id
        self.provider = provider
        self.model_name = model_name
        self.model_pack_id = model_pack_id
        self.registration_id = registration_id
        self.privacy_mode = privacy_mode
        self.route_type = route_type
        self.dispatch_status = "NOT_DISPATCHED"
        self.provider_request_id_hash: str | None = None
        self._started = time.perf_counter()
        self._terminal = False
        self._telemetry_active = True

    def dispatched(self, response: Any) -> None:
        if self.dispatch_status != "NOT_DISPATCHED" or self._terminal:
            raise RuntimeError("model attempt dispatch was already concluded")
        try:
            request_id = _provider_request_id(response)
        except Exception as exc:
            _warn_telemetry_degraded("provider_request_id", exc)
            request_id = None
        request_hash = (
            hashlib.sha256(request_id.encode("utf-8")).hexdigest()
            if request_id
            else None
        )
        if self._telemetry_active:
            try:
                self.observer._mark_dispatched(self, request_hash)
            except Exception as exc:
                if _is_identity_conflict(exc):
                    raise
                _warn_telemetry_degraded("dispatch", exc)
                self._telemetry_active = False
        self.dispatch_status = "DISPATCHED"
        self.provider_request_id_hash = request_hash

    def transport_failed(
        self,
        error_code: str,
        *,
        retry_reason: str | None = None,
    ) -> None:
        self._require_open()
        if self.dispatch_status != "NOT_DISPATCHED":
            raise RuntimeError("transport failure must precede a dispatch conclusion")
        if self._telemetry_active:
            try:
                self.observer._transport_failed(
                    self,
                    error_code=error_code,
                    retry_reason=retry_reason,
                    latency_ms=self._latency_ms(),
                )
            except Exception as exc:
                if _is_identity_conflict(exc):
                    raise
                _warn_telemetry_degraded("transport_terminal", exc)
                self._telemetry_active = False
        self.dispatch_status = "DISPATCH_UNKNOWN"
        self._terminal = True

    def failed(
        self,
        error_code: str,
        *,
        retry_reason: str | None = None,
    ) -> None:
        self._finalize(
            event_type="MODEL_INVOCATION_FAILED",
            outcome="FAILURE",
            error_code=error_code,
            retry_reason=retry_reason,
        )

    def validation_failed(self, error_code: str) -> None:
        self._finalize(
            event_type="MODEL_INVOCATION_VALIDATION_FAILED",
            outcome="FAILURE",
            error_code=error_code,
            retry_reason=None,
        )

    def succeeded(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        self._finalize(
            event_type="MODEL_INVOCATION_SUCCEEDED",
            outcome="SUCCESS",
            error_code=None,
            retry_reason=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _finalize(
        self,
        *,
        event_type: str,
        outcome: str,
        error_code: str | None,
        retry_reason: str | None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        self._require_open()
        if self.dispatch_status != "DISPATCHED":
            raise RuntimeError("provider response terminal requires DISPATCHED")
        if self._telemetry_active:
            try:
                self.observer._terminal(
                    self,
                    event_type=event_type,
                    outcome=outcome,
                    error_code=error_code,
                    retry_reason=retry_reason,
                    latency_ms=self._latency_ms(),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            except Exception as exc:
                if _is_identity_conflict(exc):
                    raise
                _warn_telemetry_degraded("terminal", exc)
                self._telemetry_active = False
        self._terminal = True

    def _require_open(self) -> None:
        if self._terminal:
            raise RuntimeError("model attempt is already terminal")

    def _latency_ms(self) -> int:
        return max(0, int((time.perf_counter() - self._started) * 1000))

    def _facts(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "logical_call_id": self.logical_call_id,
            "attempt_no": self.attempt_no,
            "fallback_from_invocation_id": self.fallback_from_invocation_id,
            "tenant_id": self.tenant_id,
            "feature_code": self.feature_code,
            "run_id": self.run_id,
            "provider": self.provider,
            "model_name": self.model_name,
            "model_pack_id": self.model_pack_id,
            "registration_id": self.registration_id,
            "privacy_mode": self.privacy_mode,
            "route_type": self.route_type,
        }


@contextmanager
def observed_tool_attempt(
    settings: Settings,
    *,
    feature_code: str,
    model_name: str,
    logical_call_id: str,
    attempt_no: int,
    fallback_from_invocation_id: str | None,
    retry_reason: str | None = None,
) -> Iterator[ProofModelAttempt]:
    """Classify one offline tool request without persisting its payload."""

    attempt = ProofModelInvocationObserver.from_settings(settings).begin(
        feature_code=feature_code,
        provider="openai_compatible",
        model_name=model_name,
        mode="api",
        registration_id=None,
        logical_call_id=logical_call_id,
        attempt_no=attempt_no,
        fallback_from_invocation_id=fallback_from_invocation_id,
    )
    try:
        yield attempt
    except httpx.HTTPError:
        if not attempt._terminal:
            if attempt.dispatch_status == "NOT_DISPATCHED":
                attempt.transport_failed(
                    "PROOF_OFFLINE_MODEL_TRANSPORT_ERROR",
                    retry_reason=retry_reason,
                )
            else:
                attempt.failed(
                    "PROOF_OFFLINE_MODEL_REQUEST_FAILED",
                    retry_reason=retry_reason,
                )
        raise
    except Exception:
        if not attempt._terminal:
            if attempt.dispatch_status == "DISPATCHED":
                attempt.validation_failed(
                    "PROOF_OFFLINE_MODEL_VALIDATION_FAILED"
                )
            else:
                attempt.transport_failed(
                    "PROOF_OFFLINE_MODEL_OUTCOME_UNKNOWN",
                    retry_reason=retry_reason,
                )
        raise
    else:
        if not attempt._terminal:
            attempt.validation_failed(
                "PROOF_OFFLINE_MODEL_TERMINAL_MISSING"
            )
            raise RuntimeError("PROOF_OFFLINE_MODEL_TERMINAL_MISSING")


def _lock_logical_attempt(
    connection,
    logical_call_id: str,
    attempt_no: int,
) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"proof:{logical_call_id}:{attempt_no}",),
    )


def _lock_projection(connection, invocation_id: str) -> dict[str, Any]:
    projection = connection.execute(
        """
        SELECT
          invocation_id,
          logical_call_id,
          attempt_no,
          tenant_id,
          lifecycle_status,
          dispatch_status,
          terminal_sequence,
          provider_request_id_hash
        FROM tuge_model_invocation_projection
        WHERE invocation_id = %(invocation_id)s
        FOR UPDATE
        """,
        {"invocation_id": invocation_id},
    ).fetchone()
    if projection is None:
        raise RuntimeError("PROOF_MODEL_PROJECTION_NOT_FOUND")
    return projection


def _validate_existing_attempt(
    existing: dict[str, Any],
    attempt: ProofModelAttempt,
    *,
    service_version: str | None,
) -> None:
    expected = {
        "logical_call_id": attempt.logical_call_id,
        "attempt_no": attempt.attempt_no,
        "fallback_from_invocation_id": attempt.fallback_from_invocation_id,
        "tenant_id": attempt.tenant_id,
        "feature_code": attempt.feature_code,
        "run_id": attempt.run_id,
        "provider": attempt.provider,
        "model_pack_id": attempt.model_pack_id,
        "model_name": attempt.model_name,
        "privacy_mode": attempt.privacy_mode,
        "route_type": attempt.route_type,
        "model_config_id": attempt.registration_id,
        "service_version": service_version,
    }
    if any(existing.get(key) != value for key, value in expected.items()):
        raise RuntimeError("PROOF_MODEL_DUPLICATE_ATTEMPT_CONFLICT")


def _hydrate_attempt(
    attempt: ProofModelAttempt,
    projection: dict[str, Any],
) -> None:
    attempt.invocation_id = str(projection["invocation_id"])
    attempt.dispatch_status = str(projection["dispatch_status"])
    attempt.provider_request_id_hash = projection.get(
        "provider_request_id_hash"
    )
    attempt._terminal = projection["lifecycle_status"] == "TERMINAL"


def _terminal_matches(
    connection,
    projection: dict[str, Any],
    *,
    event_type: str,
    dispatch_status: str,
    outcome: str,
    error_code: str | None,
    retry_reason: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
) -> bool:
    terminal_sequence = projection.get("terminal_sequence")
    if terminal_sequence is None:
        return False
    event = connection.execute(
        """
        SELECT
          event_type,
          dispatch_status,
          outcome,
          error_code,
          retry_reason,
          input_token_count,
          output_token_count
        FROM tuge_model_invocation_event
        WHERE invocation_id = %(invocation_id)s
          AND server_sequence = %(terminal_sequence)s
        """,
        {
            "invocation_id": projection["invocation_id"],
            "terminal_sequence": terminal_sequence,
        },
    ).fetchone()
    if event is None:
        return False
    expected = {
        "event_type": event_type,
        "dispatch_status": dispatch_status,
        "outcome": outcome,
        "error_code": error_code,
        "retry_reason": retry_reason,
        "input_token_count": input_tokens,
        "output_token_count": output_tokens,
    }
    return all(event.get(key) == value for key, value in expected.items())


def _require_one_row(cursor, error_code: str) -> None:
    if cursor.rowcount != 1:
        raise RuntimeError(error_code)


def _append_event(
    connection,
    attempt: ProofModelAttempt,
    *,
    sequence: int,
    event_type: str,
    dispatch_status: str,
    occurred_at: datetime,
    outcome: str | None,
    error_code: str | None,
    retry_reason: str | None,
    provider_request_id_hash: str | None,
    latency_ms: int | None,
    input_tokens: int | None,
    output_tokens: int | None,
    service_version: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO tuge_model_invocation_event (
          event_id,
          server_sequence,
          event_type,
          schema_version,
          logical_call_id,
          invocation_id,
          attempt_no,
          dispatch_status,
          fallback_from_invocation_id,
          occurred_at,
          ingested_at,
          tenant_id,
          feature_code,
          run_id,
          provider,
          provider_request_id_hash,
          model_pack_id,
          model_name,
          privacy_mode,
          route_type,
          model_config_id,
          input_token_count,
          output_token_count,
          latency_ms,
          outcome,
          error_code,
          retry_reason,
          service_version,
          metadata_json
        ) VALUES (
          %(event_id)s,
          %(server_sequence)s,
          %(event_type)s,
          1,
          %(logical_call_id)s,
          %(invocation_id)s,
          %(attempt_no)s,
          %(dispatch_status)s,
          %(fallback_from_invocation_id)s,
          %(occurred_at)s,
          %(occurred_at)s,
          %(tenant_id)s,
          %(feature_code)s,
          %(run_id)s,
          %(provider)s,
          %(provider_request_id_hash)s,
          %(model_pack_id)s,
          %(model_name)s,
          %(privacy_mode)s,
          %(route_type)s,
          %(registration_id)s,
          %(input_tokens)s,
          %(output_tokens)s,
          %(latency_ms)s,
          %(outcome)s,
          %(error_code)s,
          %(retry_reason)s,
          %(service_version)s,
          %(metadata_json)s
        )
        """,
        {
            **attempt._facts(),
            "event_id": f"mie_{uuid.uuid4().hex}",
            "server_sequence": sequence,
            "event_type": event_type,
            "dispatch_status": dispatch_status,
            "occurred_at": occurred_at,
            "provider_request_id_hash": provider_request_id_hash,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "outcome": outcome,
            "error_code": error_code,
            "retry_reason": retry_reason,
            "service_version": service_version,
            "metadata_json": Jsonb({}),
        },
    )


def _next_sequence(connection) -> int:
    connection.execute(
        """
        INSERT INTO tuge_model_observability_sequence (
          sequence_name,
          sequence_value
        ) VALUES ('event', 0)
        ON CONFLICT (sequence_name) DO NOTHING
        """
    )
    row = connection.execute(
        """
        UPDATE tuge_model_observability_sequence
        SET sequence_value = sequence_value + 1
        WHERE sequence_name = 'event'
        RETURNING sequence_value
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("model observability sequence allocation failed")
    return int(row["sequence_value"])


def usage_token_counts(
    usage: dict[str, Any],
) -> tuple[int | None, int | None]:
    if not isinstance(usage, dict):
        raise TypeError("model usage must be an object")
    return (
        _optional_token_count(usage, "prompt_tokens", "input_tokens"),
        _optional_token_count(usage, "completion_tokens", "output_tokens"),
    )


def _optional_token_count(
    usage: dict[str, Any],
    *keys: str,
) -> int | None:
    for key in keys:
        value = usage.get(key)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("token count must be an integer")
            if value < 0 or value > 9_223_372_036_854_775_807:
                raise ValueError(
                    "token count is outside the PostgreSQL bigint range"
                )
            return value
    return None


def _provider_request_id(response: Any) -> str | None:
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    for name in (
        "x-request-id",
        "x-dashscope-request-id",
        "request-id",
        "trace-id",
    ):
        value = str(headers.get(name) or "").strip()
        if value:
            return value
    return None


def _is_identity_conflict(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _IDENTITY_CONFLICT_CODES:
        return True
    return str(exc) in _IDENTITY_CONFLICT_CODES


def _warn_telemetry_degraded(operation: str, exc: BaseException) -> None:
    logger.warning(
        "proof_model_observability_degraded operation=%s error_type=%s",
        operation,
        exc.__class__.__name__,
    )


def _normalize_database_url(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized.startswith("postgresql+asyncpg://"):
        normalized = "postgresql://" + normalized.removeprefix(
            "postgresql+asyncpg://"
        )
    if not normalized.startswith(("postgresql://", "postgres://")):
        raise ValueError(
            "PROOF_MODEL_OBSERVABILITY_DATABASE_URL must be PostgreSQL "
            "when model invocation ledger is enabled"
        )
    return normalized


def _now() -> datetime:
    return datetime.now(timezone.utc)
