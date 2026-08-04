from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .domain import (
    CostSource,
    DuplicateModelInvocationAttemptError,
    DispatchStatus,
    InvocationLifecycleStatus,
    InvocationMetrics,
    InvocationNotFoundError,
    InvocationOutcome,
    InvalidInvocationTransitionError,
    ModelInvocationContext,
    ModelInvocationEventType,
    PrivacyMode,
    RouteType,
    TERMINAL_EVENT_TYPES,
    hash_provider_request_id,
    new_id,
    sanitize_metadata,
    utc_now,
    validate_error_fields,
    validate_terminal_outcome,
)
from .entities import ModelInvocationEventEntity, ModelInvocationProjectionEntity
from .repository import ModelInvocationRepository


SessionContextFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class ModelInvocationLifecycleService:
    """Atomically append lifecycle facts and advance a rebuildable projection."""

    def __init__(self, session_factory: SessionContextFactory) -> None:
        self._session_factory = session_factory

    async def start_invocation(
        self,
        context: ModelInvocationContext,
        *,
        occurred_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ModelInvocationProjectionEntity:
        context.validate()
        safe_metadata = sanitize_metadata(
            metadata,
            event_type=ModelInvocationEventType.STARTED,
        )
        occurred = _validated_occurred_at(occurred_at)
        ingested = utc_now()
        async with self._session_factory() as session:
            repository = ModelInvocationRepository(session)
            await repository.acquire_projection_write_lock()
            await repository.lock_logical_attempt(
                context.logical_call_id, context.attempt_no
            )
            existing = await repository.get_projection_by_logical_attempt(
                context.logical_call_id,
                context.attempt_no,
                for_update=True,
            )
            if existing is not None:
                self._assert_idempotent_start(existing, context)
                started = await repository.get_invocation_event(
                    existing.invocation_id,
                    (ModelInvocationEventType.STARTED.value,),
                )
                if started is None:
                    raise InvalidInvocationTransitionError("projection has no STARTED fact")
                self._validate_started_event(started)
                self._assert_event_facts(
                    started, occurred_at=occurred_at, metadata=safe_metadata
                )
                return existing
            if context.fallback_from_invocation_id:
                previous = await repository.get_projection(
                    context.fallback_from_invocation_id,
                    for_update=True,
                )
                if previous is None:
                    raise InvalidInvocationTransitionError(
                        "fallback_from_invocation_id does not exist"
                    )
                if (
                    previous.tenant_id != context.tenant_id
                    or previous.logical_call_id != context.logical_call_id
                    or previous.attempt_no >= context.attempt_no
                ):
                    raise InvalidInvocationTransitionError(
                        "fallback must reference an earlier attempt in the same logical call"
                    )


            event = ModelInvocationEventEntity(
                event_id=new_id("mie"),
                event_type=ModelInvocationEventType.STARTED.value,
                schema_version=1,
                logical_call_id=context.logical_call_id,
                invocation_id=context.invocation_id,
                attempt_no=context.attempt_no,
                dispatch_status=DispatchStatus.NOT_DISPATCHED.value,
                fallback_from_invocation_id=context.fallback_from_invocation_id,
                occurred_at=occurred,
                ingested_at=ingested,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                feature_code=context.feature_code,
                task_id=context.task_id,
                run_id=context.run_id,
                stage_id=context.stage_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                provider=context.provider,
                model_pack_id=context.model_pack_id,
                model_pack_version=context.model_pack_version,
                model_name=context.model_name,
                deployment_name=context.deployment_name,
                provider_region=context.provider_region,
                privacy_mode=context.privacy_mode.value,
                route_type=context.route_type.value,
                model_config_id=context.model_config_id,
                service_version=context.service_version,
                metadata_json=safe_metadata,
            )
            projection = ModelInvocationProjectionEntity(
                invocation_id=context.invocation_id,
                projection_version=1,
                data_as_of=ingested,
                logical_call_id=context.logical_call_id,
                attempt_no=context.attempt_no,
                fallback_from_invocation_id=context.fallback_from_invocation_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                feature_code=context.feature_code,
                task_id=context.task_id,
                run_id=context.run_id,
                stage_id=context.stage_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                started_at=occurred,
                ingested_at=ingested,
                lifecycle_status=InvocationLifecycleStatus.RUNNING.value,
                dispatch_status=DispatchStatus.NOT_DISPATCHED.value,
                provider=context.provider,
                model_pack_id=context.model_pack_id,
                model_pack_version=context.model_pack_version,
                model_name=context.model_name,
                deployment_name=context.deployment_name,
                provider_region=context.provider_region,
                privacy_mode=context.privacy_mode.value,
                route_type=context.route_type.value,
                model_config_id=context.model_config_id,
                service_version=context.service_version,
            )
            await repository.add_event(event)
            projection.started_sequence = event.server_sequence
            await repository.add_projection(projection)
            return projection

    async def mark_dispatched(
        self,
        invocation_id: str,
        *,
        provider_request_id: str | None = None,
        occurred_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ModelInvocationProjectionEntity:
        return await self._record_dispatch_conclusion(
            invocation_id,
            status=DispatchStatus.DISPATCHED,
            event_type=ModelInvocationEventType.DISPATCHED,
            provider_request_id=provider_request_id,
            occurred_at=occurred_at,
            metadata=metadata,
        )

    async def mark_dispatch_unknown(
        self,
        invocation_id: str,
        *,
        occurred_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ModelInvocationProjectionEntity:
        return await self._record_dispatch_conclusion(
            invocation_id,
            status=DispatchStatus.DISPATCH_UNKNOWN,
            event_type=ModelInvocationEventType.DISPATCH_UNKNOWN,
            provider_request_id=None,
            occurred_at=occurred_at,
            metadata=metadata,
        )

    async def terminate(
        self,
        invocation_id: str,
        *,
        event_type: ModelInvocationEventType,
        outcome: InvocationOutcome,
        error_code: str | None = None,
        retry_reason: str | None = None,
        metrics: InvocationMetrics | None = None,
        provider_request_id: str | None = None,
        occurred_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ModelInvocationProjectionEntity:
        if event_type not in TERMINAL_EVENT_TYPES:
            raise InvalidInvocationTransitionError(
                f"{event_type.value} is not terminal"
            )
        metrics = metrics or InvocationMetrics()
        metrics.validate()
        safe_metadata = sanitize_metadata(metadata, event_type=event_type)
        validate_error_fields(error_code, retry_reason)
        request_hash = hash_provider_request_id(provider_request_id)
        occurred = _validated_occurred_at(occurred_at)
        ingested = utc_now()
        async with self._session_factory() as session:
            repository = ModelInvocationRepository(session)
            await repository.acquire_projection_write_lock()
            await repository.lock_invocation(invocation_id)
            projection = await self._require_projection(
                repository,
                invocation_id,
                for_update=True,
            )
            if (
                projection.provider_request_id_hash is not None
                and request_hash is not None
                and projection.provider_request_id_hash != request_hash
            ):
                raise InvalidInvocationTransitionError(
                    "provider request id hash cannot change"
                )
            effective_request_hash = (
                request_hash or projection.provider_request_id_hash
            )
            if projection.lifecycle_status == InvocationLifecycleStatus.TERMINAL.value:
                terminal = await repository.get_invocation_event(
                    invocation_id,
                    tuple(item.value for item in TERMINAL_EVENT_TYPES),
                )
                if terminal is None:
                    raise InvalidInvocationTransitionError("terminal projection has no fact")
                self._assert_terminal_facts(
                    terminal,
                    event_type=event_type,
                    outcome=outcome,
                    error_code=error_code,
                    retry_reason=retry_reason,
                    metrics=metrics,
                    occurred_at=occurred_at,
                    provider_request_id_hash=effective_request_hash,
                    compare_provider_request_id_hash=True,
                    metadata=safe_metadata,
                )
                return projection

            if _as_aware(occurred) < _as_aware(projection.started_at):
                raise InvalidInvocationTransitionError(
                    "terminal occurred_at cannot precede STARTED"
                )
            dispatch_status = DispatchStatus(projection.dispatch_status)
            dispatch_event = None
            if (
                dispatch_status is not DispatchStatus.DISPATCHED
                and _metrics_have_usage_or_cost(metrics)
            ):
                raise InvalidInvocationTransitionError(
                    "uncertain dispatch cannot contain usage or cost"
                )
            if (
                dispatch_status is DispatchStatus.NOT_DISPATCHED
                and effective_request_hash is not None
            ):
                raise InvalidInvocationTransitionError(
                    "NOT_DISPATCHED cannot contain a provider request id hash"
                )

            if dispatch_status is not DispatchStatus.NOT_DISPATCHED:
                dispatch_event = await repository.get_invocation_event(
                    invocation_id,
                    (
                        ModelInvocationEventType.DISPATCHED.value,
                        ModelInvocationEventType.DISPATCH_UNKNOWN.value,
                    ),
                )
                if dispatch_event is None:
                    raise InvalidInvocationTransitionError(
                        "dispatch projection has no matching fact"
                    )
                if _as_aware(occurred) < _as_aware(dispatch_event.occurred_at):
                    raise InvalidInvocationTransitionError(
                        "terminal occurred_at cannot precede dispatch"
                    )
            validate_terminal_outcome(event_type, outcome, dispatch_status)
            event = self._event_from_projection(
                projection,
                event_type=event_type,
                dispatch_status=dispatch_status,
                occurred_at=occurred,
                ingested_at=ingested,
                outcome=outcome,
                error_code=error_code,
                retry_reason=retry_reason,
                metrics=metrics,
                metadata=safe_metadata,
                provider_request_id_hash=effective_request_hash,
            )
            await repository.add_event(event)
            self._apply_terminal_event(projection, event)
            await session.flush()
            return projection

    async def get_projection(
        self,
        invocation_id: str,
    ) -> ModelInvocationProjectionEntity:
        async with self._session_factory() as session:
            return await self._require_projection(
                ModelInvocationRepository(session), invocation_id
            )

    async def get_events(
        self,
        invocation_id: str,
    ) -> list[ModelInvocationEventEntity]:
        async with self._session_factory() as session:
            repository = ModelInvocationRepository(session)
            await self._require_projection(repository, invocation_id)
            return await repository.list_invocation_events(invocation_id)

    async def rebuild_projection(self, *, batch_size: int = 1_000) -> int:
        """Replay into an isolated generation and atomically publish it."""

        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        shadow_table = (
            "tuge_model_projection_shadow_"
            f"{new_id('generation').replace('_', '')}"
        )

        async with self._session_factory() as session:
            repository = ModelInvocationRepository(session)
            await repository.acquire_projection_rebuild_lock()
            await repository.create_projection_shadow(shadow_table)
            rebuilt_count = 0
            after_sequence = 0
            while True:
                started_events = await repository.list_started_event_batch(
                    after_sequence=after_sequence,
                    limit=batch_size,
                )
                if not started_events:
                    break
                invocation_ids = [event.invocation_id for event in started_events]
                events = await repository.list_events_for_invocations(invocation_ids)
                events_by_invocation: dict[
                    str, list[ModelInvocationEventEntity]
                ] = {invocation_id: [] for invocation_id in invocation_ids}
                for event in events:
                    events_by_invocation[event.invocation_id].append(event)
                projections: list[ModelInvocationProjectionEntity] = []
                for started in started_events:
                    self._validate_started_event(started)
                    projection = self._projection_from_started(started)
                    dispatch_event = None
                    for event in events_by_invocation[started.invocation_id]:
                        event_type = ModelInvocationEventType(event.event_type)
                        if event_type is ModelInvocationEventType.STARTED:
                            if event.event_id != started.event_id:
                                raise InvalidInvocationTransitionError(
                                    f"duplicate STARTED for {event.invocation_id}"
                                )
                            continue
                        self._validate_event_against_started(started, event)
                        if event_type is ModelInvocationEventType.DISPATCHED:
                            self._apply_dispatch_event(
                                projection,
                                status=DispatchStatus.DISPATCHED,
                                event=event,
                            )
                            dispatch_event = event
                        elif event_type is ModelInvocationEventType.DISPATCH_UNKNOWN:
                            self._apply_dispatch_event(
                                projection,
                                status=DispatchStatus.DISPATCH_UNKNOWN,
                                event=event,
                            )
                            dispatch_event = event
                        elif event_type in TERMINAL_EVENT_TYPES:
                            if (
                                projection.lifecycle_status
                                == InvocationLifecycleStatus.TERMINAL.value
                            ):
                                raise InvalidInvocationTransitionError(
                                    f"duplicate terminal for {event.invocation_id}"
                                )
                            if (
                                dispatch_event is not None
                                and _as_aware(event.occurred_at)
                                < _as_aware(dispatch_event.occurred_at)
                            ):
                                raise InvalidInvocationTransitionError(
                                    "terminal occurred_at cannot precede dispatch"
                                )
                            outcome = InvocationOutcome(str(event.outcome))
                            validate_terminal_outcome(
                                event_type,
                                outcome,
                                DispatchStatus(event.dispatch_status),
                            )
                            self._apply_terminal_event(projection, event)
                        else:  # pragma: no cover - exhaustive enum guard
                            raise InvalidInvocationTransitionError(event.event_type)
                    projections.append(projection)
                await repository.insert_projection_shadow(
                    shadow_table,
                    projections,
                )
                rebuilt_count += len(projections)
                after_sequence = int(started_events[-1].server_sequence)
            await repository.swap_projection_shadow(shadow_table)
            return rebuilt_count

    async def reconcile_orphans(
        self,
        *,
        older_than: timedelta,
        now: datetime | None = None,
        limit: int = 500,
    ) -> int:
        """Conservatively close stale RUNNING rows without inventing dispatch certainty."""

        if older_than.total_seconds() <= 0:
            raise ValueError("older_than must be positive")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        now = now or utc_now()
        cutoff = now - older_than
        reconciled = 0
        async with self._session_factory() as session:
            repository = ModelInvocationRepository(session)
            await repository.acquire_projection_write_lock()
            candidates = await repository.list_running_before(cutoff, limit=limit)
            for projection in candidates:
                if projection.lifecycle_status != InvocationLifecycleStatus.RUNNING.value:
                    continue
                dispatch_status = DispatchStatus(projection.dispatch_status)
                if dispatch_status is DispatchStatus.NOT_DISPATCHED:
                    # A STARTED row is written before the provider boundary.  A
                    # crashed worker leaves no fact proving whether dispatch
                    # occurred, so close it as unknown without inventing a
                    # separate dispatch conclusion.
                    dispatch_status = DispatchStatus.DISPATCH_UNKNOWN
                event = self._event_from_projection(
                    projection,
                    event_type=ModelInvocationEventType.OUTCOME_UNKNOWN,
                    dispatch_status=dispatch_status,
                    occurred_at=now,
                    ingested_at=utc_now(),
                    outcome=InvocationOutcome.UNKNOWN,
                    error_code="MODEL_INVOCATION_OUTCOME_UNCONFIRMED",
                    retry_reason=None,
                    metrics=InvocationMetrics(),
                    metadata={"reconciliation_reason": "orphan_timeout"},
                )
                await repository.add_event(event)
                self._apply_terminal_event(projection, event)
                reconciled += 1
            await session.flush()
        return reconciled

    async def _record_dispatch_conclusion(
        self,
        invocation_id: str,
        *,
        status: DispatchStatus,
        event_type: ModelInvocationEventType,
        provider_request_id: str | None,
        occurred_at: datetime | None,
        metadata: dict[str, Any] | None,
    ) -> ModelInvocationProjectionEntity:
        safe_metadata = sanitize_metadata(metadata, event_type=event_type)
        occurred = _validated_occurred_at(occurred_at)
        ingested = utc_now()
        request_hash = hash_provider_request_id(provider_request_id)
        async with self._session_factory() as session:
            repository = ModelInvocationRepository(session)
            await repository.acquire_projection_write_lock()
            await repository.lock_invocation(invocation_id)
            projection = await self._require_projection(
                repository,
                invocation_id,
                for_update=True,
            )
            current = DispatchStatus(projection.dispatch_status)
            if current is status:
                existing = await repository.get_invocation_event(
                    invocation_id,
                    (event_type.value,),
                )
                if existing is None:
                    raise InvalidInvocationTransitionError(
                        "dispatch projection has no matching fact"
                    )
                expected_hash = request_hash
                self._assert_event_facts(
                    existing,
                    occurred_at=occurred_at,
                    metadata=safe_metadata,
                    provider_request_id_hash=expected_hash,
                    compare_provider_request_id_hash=True,
                )
                return projection
            if current is not DispatchStatus.NOT_DISPATCHED:
                raise InvalidInvocationTransitionError(
                    f"dispatch already concluded as {current.value}"
                )
            if projection.lifecycle_status == InvocationLifecycleStatus.TERMINAL.value:
                raise InvalidInvocationTransitionError(
                    "cannot dispatch a terminal invocation"
                )
            if _as_aware(occurred) < _as_aware(projection.started_at):
                raise InvalidInvocationTransitionError(
                    "dispatch occurred_at cannot precede STARTED"
                )
            event = self._event_from_projection(
                projection,
                event_type=event_type,
                dispatch_status=status,
                occurred_at=occurred,
                ingested_at=ingested,
                provider_request_id_hash=request_hash,
                metadata=safe_metadata,
            )
            await repository.add_event(event)
            self._apply_dispatch_event(projection, status=status, event=event)
            await session.flush()
            return projection

    @staticmethod
    async def _require_projection(
        repository: ModelInvocationRepository,
        invocation_id: str,
        *,
        for_update: bool = False,
    ) -> ModelInvocationProjectionEntity:
        projection = await repository.get_projection(
            invocation_id,
            for_update=for_update,
        )
        if projection is None:
            raise InvocationNotFoundError(invocation_id)
        return projection

    @staticmethod
    def _assert_idempotent_start(
        projection: ModelInvocationProjectionEntity,
        context: ModelInvocationContext,
    ) -> None:
        expected = (
            context.invocation_id,
            context.logical_call_id,
            context.attempt_no,
            context.tenant_id,
            context.user_id,
            context.feature_code,
            context.task_id,
            context.run_id,
            context.stage_id,
            context.request_id,
            context.trace_id,
            context.provider,
            context.model_pack_id,
            context.model_pack_version,
            context.model_name,
            context.deployment_name,
            context.provider_region,
            context.privacy_mode.value,
            context.route_type.value,
            context.model_config_id,
            context.service_version,
            context.fallback_from_invocation_id,
        )
        actual = (
            projection.invocation_id,
            projection.logical_call_id,
            projection.attempt_no,
            projection.tenant_id,
            projection.user_id,
            projection.feature_code,
            projection.task_id,
            projection.run_id,
            projection.stage_id,
            projection.request_id,
            projection.trace_id,
            projection.provider,
            projection.model_pack_id,
            projection.model_pack_version,
            projection.model_name,
            projection.deployment_name,
            projection.provider_region,
            projection.privacy_mode,
            projection.route_type,
            projection.model_config_id,
            projection.service_version,
            projection.fallback_from_invocation_id,
        )
        if actual != expected:
            raise DuplicateModelInvocationAttemptError(
                "logical_call_id/attempt_no already belongs to another invocation"
            )

    @staticmethod
    def _assert_event_facts(
        event: ModelInvocationEventEntity,
        *,
        occurred_at: datetime | None,
        metadata: dict[str, Any],
        provider_request_id_hash: str | None = None,
        compare_provider_request_id_hash: bool = False,
    ) -> None:
        if occurred_at is not None and _as_aware(event.occurred_at) != _as_aware(occurred_at):
            raise InvalidInvocationTransitionError(
                "idempotency key was reused with a different occurred_at"
            )
        if dict(event.metadata_json or {}) != metadata:
            raise InvalidInvocationTransitionError(
                "idempotency key was reused with different metadata"
            )
        if (
            compare_provider_request_id_hash
            and event.provider_request_id_hash != provider_request_id_hash
        ):
            raise InvalidInvocationTransitionError(
                "provider request id hash fact is different"
            )

    @classmethod
    def _assert_terminal_facts(
        cls,
        event: ModelInvocationEventEntity,
        *,
        event_type: ModelInvocationEventType,
        outcome: InvocationOutcome,
        error_code: str | None,
        retry_reason: str | None,
        metrics: InvocationMetrics,
        occurred_at: datetime | None,
        provider_request_id_hash: str | None = None,
        compare_provider_request_id_hash: bool = False,
        metadata: dict[str, Any],
    ) -> None:
        cls._assert_event_facts(
            event,
            occurred_at=occurred_at,
            metadata=metadata,
            provider_request_id_hash=provider_request_id_hash,
            compare_provider_request_id_hash=compare_provider_request_id_hash,
        )
        expected = (
            event_type.value,
            outcome.value,
            error_code,
            retry_reason,
            metrics.input_tokens,
            metrics.output_tokens,
            metrics.latency_ms,
            metrics.time_to_first_token_ms,
            metrics.cost_amount,
            metrics.cost_currency,
            metrics.cost_source.value if metrics.cost_source else None,
            metrics.pricing_version,
            _optional_aware(metrics.cost_calculated_at),
        )
        actual = (
            event.event_type,
            event.outcome,
            event.error_code,
            event.retry_reason,
            event.input_token_count,
            event.output_token_count,
            event.latency_ms,
            event.time_to_first_token_ms,
            event.cost_amount,
            event.cost_currency,
            event.cost_source,
            event.pricing_version,
            _optional_aware(event.cost_calculated_at),
        )
        if actual != expected:
            raise InvalidInvocationTransitionError(
                "invocation already has a terminal event with different facts"
            )
    @staticmethod
    def _validate_started_event(event: ModelInvocationEventEntity) -> None:
        if (
            event.event_type != ModelInvocationEventType.STARTED.value
            or event.schema_version != 1
            or event.dispatch_status != DispatchStatus.NOT_DISPATCHED.value
            or event.provider_request_id_hash is not None
            or _event_has_result_payload(event)
        ):
            raise InvalidInvocationTransitionError(
                "STARTED fact contains invalid lifecycle data"
            )
        _validate_stored_hash(event.provider_request_id_hash)
        _validate_stored_metadata(
            event.metadata_json,
            event_type=ModelInvocationEventType.STARTED,
            schema_version=event.schema_version,
        )
        ModelInvocationContext(
            tenant_id=event.tenant_id,
            feature_code=event.feature_code,
            provider=event.provider,
            model_name=event.model_name,
            privacy_mode=PrivacyMode(event.privacy_mode),
            route_type=RouteType(event.route_type),
            attempt_no=event.attempt_no,
            logical_call_id=event.logical_call_id,
            invocation_id=event.invocation_id,
            fallback_from_invocation_id=event.fallback_from_invocation_id,
            user_id=event.user_id,
            task_id=event.task_id,
            run_id=event.run_id,
            stage_id=event.stage_id,
            request_id=event.request_id,
            trace_id=event.trace_id,
            model_pack_id=event.model_pack_id,
            model_pack_version=event.model_pack_version,
            deployment_name=event.deployment_name,
            provider_region=event.provider_region,
            model_config_id=event.model_config_id,
            service_version=event.service_version,
        ).validate()

    @staticmethod
    def _validate_event_against_started(
        started: ModelInvocationEventEntity,
        event: ModelInvocationEventEntity,
    ) -> None:
        expected = (
            1,
            started.logical_call_id,
            started.invocation_id,
            started.attempt_no,
            started.fallback_from_invocation_id,
            started.tenant_id,
            started.user_id,
            started.feature_code,
            started.task_id,
            started.run_id,
            started.stage_id,
            started.request_id,
            started.trace_id,
            started.provider,
            started.model_pack_id,
            started.model_pack_version,
            started.model_name,
            started.deployment_name,
            started.provider_region,
            started.privacy_mode,
            started.route_type,
            started.model_config_id,
            started.service_version,
        )
        actual = (
            event.schema_version,
            event.logical_call_id,
            event.invocation_id,
            event.attempt_no,
            event.fallback_from_invocation_id,
            event.tenant_id,
            event.user_id,
            event.feature_code,
            event.task_id,
            event.run_id,
            event.stage_id,
            event.request_id,
            event.trace_id,
            event.provider,
            event.model_pack_id,
            event.model_pack_version,
            event.model_name,
            event.deployment_name,
            event.provider_region,
            event.privacy_mode,
            event.route_type,
            event.model_config_id,
            event.service_version,
        )
        if actual != expected:
            raise InvalidInvocationTransitionError(
                "event fixed facts differ from STARTED"
            )
        if event.server_sequence <= started.server_sequence:
            raise InvalidInvocationTransitionError(
                "event server sequence is not causal"
            )
        if _as_aware(event.occurred_at) < _as_aware(started.occurred_at):
            raise InvalidInvocationTransitionError(
                "event occurred_at cannot precede STARTED"
            )
        _validate_stored_hash(event.provider_request_id_hash)
        event_type = ModelInvocationEventType(event.event_type)
        _validate_stored_metadata(
            event.metadata_json,
            event_type=event_type,
            schema_version=event.schema_version,
        )
        if event_type in {
            ModelInvocationEventType.DISPATCHED,
            ModelInvocationEventType.DISPATCH_UNKNOWN,
        }:
            expected_status = (
                DispatchStatus.DISPATCHED
                if event_type is ModelInvocationEventType.DISPATCHED
                else DispatchStatus.DISPATCH_UNKNOWN
            )
            if (
                event.dispatch_status != expected_status.value
                or _event_has_result_payload(event)
                or (
                    expected_status is DispatchStatus.DISPATCH_UNKNOWN
                    and event.provider_request_id_hash is not None
                )
            ):
                raise InvalidInvocationTransitionError(
                    "dispatch fact contains invalid lifecycle data"
                )
            return

        if event_type not in TERMINAL_EVENT_TYPES:
            raise InvalidInvocationTransitionError(
                "event type is not part of the lifecycle"
            )
        outcome = InvocationOutcome(str(event.outcome))
        dispatch_status = DispatchStatus(event.dispatch_status)
        validate_terminal_outcome(event_type, outcome, dispatch_status)
        validate_error_fields(event.error_code, event.retry_reason)
        _metrics_from_event(event).validate()
        if dispatch_status is not DispatchStatus.DISPATCHED and (
            event.input_token_count is not None
            or event.output_token_count is not None
            or event.time_to_first_token_ms is not None
            or event.cost_amount is not None
            or event.cost_currency is not None
            or event.cost_source is not None
            or event.pricing_version is not None
            or event.cost_calculated_at is not None
        ):
            raise InvalidInvocationTransitionError(
                "uncertain dispatch cannot contain usage or cost"
            )

    @staticmethod
    def _event_from_projection(
        projection: ModelInvocationProjectionEntity,
        *,
        event_type: ModelInvocationEventType,
        dispatch_status: DispatchStatus,
        occurred_at: datetime,
        ingested_at: datetime,
        outcome: InvocationOutcome | None = None,
        error_code: str | None = None,
        retry_reason: str | None = None,
        metrics: InvocationMetrics | None = None,
        provider_request_id_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ModelInvocationEventEntity:
        metrics = metrics or InvocationMetrics()
        return ModelInvocationEventEntity(
            event_id=new_id("mie"),
            event_type=event_type.value,
            schema_version=1,
            logical_call_id=projection.logical_call_id,
            invocation_id=projection.invocation_id,
            attempt_no=projection.attempt_no,
            dispatch_status=dispatch_status.value,
            fallback_from_invocation_id=projection.fallback_from_invocation_id,
            occurred_at=occurred_at,
            ingested_at=ingested_at,
            tenant_id=projection.tenant_id,
            user_id=projection.user_id,
            feature_code=projection.feature_code,
            task_id=projection.task_id,
            run_id=projection.run_id,
            stage_id=projection.stage_id,
            request_id=projection.request_id,
            trace_id=projection.trace_id,
            provider=projection.provider,
            provider_request_id_hash=(
                provider_request_id_hash
                if provider_request_id_hash is not None
                else projection.provider_request_id_hash
            ),
            model_pack_id=projection.model_pack_id,
            model_pack_version=projection.model_pack_version,
            model_name=projection.model_name,
            deployment_name=projection.deployment_name,
            provider_region=projection.provider_region,
            privacy_mode=projection.privacy_mode,
            route_type=projection.route_type,
            model_config_id=projection.model_config_id,
            input_token_count=metrics.input_tokens,
            output_token_count=metrics.output_tokens,
            latency_ms=metrics.latency_ms,
            time_to_first_token_ms=metrics.time_to_first_token_ms,
            outcome=outcome.value if outcome else None,
            error_code=error_code,
            retry_reason=retry_reason,
            cost_amount=metrics.cost_amount,
            cost_currency=metrics.cost_currency,
            cost_source=metrics.cost_source.value if metrics.cost_source else None,
            pricing_version=metrics.pricing_version,
            cost_calculated_at=metrics.cost_calculated_at,
            service_version=projection.service_version,
            metadata_json=sanitize_metadata(
                metadata,
                event_type=event_type,
            ),
        )

    @staticmethod
    def _apply_terminal(
        projection: ModelInvocationProjectionEntity,
        *,
        event_type: ModelInvocationEventType,
        outcome: InvocationOutcome,
        occurred_at: datetime,
        ingested_at: datetime,
        error_code: str | None,
        retry_reason: str | None,
        metrics: InvocationMetrics,
    ) -> None:
        projection.lifecycle_status = InvocationLifecycleStatus.TERMINAL.value
        projection.outcome = outcome.value
        projection.finished_at = occurred_at
        projection.error_code = error_code
        projection.retry_reason = retry_reason
        projection.input_token_count = metrics.input_tokens
        projection.output_token_count = metrics.output_tokens
        projection.latency_ms = metrics.latency_ms
        projection.time_to_first_token_ms = metrics.time_to_first_token_ms
        projection.cost_amount = metrics.cost_amount
        projection.cost_currency = metrics.cost_currency
        projection.cost_source = (
            metrics.cost_source.value if metrics.cost_source else None
        )
        projection.pricing_version = metrics.pricing_version
        projection.cost_calculated_at = metrics.cost_calculated_at
        projection.projection_version += 1
        projection.data_as_of = ingested_at
        projection.ingested_at = ingested_at

    @staticmethod
    def _projection_from_started(
        event: ModelInvocationEventEntity,
    ) -> ModelInvocationProjectionEntity:
        return ModelInvocationProjectionEntity(
            started_sequence=event.server_sequence,
            invocation_id=event.invocation_id,
            projection_version=1,
            data_as_of=event.ingested_at,
            logical_call_id=event.logical_call_id,
            attempt_no=event.attempt_no,
            fallback_from_invocation_id=event.fallback_from_invocation_id,
            tenant_id=event.tenant_id,
            user_id=event.user_id,
            feature_code=event.feature_code,
            task_id=event.task_id,
            run_id=event.run_id,
            stage_id=event.stage_id,
            request_id=event.request_id,
            trace_id=event.trace_id,
            started_at=event.occurred_at,
            ingested_at=event.ingested_at,
            lifecycle_status=InvocationLifecycleStatus.RUNNING.value,
            dispatch_status=event.dispatch_status,
            provider=event.provider,
            provider_request_id_hash=event.provider_request_id_hash,
            model_pack_id=event.model_pack_id,
            model_pack_version=event.model_pack_version,
            model_name=event.model_name,
            deployment_name=event.deployment_name,
            provider_region=event.provider_region,
            privacy_mode=event.privacy_mode,
            route_type=event.route_type,
            model_config_id=event.model_config_id,
            service_version=event.service_version,
        )

    @staticmethod
    def _apply_dispatch_event(
        projection: ModelInvocationProjectionEntity,
        *,
        status: DispatchStatus,
        event: ModelInvocationEventEntity,
    ) -> None:
        if projection.dispatch_status != DispatchStatus.NOT_DISPATCHED.value:
            raise InvalidInvocationTransitionError(
                f"duplicate dispatch conclusion for {event.invocation_id}"
            )
        if projection.lifecycle_status != InvocationLifecycleStatus.RUNNING.value:
            raise InvalidInvocationTransitionError(
                f"dispatch after terminal for {event.invocation_id}"
            )
        if event.dispatch_status != status.value:
            raise InvalidInvocationTransitionError(
                "dispatch event status does not match its type"
            )
        if _as_aware(event.occurred_at) < _as_aware(projection.started_at):
            raise InvalidInvocationTransitionError(
                "dispatch occurred_at cannot precede STARTED"
            )
        _validate_stored_hash(event.provider_request_id_hash)
        projection.dispatch_sequence = event.server_sequence
        projection.dispatch_status = status.value
        projection.provider_request_id_hash = event.provider_request_id_hash
        projection.projection_version += 1
        projection.data_as_of = event.ingested_at
        projection.ingested_at = event.ingested_at

    @staticmethod
    def _apply_terminal_event(
        projection: ModelInvocationProjectionEntity,
        event: ModelInvocationEventEntity,
    ) -> None:
        if projection.lifecycle_status != InvocationLifecycleStatus.RUNNING.value:
            raise InvalidInvocationTransitionError(
                "cannot apply a second terminal event"
            )
        direct_unknown = (
            projection.dispatch_status == DispatchStatus.NOT_DISPATCHED.value
            and event.event_type
            == ModelInvocationEventType.OUTCOME_UNKNOWN.value
            and event.dispatch_status == DispatchStatus.DISPATCH_UNKNOWN.value
        )
        if (
            projection.dispatch_status != event.dispatch_status
            and not direct_unknown
        ):
            raise InvalidInvocationTransitionError(
                "terminal dispatch status differs from prior facts"
            )
        if _as_aware(event.occurred_at) < _as_aware(projection.started_at):
            raise InvalidInvocationTransitionError(
                "terminal occurred_at cannot precede STARTED"
            )
        if (
            projection.provider_request_id_hash is not None
            and event.provider_request_id_hash
            != projection.provider_request_id_hash
        ):
            raise InvalidInvocationTransitionError(
                "terminal cannot clear or change provider request id hash"
            )
        _validate_stored_hash(event.provider_request_id_hash)
        projection.terminal_sequence = event.server_sequence
        projection.lifecycle_status = InvocationLifecycleStatus.TERMINAL.value
        projection.dispatch_status = event.dispatch_status
        projection.outcome = event.outcome
        projection.finished_at = event.occurred_at
        projection.error_code = event.error_code
        projection.retry_reason = event.retry_reason
        projection.input_token_count = event.input_token_count
        projection.output_token_count = event.output_token_count
        projection.latency_ms = event.latency_ms
        projection.time_to_first_token_ms = event.time_to_first_token_ms
        projection.cost_amount = event.cost_amount
        projection.cost_currency = event.cost_currency
        projection.cost_source = event.cost_source
        projection.pricing_version = event.pricing_version
        projection.cost_calculated_at = event.cost_calculated_at
        projection.provider_request_id_hash = event.provider_request_id_hash
        projection.projection_version += 1
        projection.data_as_of = event.ingested_at
        projection.ingested_at = event.ingested_at


def _metrics_have_usage_or_cost(metrics: InvocationMetrics) -> bool:
    return any(
        value is not None
        for value in (
            metrics.input_tokens,
            metrics.output_tokens,
            metrics.time_to_first_token_ms,
            metrics.cost_amount,
            metrics.cost_currency,
            metrics.cost_source,
            metrics.pricing_version,
            metrics.cost_calculated_at,
        )
    )


_HASHED_PROVIDER_REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")


def _validated_occurred_at(value: datetime | None) -> datetime:
    occurred = value or utc_now()
    if (
        not isinstance(occurred, datetime)
        or occurred.tzinfo is None
        or occurred.utcoffset() is None
    ):
        raise ValueError("occurred_at must be timezone-aware")
    return occurred


def _validate_stored_hash(value: str | None) -> None:
    if value is not None and (
        type(value) is not str
        or _HASHED_PROVIDER_REQUEST_ID.fullmatch(value) is None
    ):
        raise InvalidInvocationTransitionError(
            "provider_request_id_hash is not canonical"
        )


def _validate_stored_metadata(
    value: Any,
    *,
    event_type: ModelInvocationEventType,
    schema_version: int,
) -> None:
    if type(value) is not dict:
        raise InvalidInvocationTransitionError(
            "metadata_json must be an object"
        )
    try:
        cleaned = sanitize_metadata(
            value,
            event_type=event_type,
            schema_version=schema_version,
        )
    except ValueError:
        raise InvalidInvocationTransitionError(
            "metadata_json contains unregistered data"
        ) from None
    if cleaned != value:
        raise InvalidInvocationTransitionError(
            "metadata_json is not canonical"
        )


def _event_has_result_payload(event: ModelInvocationEventEntity) -> bool:
    return any(
        value is not None
        for value in (
            event.input_token_count,
            event.output_token_count,
            event.latency_ms,
            event.time_to_first_token_ms,
            event.outcome,
            event.error_code,
            event.retry_reason,
            event.cost_amount,
            event.cost_currency,
            event.cost_source,
            event.pricing_version,
            event.cost_calculated_at,
        )
    )


def _metrics_from_event(
    event: ModelInvocationEventEntity,
) -> InvocationMetrics:
    return InvocationMetrics(
        input_tokens=event.input_token_count,
        output_tokens=event.output_token_count,
        latency_ms=event.latency_ms,
        time_to_first_token_ms=event.time_to_first_token_ms,
        cost_amount=event.cost_amount,
        cost_currency=event.cost_currency,
        cost_source=(
            CostSource(event.cost_source)
            if event.cost_source is not None
            else None
        ),
        pricing_version=event.pricing_version,
        cost_calculated_at=event.cost_calculated_at,
    )


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=utc_now().tzinfo)


def _optional_aware(value: datetime | None) -> datetime | None:
    return _as_aware(value) if value is not None else None
