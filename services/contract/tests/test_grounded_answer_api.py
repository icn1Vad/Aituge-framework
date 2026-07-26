from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
import httpx
import pytest
from fastapi.testclient import TestClient

from contract.api.app import create_app
from contract.api.models import ReviewStatus, ReviewStatusData
from contract.application.ports import InternalRequestContext
from contract.config import Settings
from contract.errors import ContractError
from contract.grounded.models import (
    GroundedAnswerData,
    GroundedChatRequest,
    GroundedReportRequest,
)
from contract.grounded.service import (
    ContentMarkdownStreamDecoder,
    FrameworkGroundedAnswerService,
)


TOKEN = "contract-test-token"
RESULT_HASH = "sha256:" + "a" * 64
QUOTE_HASH = "sha256:" + "b" * 64


def _settings() -> Settings:
    return Settings(
        internal_auth_enabled=True,
        internal_token=TOKEN,
        framework_base_url="http://framework.test",
    )


def _context(idempotency_key: str = "grounded-key-1") -> InternalRequestContext:
    return InternalRequestContext(
        tenant_id="tenant-1",
        user_id="user-1",
        request_id="request-1",
        idempotency_key=idempotency_key,
    )


def _answer(mode: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "mode": mode,
        "review_id": "review-1",
        "document_id": "document-1",
        "contract_version_id": "version-1",
        "result_hash": RESULT_HASH,
        "content_markdown": "合同结论见 [付款条款](#docref-ref-1)。",
        "references": [
            {
                "reference_id": "docref-ref-1",
                "label": "付款条款",
                "evidence_id": "evidence-1",
                "finding_id": "finding-1",
                "document_id": "document-1",
                "contract_version_id": "version-1",
                "chunk_id": "block-1",
                "block_id": "block-1",
                "page_number": None,
                "char_start": 2,
                "char_end": 12,
                "quoted_text": "甲方应支付服务费",
                "quoted_text_hash": QUOTE_HASH,
            }
        ],
    }


def test_framework_service_runs_report_task_and_reuses_succeeded_task() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if request.url.path == "/task-manager/tasks":
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": "task-1",
                        "task_type": "contract.grounded.answer",
                        "status": "created",
                        "input_payload_json": {
                            key: value
                            for key, value in body["input_payload"].items()
                            if value is not None
                        },
                        "result_payload_json": None,
                        "error_payload_json": None,
                        "tenant_id": "tenant-1",
                        "user_id": "user-1",
                    }
                },
            )
        if request.url.path == "/task-manager/tasks/task-1/run":
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": "task-1",
                        "task_type": "contract.grounded.answer",
                        "status": "succeeded",
                        "input_payload_json": {
                            "schema_version": "1.0",
                            "mode": "REPORT",
                            "review_id": "review-1",
                            "document_id": "document-1",
                            "conversation_history": [],
                        },
                        "result_payload_json": {
                            "content": json.dumps(
                                _answer("REPORT"),
                                ensure_ascii=False,
                            ),
                            "structured": _answer("REPORT"),
                            "usage": None,
                            "thread_id": None,
                            "session_id": None,
                        },
                        "error_payload_json": None,
                        "tenant_id": "tenant-1",
                        "user_id": "user-1",
                    }
                },
            )
        raise AssertionError(request.url.path)

    service = FrameworkGroundedAnswerService(
        _settings(),
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        service.generate_report(
            review_id="review-1",
            request=GroundedReportRequest(document_id="document-1"),
            context=_context(),
        )
    )

    assert result.mode == "REPORT"
    assert result.references[0].reference_id == "docref-ref-1"
    assert [request.url.path for request in requests] == [
        "/task-manager/tasks",
        "/task-manager/tasks/task-1/run",
    ]
    assert requests[0].headers["idempotency-key"].startswith(
        "contract-grounded:report:"
    )


def test_framework_service_rejects_task_reused_for_different_input() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "task": {
                    "id": "task-1",
                    "task_type": "contract.grounded.answer",
                    "status": "succeeded",
                    "input_payload_json": {
                        **body["input_payload"],
                        "document_id": "another-document",
                    },
                    "result_payload_json": _answer("CHAT"),
                    "error_payload_json": None,
                    "tenant_id": "tenant-1",
                    "user_id": "user-1",
                }
            },
        )

    service = FrameworkGroundedAnswerService(
        _settings(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ContractError) as exc_info:
        asyncio.run(
            service.answer_chat(
                review_id="review-1",
                request=GroundedChatRequest(
                    document_id="document-1",
                    question="付款条件是什么？",
                ),
                context=_context(),
            )
        )

    assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"


def test_framework_service_does_not_restart_a_terminal_failed_task() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "task": {
                    "id": "task-failed",
                    "task_type": "contract.grounded.answer",
                    "status": "failed",
                    "input_payload_json": {
                        key: value
                        for key, value in body["input_payload"].items()
                        if value is not None
                    },
                    "result_payload_json": None,
                    "error_payload_json": {
                        "code": "StageExecutionError",
                        "message": "citation validation failed",
                    },
                    "tenant_id": "tenant-1",
                    "user_id": "user-1",
                }
            },
        )

    service = FrameworkGroundedAnswerService(
        _settings(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ContractError) as exc_info:
        asyncio.run(
            service.generate_report(
                review_id="review-1",
                request=GroundedReportRequest(document_id="document-1"),
                context=_context(),
            )
        )

    assert exc_info.value.code == "GROUNDED_ANSWER_FAILED"
    assert [request.url.path for request in requests] == ["/task-manager/tasks"]


def test_content_markdown_stream_decoder_handles_split_field_and_escapes() -> None:
    decoder = ContentMarkdownStreamDecoder()

    assert decoder.feed('{"schema_version":"1.0","content_mark') == ""
    assert decoder.feed('down":"第一行\\n付款见 ') == "第一行\n付款见 "
    assert decoder.feed('[条款](#docref-evidence-1)。","citations":[]}') == (
        "[条款](#docref-evidence-1)。"
    )


def test_framework_service_streams_markdown_and_returns_validated_answer() -> None:
    requests: list[httpx.Request] = []
    draft_json = json.dumps(
        {
            "schema_version": "1.0",
            "mode": "CHAT",
            "content_markdown": "合同结论见 [付款条款](#docref-ref-1)。",
            "citations": [{"evidence_id": "ref-1", "label": "付款条款"}],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    split_at = draft_json.index("付款条款")

    def envelope(sequence: int, delta: str) -> str:
        value = {
            "schema_version": "1.0",
            "event_id": f"event-{sequence}",
            "task_id": "task-stream",
            "run_id": "run-stream",
            "sequence": sequence,
            "event_type": "agent_delta",
            "stage_id": "generate_grounded_answer",
            "payload": {"delta": delta},
        }
        return (
            "event: agent_delta\n"
            f"data: {json.dumps(value, ensure_ascii=False)}\n\n"
        )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/task-manager/tasks":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": "task-stream",
                        "task_type": "contract.grounded.answer",
                        "status": "created",
                        "input_payload_json": {
                            key: value
                            for key, value in body["input_payload"].items()
                            if value is not None
                        },
                        "result_payload_json": None,
                        "error_payload_json": None,
                        "tenant_id": "tenant-1",
                        "user_id": "user-1",
                        "current_run_id": None,
                    }
                },
            )
        if request.url.path == "/task-manager/tasks/task-stream/stream":
            terminal = {
                "schema_version": "1.0",
                "event_id": "event-3",
                "task_id": "task-stream",
                "run_id": "run-stream",
                "sequence": 3,
                "event_type": "task_succeeded",
                "payload": {},
            }
            body = (
                envelope(1, draft_json[:split_at])
                + envelope(2, draft_json[split_at:])
                + "event: task_succeeded\n"
                + f"data: {json.dumps(terminal)}\n\n"
            )
            return httpx.Response(
                200,
                text=body,
                headers={"Content-Type": "text/event-stream"},
            )
        if request.url.path == "/task-manager/tasks/task-stream":
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": "task-stream",
                        "task_type": "contract.grounded.answer",
                        "status": "succeeded",
                        "input_payload_json": {
                            "schema_version": "1.0",
                            "mode": "CHAT",
                            "review_id": "review-1",
                            "document_id": "document-1",
                            "question": "付款条件是什么？",
                            "conversation_history": [],
                        },
                        "result_payload_json": {
                            "structured": _answer("CHAT"),
                        },
                        "error_payload_json": None,
                        "tenant_id": "tenant-1",
                        "user_id": "user-1",
                        "current_run_id": "run-stream",
                    }
                },
            )
        raise AssertionError(request.url.path)

    service = FrameworkGroundedAnswerService(
        _settings(),
        transport=httpx.MockTransport(handler),
    )

    async def collect() -> list[tuple[str, dict[str, object]]]:
        return [
            event
            async for event in service.stream_chat(
                review_id="review-1",
                request=GroundedChatRequest(
                    document_id="document-1",
                    question="付款条件是什么？",
                ),
                context=_context(),
            )
        ]

    events = asyncio.run(collect())
    assert [event[0] for event in events] == ["meta", "delta", "delta", "done"]
    assert "".join(
        str(data["delta"]) for event_type, data in events if event_type == "delta"
    ) == "合同结论见 [付款条款](#docref-ref-1)。"
    assert events[-1][1]["answer"] == _answer("CHAT")
    assert [request.url.path for request in requests] == [
        "/task-manager/tasks",
        "/task-manager/tasks/task-stream/stream",
        "/task-manager/tasks/task-stream",
    ]


class _SucceededReviewService:
    def get_status(
        self,
        review_id: str,
        *,
        context: InternalRequestContext,
    ) -> ReviewStatusData:
        assert context.tenant_id == "tenant-1"
        return ReviewStatusData(
            review_id=review_id,
            business_task_id="business-1",
            contract_version_id="version-1",
            status=ReviewStatus.SUCCEEDED,
            document_id="document-1",
            updated_at=datetime.now(timezone.utc),
        )


class _FakeGroundedService:
    async def generate_report(self, **_kwargs: object) -> GroundedAnswerData:
        return GroundedAnswerData.model_validate(_answer("REPORT"))

    async def answer_chat(self, **_kwargs: object) -> GroundedAnswerData:
        return GroundedAnswerData.model_validate(_answer("CHAT"))

    async def stream_chat(self, **_kwargs: object):
        yield "meta", {"request_id": "request-1", "task_id": "task-1"}
        yield "delta", {"delta": "合同结论见 "}
        yield "delta", {"delta": "[付款条款](#docref-ref-1)。"}
        yield "done", {"answer": _answer("CHAT")}


def _api_client() -> TestClient:
    return TestClient(
        create_app(
            _settings(),
            service=_SucceededReviewService(),
            grounded_answer_service=_FakeGroundedService(),
        ),
        raise_server_exceptions=False,
    )


def _headers() -> dict[str, str]:
    return {
        "X-Internal-Service": "continew-java",
        "X-Internal-Token": TOKEN,
        "X-User-Id": "user-1",
        "X-Tenant-Id": "tenant-1",
        "X-Request-Id": "request-1",
        "Idempotency-Key": "grounded-key-1",
    }


def test_report_and_chat_business_endpoints_return_grounded_references() -> None:
    client = _api_client()

    report = client.post(
        "/v1/contract-reviews/review-1/report",
        headers=_headers(),
        json={"schema_version": "1.0", "document_id": "document-1"},
    )
    chat = client.post(
        "/v1/contract-reviews/review-1/chat",
        headers=_headers(),
        json={
            "schema_version": "1.0",
            "document_id": "document-1",
            "question": "付款条件是什么？",
            "conversation_history": [],
        },
    )

    assert report.status_code == 200
    assert report.json()["data"]["mode"] == "REPORT"
    assert chat.status_code == 200
    assert chat.json()["data"]["mode"] == "CHAT"
    assert chat.json()["data"]["references"][0]["chunk_id"] == "block-1"


def test_chat_stream_endpoint_returns_sse_events() -> None:
    client = _api_client()

    response = client.post(
        "/v1/contract-reviews/review-1/chat/stream",
        headers=_headers(),
        json={
            "schema_version": "1.0",
            "document_id": "document-1",
            "question": "付款条件是什么？",
            "conversation_history": [],
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: meta" in response.text
    assert "event: delta" in response.text
    assert "event: done" in response.text
    assert '"reference_id":"docref-ref-1"' in response.text


def test_grounded_endpoints_require_idempotency_and_matching_document() -> None:
    client = _api_client()
    headers = _headers()
    headers.pop("Idempotency-Key")

    missing_key = client.post(
        "/v1/contract-reviews/review-1/report",
        headers=headers,
        json={"schema_version": "1.0", "document_id": "document-1"},
    )
    mismatch = client.post(
        "/v1/contract-reviews/review-1/chat",
        headers=_headers(),
        json={
            "schema_version": "1.0",
            "document_id": "wrong-document",
            "question": "付款条件是什么？",
        },
    )

    assert missing_key.status_code == 422
    assert missing_key.json()["error"]["code"] == "REQUEST_SCHEMA_INVALID"
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "REVIEW_DOCUMENT_MISMATCH"
