from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from contract.application.idempotency import build_framework_request_fingerprint


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
    model_pack_id: str = "api-rerank"
    execution_mode: str = "FULL_REVIEW"
    party_resolution_id: str | None = None
    confirmed_party_a_name: str | None = None
    confirmed_party_b_name: str | None = None
    primary_playbook_id: str | None = None
    selected_playbook_ids: tuple[str, ...] = ("base_neutral",)
    roles_by_playbook: dict[str, str] | None = None
    rule_release_id: str | None = None

    @property
    def task_idempotency_key(self) -> str:
        prefix = "contract-party-resolution" if self.execution_mode == "PARTY_RESOLUTION" else "contract-review"
        return f"{prefix}:{self.review_id}:attempt:{self.attempt_no}"

    @property
    def run_idempotency_key(self) -> str:
        return f"{self.task_idempotency_key}:run"

    @property
    def request_fingerprint(self) -> str:
        return build_framework_request_fingerprint(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            review_id=self.review_id,
            attempt_no=self.attempt_no,
            business_task_id=self.business_task_id,
            contract_version_id=self.contract_version_id,
            party_resolution_id=self.party_resolution_id,
            document_id=self.document_id,
            perspective=self.perspective,
            our_party_name=self.our_party_name,
            contract_type=self.contract_type,
            review_attitude=self.review_attitude,
            schema_version=self.schema_version,
            execution_mode=self.execution_mode,
            confirmed_party_a_name=self.confirmed_party_a_name,
            confirmed_party_b_name=self.confirmed_party_b_name,
            primary_playbook_id=self.primary_playbook_id,
            selected_playbook_ids=self.selected_playbook_ids,
            roles_by_playbook=self.roles_by_playbook,
            rule_release_id=self.rule_release_id,
        )

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
