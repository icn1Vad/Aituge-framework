from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from services.contract.capabilities.model_observation import (
    ModelObservationFinalizeError,
    finalize_completion_success,
    finalize_completion_validation_failed,
)


_SECRET = "MODEL_RAW_RESPONSE_SECRET_CANARY"


class _FailingFinalizer:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: list[tuple[str, str | None]] = []

    async def succeed(self) -> None:
        self.calls.append(("SUCCESS", None))
        raise self.exc

    async def validation_failed(self, code: str) -> None:
        self.calls.append(("VALIDATION_FAILED", code))
        raise self.exc


class _IdentityConflict(RuntimeError):
    code = "MODEL_INVOCATION_DUPLICATE_ATTEMPT"


@pytest.mark.asyncio
async def test_success_recorder_failure_preserves_business_result_and_redacts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    finalizer = _FailingFinalizer(RuntimeError(_SECRET))
    completion = SimpleNamespace(terminal_finalizer=finalizer)
    caplog.set_level(
        logging.WARNING,
        logger="services.contract.capabilities.model_observation",
    )

    await finalize_completion_success(completion)
    business_result = {"status": "COMPLETED"}

    assert business_result == {"status": "COMPLETED"}
    assert finalizer.calls == [("SUCCESS", None)]
    assert _SECRET not in caplog.text
    assert "model_observability_finalize_degraded" in caplog.text


@pytest.mark.asyncio
async def test_validation_recorder_failure_preserves_validation_semantics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    finalizer = _FailingFinalizer(RuntimeError(_SECRET))
    completion = SimpleNamespace(terminal_finalizer=finalizer)
    caplog.set_level(
        logging.WARNING,
        logger="services.contract.capabilities.model_observation",
    )

    await finalize_completion_validation_failed(
        completion,
        "MODEL_OUTPUT_SCHEMA_INVALID",
    )
    business_error_code = "CONTRACT_OUTPUT_INVALID"

    assert business_error_code == "CONTRACT_OUTPUT_INVALID"
    assert finalizer.calls == [
        ("VALIDATION_FAILED", "MODEL_OUTPUT_SCHEMA_INVALID")
    ]
    assert _SECRET not in caplog.text


@pytest.mark.asyncio
async def test_cancel_recorder_failure_does_not_replace_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    finalizer = _FailingFinalizer(RuntimeError(_SECRET))
    completion = SimpleNamespace(terminal_finalizer=finalizer)
    caplog.set_level(
        logging.WARNING,
        logger="services.contract.capabilities.model_observation",
    )

    async def cancelled_operation() -> None:
        try:
            raise asyncio.CancelledError
        except asyncio.CancelledError:
            await finalize_completion_validation_failed(
                completion,
                "MODEL_OUTPUT_PROCESSING_CANCELLED",
            )
            raise

    with pytest.raises(asyncio.CancelledError):
        await cancelled_operation()

    assert finalizer.calls == [
        ("VALIDATION_FAILED", "MODEL_OUTPUT_PROCESSING_CANCELLED")
    ]
    assert _SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "MODEL_INVOCATION_DUPLICATE_ATTEMPT",
        "MODEL_INVOCATION_IDENTITY_CONFLICT",
        "MODEL_INVOCATION_INVALID_TRANSITION",
    ],
)
async def test_identity_conflict_remains_fail_closed_without_retrying_terminal(
    code: str,
) -> None:
    conflict = _IdentityConflict("cross-wire")
    conflict.code = code
    finalizer = _FailingFinalizer(conflict)
    completion = SimpleNamespace(terminal_finalizer=finalizer)

    with pytest.raises(ModelObservationFinalizeError):
        await finalize_completion_success(completion)

    assert finalizer.calls == [("SUCCESS", None)]
