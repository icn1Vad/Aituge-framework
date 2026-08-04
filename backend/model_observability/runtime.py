from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from .domain import (
    DispatchStatus,
    DuplicateModelInvocationAttemptError,
    InvocationMetrics,
    InvocationOutcome,
    ModelInvocationContext,
    ModelInvocationEventType,
    PrivacyMode,
    RouteType,
    new_id,
)
from .lifecycle import ModelInvocationLifecycleService

logger = logging.getLogger(__name__)
_FINALIZE_IDENTITY_CONFLICT_CODES = frozenset(
    {
        "MODEL_INVOCATION_DUPLICATE_ATTEMPT",
        "MODEL_INVOCATION_IDENTITY_CONFLICT",
        "MODEL_INVOCATION_INVALID_TRANSITION",
        "MODEL_INVOCATION_LOGICAL_CALL_CONFLICT",
    }
)


class RuntimeObservationFinalizeError(RuntimeError):
    code = "MODEL_OBSERVABILITY_FINALIZE_FAILED"


@dataclass(frozen=True, slots=True)
class RuntimeObservabilityContext:
    feature_code: str = "framework.llm"
    privacy_mode: PrivacyMode = PrivacyMode.STANDARD
    logical_call_id: str | None = None
    attempt_no: int = 1
    fallback_from_invocation_id: str | None = None
    user_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    stage_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    service_version: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeModelDescriptor:
    tenant_id: str
    provider: str
    model_name: str
    route_type: RouteType
    model_pack_id: str | None
    model_pack_version: str | None = None
    deployment_name: str | None = None
    provider_region: str | None = None
    model_config_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeInvocationHandle:
    invocation_id: str
    logical_call_id: str
    attempt_no: int


class ModelInvocationRuntimeRecorder(Protocol):
    async def begin(
        self,
        descriptor: RuntimeModelDescriptor,
        context: RuntimeObservabilityContext,
    ) -> RuntimeInvocationHandle: ...

    async def dispatched(
        self,
        handle: RuntimeInvocationHandle,
        *,
        provider_request_id: str | None = None,
    ) -> None: ...

    async def failed(
        self,
        handle: RuntimeInvocationHandle,
        *,
        dispatch_status: DispatchStatus,
        error_code: str,
        metrics: InvocationMetrics,
    ) -> None: ...

    async def succeeded(
        self,
        handle: RuntimeInvocationHandle,
        *,
        metrics: InvocationMetrics,
        provider_request_id: str | None = None,
        finish_reason: str | None = None,
    ) -> None: ...
    async def denied(
        self,
        handle: RuntimeInvocationHandle,
        *,
        metrics: InvocationMetrics,
        guardrail_code: str,
        provider_request_id: str | None = None,
    ) -> None: ...

    async def validation_failed(
        self,
        handle: RuntimeInvocationHandle,
        *,
        metrics: InvocationMetrics,
        validation_code: str,
        provider_request_id: str | None = None,
    ) -> None: ...

@dataclass(slots=True)
class RuntimeInvocationFinalizer:
    """One-shot decision for a Provider-completed response awaiting local validation."""

    recorder: ModelInvocationRuntimeRecorder
    handle: RuntimeInvocationHandle
    metrics: InvocationMetrics
    provider_request_id: str | None = None
    finish_reason: str | None = None
    _decision: str | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def invocation_id(self) -> str:
        return self.handle.invocation_id

    async def succeed(self) -> None:
        async def write() -> None:
            await self.recorder.succeeded(
                self.handle,
                metrics=self.metrics,
                provider_request_id=self.provider_request_id,
                finish_reason=self.finish_reason,
            )

        await self._decide("SUCCESS", write)

    async def deny(self, guardrail_code: str = "OUTPUT_POLICY_REJECTED") -> None:
        async def write() -> None:
            await self.recorder.denied(
                self.handle,
                metrics=self.metrics,
                guardrail_code=guardrail_code,
                provider_request_id=self.provider_request_id,
            )

        await self._decide(f"DENIED:{guardrail_code}", write)

    async def validation_failed(
        self,
        validation_code: str = "MODEL_OUTPUT_SCHEMA_INVALID",
    ) -> None:
        async def write() -> None:
            await self.recorder.validation_failed(
                self.handle,
                metrics=self.metrics,
                validation_code=validation_code,
                provider_request_id=self.provider_request_id,
            )

        await self._decide(f"VALIDATION_FAILED:{validation_code}", write)

    async def _decide(self, decision: str, write) -> None:
        async with self._lock:
            if self._decision is not None:
                if self._decision == decision:
                    return
                raise RuntimeError(
                    f"model invocation already finalized as {self._decision}"
                )
            await write()
            self._decision = decision


async def finalize_deferred_completion_success(completion: Any) -> None:
    finalizer = getattr(completion, "terminal_finalizer", None)
    if finalizer is None:
        return
    await _write_deferred_terminal("success", finalizer.succeed)


async def finalize_deferred_completion_validation_failed(
    completion: Any,
    validation_code: str,
) -> None:
    finalizer = getattr(completion, "terminal_finalizer", None)
    if finalizer is None:
        return

    async def write() -> None:
        await finalizer.validation_failed(validation_code)

    await _write_deferred_terminal("validation_failed", write)


async def _write_deferred_terminal(operation: str, write) -> None:
    try:
        await write()
    except Exception as exc:
        if _is_finalize_identity_conflict(exc):
            raise RuntimeObservationFinalizeError(
                "Model observation identity conflict"
            ) from None
        logger.warning(
            "model_observability_finalize_degraded operation=%s error_type=%s",
            operation,
            exc.__class__.__name__,
        )


def _is_finalize_identity_conflict(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _FINALIZE_IDENTITY_CONFLICT_CODES:
        return True
    if exc.__class__.__name__ == "DuplicateModelInvocationAttemptError":
        return True
    return str(exc).startswith("model invocation already finalized as ")

class ModelRequestNotDispatchedError(RuntimeError):
    """Marker for a local boundary failure that proves no execution occurred."""



class DatabaseModelInvocationRuntimeRecorder:
    def __init__(self, lifecycle: ModelInvocationLifecycleService) -> None:
        self._lifecycle = lifecycle

    async def begin(
        self,
        descriptor: RuntimeModelDescriptor,
        context: RuntimeObservabilityContext,
    ) -> RuntimeInvocationHandle:
        logical_call_id = context.logical_call_id or new_id("logical")
        invocation_id = new_id("inv")
        model_context = ModelInvocationContext(
            tenant_id=descriptor.tenant_id,
            feature_code=context.feature_code,
            provider=descriptor.provider,
            model_name=descriptor.model_name,
            privacy_mode=context.privacy_mode,
            route_type=descriptor.route_type,
            attempt_no=context.attempt_no,
            logical_call_id=logical_call_id,
            invocation_id=invocation_id,
            fallback_from_invocation_id=context.fallback_from_invocation_id,
            user_id=context.user_id,
            task_id=context.task_id,
            run_id=context.run_id,
            stage_id=context.stage_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            model_pack_id=descriptor.model_pack_id,
            model_pack_version=descriptor.model_pack_version,
            deployment_name=descriptor.deployment_name,
            provider_region=descriptor.provider_region,
            model_config_id=descriptor.model_config_id,
            service_version=context.service_version,
        )
        projection = await self._lifecycle.start_invocation(model_context)
        if projection.invocation_id != invocation_id:
            raise DuplicateModelInvocationAttemptError(
                "logical_call_id and attempt_no were already started"
            )
        return RuntimeInvocationHandle(
            invocation_id=invocation_id,
            logical_call_id=logical_call_id,
            attempt_no=context.attempt_no,
        )

    async def dispatched(
        self,
        handle: RuntimeInvocationHandle,
        *,
        provider_request_id: str | None = None,
    ) -> None:
        await self._lifecycle.mark_dispatched(
            handle.invocation_id,
            provider_request_id=provider_request_id,
        )

    async def failed(
        self,
        handle: RuntimeInvocationHandle,
        *,
        dispatch_status: DispatchStatus,
        error_code: str,
        metrics: InvocationMetrics,
    ) -> None:
        if dispatch_status is DispatchStatus.DISPATCHED:
            await self._lifecycle.mark_dispatched(handle.invocation_id)
        elif dispatch_status is DispatchStatus.DISPATCH_UNKNOWN:
            await self._lifecycle.mark_dispatch_unknown(handle.invocation_id)
        await self._lifecycle.terminate(
            handle.invocation_id,
            event_type=ModelInvocationEventType.FAILED,
            outcome=(
                InvocationOutcome.TIMEOUT
                if error_code == "MODEL_PROVIDER_TIMEOUT"
                else InvocationOutcome.FAILURE
            ),
            error_code=error_code,
            metrics=metrics,
        )

    async def succeeded(
        self,
        handle: RuntimeInvocationHandle,
        *,
        metrics: InvocationMetrics,
        provider_request_id: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        await self._lifecycle.mark_dispatched(
            handle.invocation_id,
            provider_request_id=provider_request_id,
        )
        await self._lifecycle.terminate(
            handle.invocation_id,
            event_type=ModelInvocationEventType.SUCCEEDED,
            outcome=InvocationOutcome.SUCCESS,
            metrics=metrics,
            provider_request_id=provider_request_id,
            metadata=(
                {"finish_reason": finish_reason}
                if finish_reason is not None
                else {}
            ),
        )

    async def denied(
        self,
        handle: RuntimeInvocationHandle,
        *,
        metrics: InvocationMetrics,
        guardrail_code: str,
        provider_request_id: str | None = None,
    ) -> None:
        await self._lifecycle.mark_dispatched(
            handle.invocation_id,
            provider_request_id=provider_request_id,
        )
        await self._lifecycle.terminate(
            handle.invocation_id,
            event_type=ModelInvocationEventType.OUTPUT_GUARDRAIL_REJECTED,
            outcome=InvocationOutcome.DENIED,
            metrics=metrics,
            provider_request_id=provider_request_id,
            metadata={"guardrail_code": guardrail_code},
        )

    async def validation_failed(
        self,
        handle: RuntimeInvocationHandle,
        *,
        metrics: InvocationMetrics,
        validation_code: str,
        provider_request_id: str | None = None,
    ) -> None:
        await self._lifecycle.mark_dispatched(
            handle.invocation_id,
            provider_request_id=provider_request_id,
        )
        await self._lifecycle.terminate(
            handle.invocation_id,
            event_type=ModelInvocationEventType.VALIDATION_FAILED,
            outcome=InvocationOutcome.FAILURE,
            error_code=validation_code,
            metrics=metrics,
            provider_request_id=provider_request_id,
            metadata={"schema_validation_code": validation_code},
        )

def _safe_provider_status_code(exc: BaseException) -> int | None:
    try:
        raw = getattr(exc, "status_code", None)
    except Exception:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        status_code = raw
    elif (
        isinstance(raw, str)
        and len(raw) == 3
        and raw.isascii()
        and raw.isdigit()
    ):
        status_code = int(raw)
    else:
        return None
    return status_code if 100 <= status_code <= 599 else None


def classify_dispatch_exception(exc: BaseException) -> DispatchStatus:
    """Choose NOT_DISPATCHED only with proof; otherwise preserve uncertainty."""

    if isinstance(exc, ModelRequestNotDispatchedError):
        return DispatchStatus.NOT_DISPATCHED
    try:
        response_present = getattr(exc, "response", None) is not None
    except Exception:
        response_present = False
    # An HTTP status response proves that the provider accepted the transport.
    if _safe_provider_status_code(exc) is not None and response_present:
        return DispatchStatus.DISPATCHED
    return DispatchStatus.DISPATCH_UNKNOWN


def stable_provider_error_code(exc: BaseException) -> str:
    if isinstance(exc, ModelRequestNotDispatchedError):
        return "MODEL_REQUEST_NOT_DISPATCHED"
    if isinstance(exc, TimeoutError):
        return "MODEL_PROVIDER_TIMEOUT"
    name = exc.__class__.__name__.lower()
    if "timeout" in name:
        return "MODEL_PROVIDER_TIMEOUT"
    if _safe_provider_status_code(exc) is not None:
        return "MODEL_PROVIDER_HTTP_ERROR"
    return "MODEL_PROVIDER_DISPATCH_ERROR"


def recorder_from_environment() -> ModelInvocationRuntimeRecorder | None:
    enabled = os.getenv("MODEL_INVOCATION_LEDGER_ENABLED", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    # Imported lazily so an unregistered branch cannot mutate the root DB schema.
    from db.db_context import create_db_session

    return DatabaseModelInvocationRuntimeRecorder(
        ModelInvocationLifecycleService(create_db_session)
    )
