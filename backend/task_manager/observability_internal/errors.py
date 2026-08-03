from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class InternalObservabilityError(Exception):
    """Stable internal API failure that never retains request credentials."""

    status_code: int
    code: str
    request_id: str
    retryable: bool = False

    def __post_init__(self) -> None:
        # Exception args are deliberately capability-free. Do not add the
        # rejected token, URL or query string to this object or its repr.
        Exception.__init__(self, self.code)

    def as_payload(self) -> dict[str, object]:
        return {
            "code": self.code,
            "requestId": self.request_id,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:
        return (
            "InternalObservabilityError("
            f"status_code={self.status_code}, code={self.code!r}, "
            f"request_id={self.request_id!r}, retryable={self.retryable})"
        )


@dataclass(slots=True)
class ResourceAccessError(InternalObservabilityError):
    """Uniform external 404 with a private, auditable authorization result."""

    audit_result: str = "NOT_FOUND"

    def __post_init__(self) -> None:
        if self.audit_result not in {"DENIED", "NOT_FOUND"}:
            raise ValueError("unsupported resource access audit result")
        InternalObservabilityError.__post_init__(self)


def invalid_request(
    request_id: str, code: str = "OBSERVABILITY_QUERY_INVALID"
) -> InternalObservabilityError:
    return InternalObservabilityError(400, code, request_id)


def not_found(request_id: str) -> InternalObservabilityError:
    return InternalObservabilityError(
        404, "OBSERVABILITY_RESOURCE_NOT_FOUND", request_id
    )


def invisible(request_id: str, *, exists: bool) -> ResourceAccessError:
    return ResourceAccessError(
        status_code=404,
        code="OBSERVABILITY_RESOURCE_NOT_FOUND",
        request_id=request_id,
        retryable=False,
        audit_result="DENIED" if exists else "NOT_FOUND",
    )


def invalid_source(request_id: str) -> InternalObservabilityError:
    return InternalObservabilityError(
        500, "OBSERVABILITY_SOURCE_DATA_INVALID", request_id
    )


def audit_write_required(request_id: str) -> InternalObservabilityError:
    return InternalObservabilityError(
        503, "SECURITY_AUDIT_WRITE_REQUIRED", request_id, retryable=False
    )
