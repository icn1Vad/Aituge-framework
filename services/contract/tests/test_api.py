from __future__ import annotations

import importlib
import json

from fastapi.testclient import TestClient

from contract.api.app import create_app
from contract.callback.models import FrameworkCallbackData
from contract.config import Settings
from contract.internal.models import ContractDocumentToolData, ContractWindowPlanToolData
from contract.risk.plan_builder import RiskReviewPlanBuilder
from services.contract.capabilities.revision_drafts import (
    InMemoryRevisionDraftCache,
    InMemoryRevisionSourceProvider,
    RevisionDraftService,
    RevisionReviewSource,
)

from risk_test_data import risk_plan_input


TOKEN = "contract-test-token"
PDF_BYTES = b"%PDF-1.4\ncontract review test\n%%EOF"


class ResolvingRevisionSourceProvider(InMemoryRevisionSourceProvider):
    async def resolve_generation_id(self, review_id: str, result_hash: str) -> str:
        matching = [
            generation_id
            for candidate_review_id, generation_id, candidate_result_hash in self.sources
            if candidate_review_id == review_id and candidate_result_hash == result_hash
        ]
        if len(matching) != 1:
            raise AssertionError("test source must resolve to exactly one Generation")
        return matching[0]


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


def _revision_client() -> TestClient:
    result_hash = "sha256:" + "a" * 64
    source = RevisionReviewSource(
        review_id="review-revision-1",
        generation_id="generation-revision-1",
        result_hash=result_hash,
        review_status="COMPLETED",
        perspective="PARTY_A",
        our_party="Party A",
        counterparty="Party B",
        findings=[],
        evidences=[],
        contract_ir=[],
    )
    return TestClient(
        create_app(
            Settings(
                internal_auth_enabled=True,
                internal_token=TOKEN,
                mock_mode=True,
            ),
            revision_draft_service=RevisionDraftService(
                source_provider=ResolvingRevisionSourceProvider(
                    {(source.review_id, source.generation_id, source.result_hash): source}
                ),
                generator=object(),
                cache=InMemoryRevisionDraftCache(),
            ),
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


def test_hidden_revision_draft_routes_use_internal_auth_and_request_identity() -> None:
    client = _revision_client()
    params = {
        "result_hash": "sha256:" + "a" * 64,
    }

    unauthorized = client.get(
        "/v1/internal/contract-reviews/review-revision-1/revision-drafts",
        params=params,
        headers=_headers(**{"X-Internal-Token": "wrong"}),
    )
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED_INTERNAL_CALL"
    assert unauthorized.json()["request_id"] == "req-10001"

    wrong_generation = client.get(
        "/v1/internal/contract-reviews/review-revision-1/revision-drafts",
        params={**params, "generation_id": "generation-other"},
        headers=_headers(),
    )
    assert wrong_generation.status_code == 404
    assert wrong_generation.json()["error"]["code"] == "GENERATION_NOT_FOUND"

    first = client.get(
        "/v1/internal/contract-reviews/review-revision-1/revision-drafts",
        params=params,
        headers=_headers(),
    )
    assert first.status_code == 200
    assert first.json() == {
        "schema_version": "1.0",
        "review_id": "review-revision-1",
        "generation_id": "generation-revision-1",
        "result_hash": "sha256:" + "a" * 64,
        "status": "COMPLETED",
        "drafts": [],
        "failed_findings": [],
        "model_call_count": 0,
        "duration_ms": first.json()["duration_ms"],
        "cache_hit": False,
    }

    generated = client.post(
        "/v1/internal/contract-reviews/review-revision-1/revision-drafts:generate",
        params=params,
        headers=_headers(),
    )
    assert generated.status_code == 200
    assert generated.json()["cache_hit"] is True


def test_non_mock_mode_lazily_builds_runtime_service(monkeypatch) -> None:
    app_module = importlib.import_module("contract.api.app")

    class RuntimeStub:
        def health(self):
            return {
                "status": "UP",
                "service": "contract",
                "schema_version": "1.0",
                "mode": "runtime",
            }

    built = []
    monkeypatch.setattr(
        app_module,
        "build_runtime_contract_review_service",
        lambda settings: built.append(settings) or RuntimeStub(),
    )
    client = TestClient(
        create_app(Settings(mock_mode=False, database_url="postgresql://not-opened")),
        raise_server_exceptions=False,
    )

    first = client.get("/health", headers={"X-Request-Id": "req-runtime-health"})
    second = client.get("/health", headers={"X-Request-Id": "req-runtime-health-2"})

    assert first.status_code == 200
    assert first.json()["data"]["mode"] == "runtime"
    assert second.status_code == 200
    assert len(built) == 1


def test_non_mock_lifespan_runs_startup_reconciliation_once() -> None:
    class RuntimeStub:
        def __init__(self) -> None:
            self.reconcile_calls = 0

        def reconcile_nonterminal_reviews(self) -> int:
            self.reconcile_calls += 1
            return 3

    runtime = RuntimeStub()
    app = create_app(
        Settings(mock_mode=False, database_url="postgresql://not-opened"),
        service=runtime,
    )

    with TestClient(app, raise_server_exceptions=False):
        pass

    assert runtime.reconcile_calls == 1


def test_framework_callback_requires_its_own_token_and_strict_terminal_shape() -> None:
    class CallbackStub:
        def accept(self, path_review_id, callback):
            assert path_review_id == callback.review_id == "review-1"
            return FrameworkCallbackData(accepted=True, duplicate=False)

    client = TestClient(
        create_app(
            Settings(
                mock_mode=True,
                internal_token=TOKEN,
                framework_result_sink_internal_token="framework-token",
            ),
            internal_service=object(),
            callback_service=CallbackStub(),
        ),
        raise_server_exceptions=False,
    )
    payload = {
        "schema_version": "1.0",
        "review_id": "review-1",
        "attempt_no": 1,
        "framework_task_id": "task-1",
        "framework_run_id": "run-1",
        "stage_id": None,
        "event_sequence": 1000,
        "callback_id": "callback-run-1-success",
        "callback_type": "RUN_SUCCEEDED",
        "result": None,
        "error": None,
    }
    headers = {
        "X-Internal-Service": "aituge-framework",
        "X-Internal-Token": "framework-token",
        "X-Request-Id": "req-framework-callback",
    }

    accepted = client.post(
        "/v1/internal/contract-reviews/review-1/framework-result",
        headers=headers,
        json=payload,
    )
    unauthorized = client.post(
        "/v1/internal/contract-reviews/review-1/framework-result",
        headers={**headers, "X-Internal-Token": TOKEN},
        json=payload,
    )
    invalid = client.post(
        "/v1/internal/contract-reviews/review-1/framework-result",
        headers=headers,
        json={**payload, "stage_id": "finalize_review"},
    )

    assert accepted.status_code == 200
    assert accepted.json()["data"] == {
        "accepted": True,
        "duplicate": False,
        "ignored_reason": None,
    }
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED_INTERNAL_CALL"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "REQUEST_SCHEMA_INVALID"


def test_framework_tool_endpoint_uses_callback_credential_and_typed_response() -> None:
    class InternalStub:
        def get_document(self, payload):
            assert payload.review_id == "review-1"
            assert payload.document_id == "document-1"
            return ContractDocumentToolData(
                review_id="review-1",
                document_id="document-1",
                contract_version_id="version-1",
                original_name="contract.pdf",
                content_type="application/pdf",
                file_type="pdf",
                file_size=128,
                content_hash="sha256:" + "1" * 64,
                generation_id="generation-1",
                generation_status="RUNNING",
                block_count=1,
            )

        def get_window_plan(self, payload):
            assert payload.review_id == "review-1"
            assert payload.document_id == "document-1"
            return ContractWindowPlanToolData(
                review_id="review-1",
                document_id="document-1",
                generation_id="generation-1",
                expected_blocks=[{"block_id": "block-1", "text_length": 4}],
                expected_section_ids=["section-1"],
                windows=[
                    {
                        "window_id": "window-1",
                        "sequence_no": 1,
                        "section_ids": ["section-1"],
                        "heading_path": [],
                        "clause_nos": [],
                        "primary_block_ids": ["block-1"],
                        "estimated_tokens": 4,
                        "source_text": "test",
                        "context_text": "",
                        "offset_map": [
                            {
                                "rendered_start": 0,
                                "rendered_end": 4,
                                "block_id": "block-1",
                                "block_no": 1,
                                "block_char_start": 0,
                                "block_char_end": 4,
                                "page_number": None,
                            }
                        ],
                    }
                ],
            )

        def get_risk_plan(self, payload):
            assert payload.review_id == "review-1"
            assert payload.document_id == "document-1"
            assert payload.selected_playbook_ids == ["base_neutral"]
            return RiskReviewPlanBuilder().build(risk_plan_input())

    client = TestClient(
        create_app(
            Settings(
                mock_mode=True,
                internal_token=TOKEN,
                framework_result_sink_internal_token="framework-token",
            ),
            internal_service=InternalStub(),
            callback_service=object(),
        ),
        raise_server_exceptions=False,
    )
    headers = {
        "X-Internal-Service": "aituge-framework",
        "X-Internal-Token": "framework-token",
        "X-Request-Id": "req-framework-tool",
    }
    payload = {"review_id": "review-1", "document_id": "document-1"}

    accepted = client.post(
        "/v1/internal/contract-tools/document",
        headers=headers,
        json=payload,
    )
    unauthorized = client.post(
        "/v1/internal/contract-tools/document",
        headers={**headers, "X-Internal-Token": TOKEN},
        json=payload,
    )
    windows = client.post(
        "/v1/internal/contract-tools/windows",
        headers=headers,
        json=payload,
    )
    risk_plan = client.post(
        "/v1/internal/contract-reviews/review-1/risk-plan",
        headers=headers,
        json={**payload, "selected_playbook_ids": ["base_neutral"]},
    )

    assert accepted.status_code == 200
    assert accepted.json()["data"]["generation_status"] == "RUNNING"
    assert accepted.json()["data"]["block_count"] == 1
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED_INTERNAL_CALL"
    assert windows.status_code == 200
    assert windows.json()["data"]["concurrency"] == 10
    assert windows.json()["data"]["windows"][0]["primary_block_ids"] == ["block-1"]
    assert risk_plan.status_code == 200
    assert risk_plan.json()["data"]["plan_version"] == "1.0"
    assert len(risk_plan.json()["data"]["review_units"]) == 7


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
    assert created_data["model_pack_id"] == "api-rerank"
    assert created_data["reused"] is False
    review_id = created_data["review_id"]

    status_response = client.get(f"/v1/contract-reviews/{review_id}", headers=_headers())
    assert status_response.status_code == 200
    assert status_response.json()["data"]["status"] == "CREATED"
    assert status_response.json()["data"]["document_id"] == created_data["document_id"]
    assert status_response.json()["data"]["model_pack_id"] == "api-rerank"

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


def test_review_accepts_explicit_model_pack_and_rejects_unknown_pack() -> None:
    client = _client()

    selected = _create_review(
        client,
        payload=_request_payload(
            business_task_id="10002",
            model_pack_id="local-rerank",
        ),
        headers=_headers(
            **{
                "X-Request-Id": "req-10002",
                "Idempotency-Key": "idem-10002",
            }
        ),
    )
    assert selected.status_code == 201
    assert selected.json()["data"]["model_pack_id"] == "local-rerank"

    invalid = _create_review(
        client,
        payload=_request_payload(
            business_task_id="10003",
            model_pack_id="missing-pack",
        ),
        headers=_headers(
            **{
                "X-Request-Id": "req-10003",
                "Idempotency-Key": "idem-10003",
            }
        ),
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_MODEL_PACK"


def test_review_ai_mode_selects_package_and_overrides_payload_package() -> None:
    client = _client()

    selected = _create_review(
        client,
        payload=_request_payload(
            business_task_id="10004",
            model_pack_id="api-rerank",
        ),
        headers=_headers(
            **{
                "X-Request-Id": "req-10004",
                "Idempotency-Key": "idem-10004",
                "X-AI-Mode": "private",
            }
        ),
    )

    assert selected.status_code == 201
    assert selected.json()["data"]["model_pack_id"] == "api-rerank-similarity"


def test_review_rejects_unknown_ai_mode() -> None:
    client = _client()

    response = _create_review(
        client,
        payload=_request_payload(business_task_id="10005"),
        headers=_headers(
            **{
                "X-Request-Id": "req-10005",
                "Idempotency-Key": "idem-10005",
                "X-AI-Mode": "secret",
            }
        ),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_AI_MODE"


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


def test_create_requires_idempotency_key_with_standard_error_response() -> None:
    headers = _headers()
    headers.pop("Idempotency-Key")

    response = _create_review(_client(), headers=headers)

    assert response.status_code == 422
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "REQUEST_SCHEMA_INVALID"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["user_action_required"] is True
    assert "detail" not in payload
    assert any(
        violation["location"] == ["header", "Idempotency-Key"]
        and violation["type"] == "missing"
        for violation in payload["error"]["details"]["violations"]
    )


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


def test_encrypted_docx_signature_is_rejected() -> None:
    response = _create_review(
        _client(),
        filename="contract.docx",
        content=bytes.fromhex("D0CF11E0A1B11AE1") + b"encrypted-package",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FILE_ENCRYPTED"


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
