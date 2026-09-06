from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contract.application.framework_gateway import (
    FrameworkExecutionRequest,
    FrameworkGateway,
    FrameworkProtocolError,
    FrameworkRunSnapshot,
    FrameworkTimeoutError,
    FrameworkUnavailableError,
)
from contract.config import Settings
from contract.observability import contract_span, inject_trace_headers


TASK_TYPE = "contract.review.run"
PARTY_RESOLUTION_TASK_TYPE = "contract.party-resolution.run"
KNOWN_RUN_STATUSES = frozenset(
    {
        "pending",
        "queued",
        "running",
        "paused",
        "waiting_human",
        "succeeded",
        "failed",
        "cancelled",
    }
)
STAGE_MAPPING = {
    "parse_contract": "PARSING",
    "resolve_parties": "PARTY_RESOLUTION",
    "extract_contract_ir": "IR_EXTRACTION",
    "rights_obligations_review": "RIGHTS_OBLIGATIONS",
    "commercial_terms_review": "RISK_REVIEW",
    "liability_termination_review": "RISK_REVIEW",
    "missing_ambiguous_clauses": "RISK_REVIEW",
    "relation_extraction": "RISK_REVIEW",
    "verify_evidence": "EVIDENCE_VERIFICATION",
    "finalize_review": "FINALIZING",
}


class _FrameworkModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _TaskRecord(_FrameworkModel):
    id: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    model_pack_id: str = Field(min_length=1)
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class _TaskEnvelope(_FrameworkModel):
    task: _TaskRecord


class _RunStart(_FrameworkModel):
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: str = Field(min_length=1)


class _RunRecord(_FrameworkModel):
    id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    current_stage_id: str | None = None
    cancel_requested: bool = False
    updated_at: datetime


class _RunEnvelope(_FrameworkModel):
    run: _RunRecord


class FrameworkHttpGateway(FrameworkGateway):
    """Synchronous adapter for the existing TaskManager v1 HTTP API."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = settings.framework_base_url.rstrip("/")
        self.connect_timeout = settings.framework_connect_timeout_seconds
        self.read_timeout = settings.framework_read_timeout_seconds
        self.cancel_wait = settings.framework_cancel_wait_seconds
        self.transport = transport

    def create_execution(self, request: FrameworkExecutionRequest) -> FrameworkRunSnapshot:
        headers = self._headers(request.tenant_id, request.user_id, review_id=request.review_id, task_id=request.business_task_id)
        task_type = self._task_type(request)
        title = (
            f"Contract party resolution {request.contract_version_id}"
            if request.execution_mode == "PARTY_RESOLUTION"
            else f"Contract review {request.business_task_id}"
        )
        task_payload = {
            "task_type": task_type,
            "title": title,
            "model_pack_id": request.model_pack_id,
            "input_payload": {
                "schema_version": request.schema_version,
                "review_id": request.review_id,
                "attempt_no": request.attempt_no,
                "business_task_id": request.business_task_id,
                "contract_version_id": request.contract_version_id,
                "party_resolution_id": request.party_resolution_id,
                "document_id": request.document_id,
                "perspective": request.perspective,
                "our_party_name": request.our_party_name,
                "execution_mode": request.execution_mode,
                "confirmed_party_a_name": request.confirmed_party_a_name,
                "confirmed_party_b_name": request.confirmed_party_b_name,
                "contract_type": request.contract_type,
                "review_attitude": request.review_attitude,
                "rule_review_standard": request.rule_review_standard,
            },
            "stream": False,
            "metadata": {
                "source_service": "ai-contract",
                "schema_version": request.schema_version,
                "contract_review_id": request.review_id,
                "contract_attempt_no": request.attempt_no,
                "contract_request_fingerprint": request.request_fingerprint,
            },
        }
        task_data = self._request_json(
            "POST",
            "/task-manager/tasks",
            headers={**headers, "Idempotency-Key": request.task_idempotency_key},
            json=task_payload,
        )
        task = self._validate(_TaskEnvelope, task_data, "task creation response").task
        self._validate_task(task, request)

        run_data = self._request_json(
            "POST",
            f"/task-manager/tasks/{task.id}/runs",
            headers={**headers, "Idempotency-Key": request.run_idempotency_key},
            json={"stream": False},
        )
        started = self._validate(_RunStart, run_data, "run creation response")
        if started.task_id != task.id:
            raise FrameworkProtocolError("Framework Run belongs to a different Task")
        return self.get_run(
            task.id,
            started.run_id,
            tenant_id=request.tenant_id,
            user_id=request.user_id,
        )

    def get_run(
        self,
        task_id: str,
        run_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> FrameworkRunSnapshot:
        data = self._request_json(
            "GET",
            f"/task-manager/runs/{run_id}",
            headers=self._headers(tenant_id, user_id, task_id=task_id, run_id=run_id),
        )
        record = self._validate(_RunEnvelope, data, "run status response").run
        if record.task_id != task_id or record.id != run_id:
            raise FrameworkProtocolError("Framework returned a mismatched Task or Run")
        return self._snapshot(record)

    def cancel_run(
        self,
        task_id: str,
        run_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> FrameworkRunSnapshot:
        headers = self._headers(tenant_id, user_id, task_id=task_id, run_id=run_id)
        data = self._request_json(
            "POST",
            f"/task-manager/runs/{run_id}/cancel",
            headers=headers,
        )
        record = self._validate(_RunEnvelope, data, "run cancellation response").run
        if record.task_id != task_id or record.id != run_id:
            raise FrameworkProtocolError("Framework cancelled a mismatched Task or Run")
        snapshot = self._snapshot(record)
        deadline = time.monotonic() + self.cancel_wait
        while not snapshot.terminal and time.monotonic() < deadline:
            time.sleep(0.1)
            snapshot = self.get_run(
                task_id,
                run_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
        return snapshot

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.read_timeout,
            pool=self.connect_timeout,
        )
        try:
            with contract_span("contract.framework.request", {"http.request.method": method, "peer.service": "contract-review-framework"}):
                inject_trace_headers(headers)
                with httpx.Client(
                    base_url=self.base_url,
                    timeout=timeout,
                    transport=self.transport,
                ) as client:
                    response = client.request(method, path, headers=headers, json=json)
        except httpx.TimeoutException as exc:
            raise FrameworkTimeoutError() from exc
        except httpx.RequestError as exc:
            raise FrameworkUnavailableError("Unable to connect to Framework") from exc
        if response.status_code >= 500:
            raise FrameworkUnavailableError(
                f"Framework returned HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise FrameworkProtocolError(
                f"Framework rejected the request with HTTP {response.status_code}"
            )
        try:
            value = response.json()
        except ValueError as exc:
            raise FrameworkProtocolError("Framework returned non-JSON content") from exc
        if not isinstance(value, dict):
            raise FrameworkProtocolError("Framework response must be a JSON object")
        return value

    @staticmethod
    def _validate(model, value: dict[str, Any], label: str):
        try:
            return model.model_validate(value)
        except ValidationError as exc:
            raise FrameworkProtocolError(f"Invalid Framework {label}") from exc

    @staticmethod
    def _task_type(request: FrameworkExecutionRequest) -> str:
        if request.execution_mode == "FULL_REVIEW":
            return TASK_TYPE
        if request.execution_mode == "PARTY_RESOLUTION":
            return PARTY_RESOLUTION_TASK_TYPE
        raise FrameworkProtocolError("Contract execution mode is invalid")

    @classmethod
    def _validate_task(cls, task: _TaskRecord, request: FrameworkExecutionRequest) -> None:
        if task.task_type != cls._task_type(request):
            raise FrameworkProtocolError("Framework reused an incompatible Task type")
        if task.tenant_id != request.tenant_id or task.user_id != request.user_id:
            raise FrameworkProtocolError("Framework Task scope does not match the review scope")
        if task.model_pack_id != request.model_pack_id:
            raise FrameworkProtocolError(
                "Framework Task model pack does not match the review"
            )
        metadata = task.metadata_json
        if (
            metadata.get("contract_review_id") != request.review_id
            or metadata.get("contract_attempt_no") != request.attempt_no
            or metadata.get("schema_version") != request.schema_version
        ):
            raise FrameworkProtocolError(
                "Framework reused a Task with incompatible contract metadata"
            )

    @staticmethod
    def _snapshot(record: _RunRecord) -> FrameworkRunSnapshot:
        status = record.status.strip().lower()
        if status not in KNOWN_RUN_STATUSES:
            raise FrameworkProtocolError(f"Framework returned unknown Run status '{record.status}'")
        updated_at = record.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        else:
            updated_at = updated_at.astimezone(timezone.utc)
        if record.current_stage_id is None:
            stage = "PARSING"
        else:
            stage = STAGE_MAPPING.get(record.current_stage_id)
            if stage is None:
                raise FrameworkProtocolError(
                    f"Framework returned unknown contract stage '{record.current_stage_id}'"
                )
        return FrameworkRunSnapshot(
            task_id=record.task_id,
            run_id=record.id,
            status=status,
            current_stage_id=stage,
            cancel_requested=record.cancel_requested,
            updated_at=updated_at,
        )

    @staticmethod
    def _headers(
        tenant_id: str,
        user_id: str,
        *,
        review_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "X-Internal-Service": "ai-contract",
            "X-Tenant-Id": tenant_id,
            "X-User-Id": user_id,
            "X-Roles": "service",
        }
        if review_id:
            headers["X-Contract-Review-Id"] = review_id
            headers["X-Review-Id"] = review_id
        if task_id:
            headers["X-Business-Task-Id"] = task_id
            headers["X-Task-Id"] = task_id
        if run_id:
            headers["X-Run-Id"] = run_id
        return headers
