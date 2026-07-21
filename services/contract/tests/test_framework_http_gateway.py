from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from contract.application.framework_gateway import (
    FrameworkExecutionRequest,
    FrameworkProtocolError,
    FrameworkTimeoutError,
    FrameworkUnavailableError,
)
from contract.application.framework_http_gateway import FrameworkHttpGateway
from contract.config import Settings


def _request() -> FrameworkExecutionRequest:
    return FrameworkExecutionRequest(
        review_id="review-1",
        attempt_no=2,
        tenant_id="tenant-1",
        user_id="user-1",
        business_task_id="business-1",
        contract_version_id="version-1",
        document_id="document-1",
        perspective="PARTY_B",
        our_party_name="乙方单位",
        contract_type="AUTO",
        review_attitude="NEUTRAL",
        schema_version="1.0",
    )


def _settings(**overrides) -> Settings:
    values = {
        "framework_base_url": "http://framework.test:8894",
        "framework_connect_timeout_seconds": 1,
        "framework_read_timeout_seconds": 1,
        "framework_cancel_wait_seconds": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _run_payload(*, status: str = "running", stage: str | None = "commercial_terms_review"):
    return {
        "run": {
            "id": "run-1",
            "task_id": "task-1",
            "status": status,
            "current_stage_id": stage,
            "cancel_requested": status == "cancelled",
            "updated_at": "2026-07-19T10:00:00",
        }
    }


def test_create_execution_uses_scoped_headers_and_frozen_idempotency_keys() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/task-manager/tasks":
            payload = json.loads(request.content)
            assert payload["task_type"] == "contract.review.run"
            assert payload["input_payload"] == {
                "schema_version": "1.0",
                "review_id": "review-1",
                "attempt_no": 2,
                "business_task_id": "business-1",
                "contract_version_id": "version-1",
                "document_id": "document-1",
                "perspective": "PARTY_B",
                "our_party_name": "乙方单位",
                "contract_type": "AUTO",
                "review_attitude": "NEUTRAL",
            }
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": "task-1",
                        "task_type": "contract.review.run",
                        "user_id": "user-1",
                        "tenant_id": "tenant-1",
                        "metadata_json": payload["metadata"],
                    }
                },
            )
        if request.url.path == "/task-manager/tasks/task-1/runs":
            return httpx.Response(
                200,
                json={
                    "task_id": "task-1",
                    "run_id": "run-1",
                    "status": "pending",
                    "stream_url": "/task-manager/runs/run-1/events/stream",
                },
            )
        if request.url.path == "/task-manager/runs/run-1":
            return httpx.Response(200, json=_run_payload())
        raise AssertionError(request.url.path)

    gateway = FrameworkHttpGateway(_settings(), transport=httpx.MockTransport(handler))
    snapshot = gateway.create_execution(_request())

    assert snapshot.task_id == "task-1"
    assert snapshot.run_id == "run-1"
    assert snapshot.status == "running"
    assert snapshot.current_stage_id == "RISK_REVIEW"
    assert snapshot.updated_at == datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    assert [item.url.path for item in calls] == [
        "/task-manager/tasks",
        "/task-manager/tasks/task-1/runs",
        "/task-manager/runs/run-1",
    ]
    assert calls[0].headers["idempotency-key"] == "contract-review:review-1:attempt:2"
    assert calls[1].headers["idempotency-key"] == "contract-review:review-1:attempt:2:run"
    for call in calls:
        assert call.headers["x-internal-service"] == "ai-contract"
        assert call.headers["x-tenant-id"] == "tenant-1"
        assert call.headers["x-user-id"] == "user-1"
        assert call.headers["x-roles"] == "service"


def test_cancel_returns_real_terminal_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/task-manager/runs/run-1/cancel"
        return httpx.Response(200, json=_run_payload(status="cancelled", stage="verify_evidence"))

    gateway = FrameworkHttpGateway(_settings(), transport=httpx.MockTransport(handler))
    snapshot = gateway.cancel_run(
        "task-1",
        "run-1",
        tenant_id="tenant-1",
        user_id="user-1",
    )

    assert snapshot.status == "cancelled"
    assert snapshot.terminal is True
    assert snapshot.cancel_requested is True
    assert snapshot.current_stage_id == "EVIDENCE_VERIFICATION"


@pytest.mark.parametrize(
    ("response_status", "expected_error"),
    [
        (400, FrameworkProtocolError),
        (500, FrameworkUnavailableError),
    ],
)
def test_http_failures_map_to_stable_gateway_errors(response_status, expected_error) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(response_status, json={"detail": "x"})
    )
    gateway = FrameworkHttpGateway(_settings(), transport=transport)

    with pytest.raises(expected_error):
        gateway.get_run(
            "task-1",
            "run-1",
            tenant_id="tenant-1",
            user_id="user-1",
        )


def test_transport_timeout_maps_to_504_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    gateway = FrameworkHttpGateway(_settings(), transport=httpx.MockTransport(handler))

    with pytest.raises(FrameworkTimeoutError) as raised:
        gateway.get_run(
            "task-1",
            "run-1",
            tenant_id="tenant-1",
            user_id="user-1",
        )

    assert raised.value.code == "FRAMEWORK_TIMEOUT"
    assert raised.value.status_code == 504
    assert raised.value.retryable is True


def test_task_scope_or_metadata_mismatch_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/task-manager/tasks":
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": "task-1",
                        "task_type": "contract.review.run",
                        "user_id": "other-user",
                        "tenant_id": "tenant-1",
                        "metadata_json": {
                            "contract_review_id": "review-1",
                            "contract_attempt_no": 2,
                            "schema_version": "1.0",
                        },
                    }
                },
            )
        raise AssertionError("Run must not be created after scope mismatch")

    gateway = FrameworkHttpGateway(_settings(), transport=httpx.MockTransport(handler))

    with pytest.raises(FrameworkProtocolError, match="scope"):
        gateway.create_execution(_request())


def test_unknown_contract_stage_is_rejected_instead_of_masked_as_parsing() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=_run_payload(stage="future_contract_stage"))
    )
    gateway = FrameworkHttpGateway(_settings(), transport=transport)

    with pytest.raises(FrameworkProtocolError, match="unknown contract stage"):
        gateway.get_run(
            "task-1",
            "run-1",
            tenant_id="tenant-1",
            user_id="user-1",
        )


@pytest.mark.parametrize(
    "stage",
    [
        "extract_ir_definitions_basics",
        "extract_ir_rights_duties",
        "extract_ir_commercial_terms",
        "extract_ir_liability_termination",
        "extract_ir_special_terms",
    ],
)
def test_internal_ir_fragment_stages_remain_external_ir_extraction(stage: str) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=_run_payload(stage=stage))
    )
    gateway = FrameworkHttpGateway(_settings(), transport=transport)

    snapshot = gateway.get_run(
        "task-1",
        "run-1",
        tenant_id="tenant-1",
        user_id="user-1",
    )

    assert snapshot.current_stage_id == "IR_EXTRACTION"
