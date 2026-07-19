from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


FRAMEWORK_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class FrameworkExecutionRequest:
    review_id: str
    attempt_no: int
    tenant_id: str
    user_id: str
    business_task_id: str
    contract_version_id: str
    document_id: str
    perspective: str
    our_party_name: str | None
    contract_type: str
    review_attitude: str
    schema_version: str

    @property
    def task_idempotency_key(self) -> str:
        return f"contract-review:{self.review_id}:attempt:{self.attempt_no}"

    @property
    def run_idempotency_key(self) -> str:
        return f"{self.task_idempotency_key}:run"


@dataclass(frozen=True, slots=True)
class FrameworkRunSnapshot:
    task_id: str
    run_id: str
    status: str
    current_stage_id: str | None
    cancel_requested: bool
    updated_at: datetime

    @property
    def terminal(self) -> bool:
        return self.status in FRAMEWORK_TERMINAL_STATUSES


class FrameworkGateway(Protocol):
    def create_execution(self, request: FrameworkExecutionRequest) -> FrameworkRunSnapshot: ...

    def get_run(
        self,
        task_id: str,
        run_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> FrameworkRunSnapshot: ...

    def cancel_run(
        self,
        task_id: str,
        run_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> FrameworkRunSnapshot: ...


class FrameworkGatewayError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class FrameworkUnavailableError(FrameworkGatewayError):
    def __init__(self, message: str = "Framework is temporarily unavailable") -> None:
        super().__init__(
            message,
            code="FRAMEWORK_UNAVAILABLE",
            status_code=503,
            retryable=True,
        )


class FrameworkTimeoutError(FrameworkGatewayError):
    def __init__(self, message: str = "Framework request timed out") -> None:
        super().__init__(
            message,
            code="FRAMEWORK_TIMEOUT",
            status_code=504,
            retryable=True,
        )


class FrameworkProtocolError(FrameworkGatewayError):
    def __init__(self, message: str = "Framework returned an invalid response") -> None:
        super().__init__(
            message,
            code="FRAMEWORK_PROTOCOL_ERROR",
            status_code=502,
            retryable=False,
        )
