from __future__ import annotations

import json

from fastapi.testclient import TestClient

from contract.api.app import create_app
from contract.config import Settings


TOKEN = "contract-test-token"
PDF_BYTES = b"%PDF-1.4\ncontract review test\n%%EOF"


def _client() -> TestClient:
    return TestClient(
        create_app(
            Settings(
                internal_auth_enabled=True,
                internal_token=TOKEN,
                mock_mode=True,
            )
        ),
        raise_server_exceptions=False,
    )


def _headers(**overrides: str) -> dict[str, str]:
    headers = {
        "X-Internal-Service": "continew-java",
        "X-Internal-Token": TOKEN,
        "X-User-Id": "1",
        "X-Tenant-Id": "1",
        "X-Request-Id": "req-10001",
        "Idempotency-Key": "idem-10001",
    }
    headers.update(overrides)
    return headers


def _request_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "business_task_id": "10001",
        "contract_version_id": "20001",
        "perspective": "PARTY_B",
        "our_party_name": "某某单位",
        "contract_type": "AUTO",
        "review_attitude": "NEUTRAL",
        "schema_version": "1.0",
    }
    payload.update(overrides)
    return payload


def _create_review(
    client: TestClient,
    *,
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    content: bytes = PDF_BYTES,
    filename: str = "contract.pdf",
    content_type: str = "application/pdf",
):
    return client.post(
        "/v1/contract-reviews",
        headers=headers or _headers(),
        files={
            "file": (filename, content, content_type),
            "request": (None, json.dumps(payload or _request_payload(), ensure_ascii=False), "application/json"),
        },
    )


def test_health_does_not_require_internal_auth() -> None:
    response = _client().get("/health", headers={"X-Request-Id": "req-health"})

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {
            "status": "UP",
            "service": "contract",
            "schema_version": "1.0",
            "mode": "mock",
        },
        "request_id": "req-health",
    }


def test_create_status_result_not_ready_and_cancel_flow() -> None:
    client = _client()

    created = _create_review(client)

    assert created.status_code == 201
    created_data = created.json()["data"]
    assert created_data["status"] == "CREATED"
    assert created_data["current_stage"] is None
    assert created_data["framework_attempt_no"] is None
    assert created_data["framework_task_id"] is None
    assert created_data["framework_run_id"] is None
    assert created_data["reused"] is False
    review_id = created_data["review_id"]

    status_response = client.get(f"/v1/contract-reviews/{review_id}", headers=_headers())
    assert status_response.status_code == 200
    assert status_response.json()["data"]["status"] == "CREATED"
    assert status_response.json()["data"]["document_id"] == created_data["document_id"]

    result_response = client.get(f"/v1/contract-reviews/{review_id}/result", headers=_headers())
    assert result_response.status_code == 409
    assert result_response.json()["error"]["code"] == "REVIEW_NOT_READY"
    assert result_response.json()["error"]["retryable"] is True

    cancelled = client.post(f"/v1/contract-reviews/{review_id}/cancel", headers=_headers())
    assert cancelled.status_code == 200
    assert cancelled.json()["data"] == {
        "review_id": review_id,
        "status": "CANCELLED",
        "already_terminal": False,
    }

    cancelled_again = client.post(f"/v1/contract-reviews/{review_id}/cancel", headers=_headers())
    assert cancelled_again.status_code == 200
    assert cancelled_again.json()["data"]["already_terminal"] is True


def test_same_request_is_reused_and_changed_request_conflicts() -> None:
    client = _client()
    first = _create_review(client)
    repeated = _create_review(client)

    assert repeated.status_code == 201
    assert repeated.json()["data"]["review_id"] == first.json()["data"]["review_id"]
    assert repeated.json()["data"]["reused"] is True

    conflict = _create_review(client, payload=_request_payload(perspective="PARTY_A"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_normalized_party_name_is_used_for_idempotency() -> None:
    client = _client()
    first = _create_review(client, payload=_request_payload(our_party_name="　某某\t单位 "))
    repeated = _create_review(client, payload=_request_payload(our_party_name="某某 单位"))

    assert repeated.status_code == 201
    assert repeated.json()["data"]["review_id"] == first.json()["data"]["review_id"]
    assert repeated.json()["data"]["reused"] is True


def test_changed_raw_file_bytes_conflict_with_same_key() -> None:
    client = _client()
    _create_review(client)

    conflict = _create_review(client, content=PDF_BYTES + b"\nchanged")

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_business_task_key_is_also_idempotent() -> None:
    client = _client()
    first = _create_review(client)
    second_headers = _headers(**{"Idempotency-Key": "idem-other"})
    repeated = _create_review(client, headers=second_headers)

    assert repeated.status_code == 201
    assert repeated.json()["data"]["review_id"] == first.json()["data"]["review_id"]
    assert repeated.json()["data"]["reused"] is True


def test_tenant_and_user_scope_is_enforced() -> None:
    client = _client()
    created = _create_review(client)
    review_id = created.json()["data"]["review_id"]

    response = client.get(
        f"/v1/contract-reviews/{review_id}",
        headers=_headers(**{"X-User-Id": "2"}),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCESS_DENIED"


def test_internal_token_is_required() -> None:
    response = _create_review(_client(), headers=_headers(**{"X-Internal-Token": "wrong"}))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED_INTERNAL_CALL"


def test_request_schema_forbids_extra_fields() -> None:
    response = _create_review(_client(), payload=_request_payload(unfrozen_option="value"))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_SCHEMA_INVALID"


def test_only_pdf_and_docx_are_accepted() -> None:
    response = _create_review(
        _client(),
        filename="contract.txt",
        content=b"plain text",
        content_type="text/plain",
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "FILE_TYPE_UNSUPPORTED"


def test_docx_upload_is_accepted_by_protocol_skeleton() -> None:
    response = _create_review(
        _client(),
        filename="contract.docx",
        content=b"PK\x03\x04mock-docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert response.status_code == 201


def test_configured_file_size_limit_returns_413() -> None:
    client = TestClient(
        create_app(
            Settings(
                internal_auth_enabled=True,
                internal_token=TOKEN,
                max_file_size=8,
                mock_mode=True,
            )
        ),
        raise_server_exceptions=False,
    )

    response = _create_review(client, content=PDF_BYTES)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_corrupted_pdf_is_rejected() -> None:
    response = _create_review(_client(), content=b"not a pdf")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FILE_CORRUPTED"


def test_missing_headers_use_standard_error_envelope() -> None:
    response = _client().get("/v1/contract-reviews/review-1")

    assert response.status_code == 422
    assert response.json()["success"] is False
    assert response.json()["error"]["code"] == "REQUEST_SCHEMA_INVALID"
    assert response.json()["request_id"].startswith("req-")
