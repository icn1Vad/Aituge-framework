from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)
_IDENTITY_CONFLICT_CODES = frozenset(
    {
        "MODEL_INVOCATION_DUPLICATE_ATTEMPT",
        "MODEL_INVOCATION_IDENTITY_CONFLICT",
        "MODEL_INVOCATION_INVALID_TRANSITION",
        "MODEL_INVOCATION_LOGICAL_CALL_CONFLICT",
        "PROOF_MODEL_DUPLICATE_ATTEMPT",
        "PROOF_MODEL_FALLBACK_INVALID",
    }
)


class ModelObservationFinalizeError(RuntimeError):
    code = "MODEL_OBSERVABILITY_FINALIZE_FAILED"


def deferred_completion_kwargs(
    previous_completion: Any | None,
    *,
    repair_no: int,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"defer_terminal": True}
    if previous_completion is None:
        return kwargs
    try:
        logical_call_id = getattr(previous_completion, "logical_call_id", None)
        invocation_id = getattr(previous_completion, "invocation_id", None)
        model_attempt_no = getattr(previous_completion, "model_attempt_no", None)
        finalizer = getattr(previous_completion, "terminal_finalizer", None)
    except Exception:
        raise ModelObservationFinalizeError(
            "Instrumented repair identity is unavailable"
        ) from None
    identity_valid = (
        isinstance(logical_call_id, str)
        and bool(logical_call_id)
        and isinstance(invocation_id, str)
        and bool(invocation_id)
        and isinstance(model_attempt_no, int)
        and not isinstance(model_attempt_no, bool)
        and model_attempt_no >= 1
    )
    if not identity_valid:
        if finalizer is None:
            return kwargs
        raise ModelObservationFinalizeError(
            "Instrumented repair identity is unavailable"
        )
    kwargs.update(
        logical_call_id=logical_call_id,
        model_attempt_no=model_attempt_no + 1,
        fallback_from_invocation_id=invocation_id,
    )
    return kwargs


async def finalize_completion_success(completion: Any) -> None:
    finalizer = _terminal_finalizer(completion)
    if finalizer is None:
        return
    await _write_terminal_fail_open(
        "success",
        finalizer.succeed,
    )


async def finalize_completion_validation_failed(
    completion: Any,
    validation_code: str,
) -> None:
    finalizer = _terminal_finalizer(completion)
    if finalizer is None:
        return

    async def write() -> None:
        await finalizer.validation_failed(validation_code)

    await _write_terminal_fail_open("validation_failed", write)


def _terminal_finalizer(completion: Any) -> Any | None:
    try:
        return getattr(completion, "terminal_finalizer", None)
    except Exception:
        logger.warning(
            "model_observability_finalize_degraded operation=lookup "
            "error_type=attribute_access"
        )
        return None


async def _write_terminal_fail_open(operation: str, write) -> None:
    try:
        await write()
    except Exception as exc:
        if _is_identity_conflict(exc):
            raise ModelObservationFinalizeError(
                "Model observation identity conflict"
            ) from None
        logger.warning(
            "model_observability_finalize_degraded operation=%s error_type=%s",
            operation,
            exc.__class__.__name__,
        )


def _is_identity_conflict(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _IDENTITY_CONFLICT_CODES:
        return True
    if exc.__class__.__name__ == "DuplicateModelInvocationAttemptError":
        return True
    message = str(exc)
    return message.startswith("model invocation already finalized as ")
