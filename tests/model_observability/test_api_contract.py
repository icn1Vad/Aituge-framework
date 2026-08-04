from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from model_observability.api import create_model_observability_router
from model_observability.domain import ModelSnapshotError
from model_observability.query import (
    ModelInvocationQueryService,
    ModelQueryScope,
)


_JWT = f"eyJ{'A' * 8}.{'B' * 8}.{'C' * 8}"
_HWM = f"hwm_{'H' * 20}"
_QTK = f"qtk_qs_{'Q' * 43}.{'S' * 43}"
_CUR = f"cur_{'U' * 24}"
_CRF = f"crf_revfin_v1.payload.{'R' * 43}"
_CAP = f"cap.payload.{'C' * 43}"
_OBS1 = f"obs1.test.MODEL_READ.{'O' * 20}.4102444800.{'Z' * 43}"
_SK_KEY = f"sk-proj-{'K' * 32}"
_BEARER_ID = f"Bearer-{'B' * 24}"
_AUTHORIZATION_ID = f"Authorization-{'A' * 24}"
_API_KEY = f"api_key-{'K' * 24}"
_SECRET = f"secret-{'S' * 24}"
_SCOPE_TOKEN = "scope-token-redaction-sentinel"
_SENSITIVE_ERROR_CODES = [
    pytest.param(_QTK, id="qtk"),
    pytest.param(_JWT, id="jwt"),
    pytest.param(_HWM, id="high-watermark"),
    pytest.param(_CUR, id="cursor"),
    pytest.param(_CRF, id="revision-finalize"),
    pytest.param(_CAP, id="capability"),
    pytest.param(_OBS1, id="observability-capability"),
    pytest.param(_SK_KEY, id="sk-key"),
    pytest.param(_BEARER_ID, id="bearer"),
    pytest.param(_AUTHORIZATION_ID, id="authorization"),
    pytest.param(_API_KEY, id="api-key"),
    pytest.param(_SECRET, id="secret"),
]


class _UnusedQueryService:
    pass


class _UnusedAuthorizer:
    async def authorize(self, **_kwargs):  # pragma: no cover - schema only
        raise AssertionError("contract test must not execute handlers")


class _RejectingAuthorizer:
    async def authorize(self, **_kwargs):
        raise HTTPException(status_code=403, detail={"code": "SCOPE_DENIED"})


class _CodedRejectingAuthorizer:
    def __init__(self, code):
        self.code = code

    async def authorize(self, **_kwargs):
        raise HTTPException(status_code=403, detail={"code": self.code})


class _CodedSnapshotError(ModelSnapshotError):
    def __init__(self, code):
        super().__init__("snapshot rejected")
        self.code = code


class _RejectingQueryService:
    def __init__(self, code):
        self.code = code

    async def get_invocation(self, **_kwargs):
        raise _CodedSnapshotError(self.code)


class _AllowingAuthorizer:
    async def authorize(self, **_kwargs):
        return ModelQueryScope(("tenant-a",), "scope-a")


class _SlowAuthorizer:
    async def authorize(self, **_kwargs):
        await asyncio.sleep(0.2)
        raise AssertionError("deadline did not cancel the authorizer")


class _SuccessfulQueryService:
    async def get_invocation(self, **_kwargs):
        return {
            "invocation": {
                "invocationId": "inv-success",
                "projectionVersion": 1,
                "dataAsOf": "2026-07-31T12:00:00Z",
                "logicalCallId": "call-success",
                "attemptNo": 1,
                "tenantId": "tenant-a",
                "featureCode": "contract.review",
                "startedAt": "2026-07-31T12:00:00Z",
                "ingestedAt": "2026-07-31T12:00:00Z",
                "lifecycleStatus": "RUNNING",
                "dispatchStatus": "NOT_DISPATCHED",
                "provider": "deepseek",
                "modelName": "deepseek-v4-pro",
                "privacyMode": "PRIVATE",
                "routeType": "EXTERNAL",
            },
            "events": [],
        }

    async def get_event(self, **_kwargs):
        return {
            "eventId": "model-event-success",
            "eventType": "MODEL_INVOCATION_SUCCEEDED",
            "logicalCallId": "call-success",
            "invocationId": "inv-success",
            "attemptNo": 1,
            "tenantId": "tenant-a",
            "featureCode": "contract.review",
            "occurredAt": "2026-07-31T12:00:00Z",
            "ingestedAt": "2026-07-31T12:00:00Z",
            "outcome": "SUCCESS",
            "dispatchStatus": "DISPATCHED",
            "provider": "deepseek",
            "modelName": "deepseek-v4-pro",
            "privacyMode": "PRIVATE",
            "routeType": "EXTERNAL",
        }

def _unexpected_session_factory():
    raise AssertionError("sensitive input must be rejected before database access")


def _assert_protected_headers(response, request_id: str) -> None:
    assert response.headers["X-Request-ID"] == request_id
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Vary"] == "Authorization, X-Observability-Scope"


def _serialized_response(response) -> str:
    headers = "\n".join(f"{key}: {value}" for key, value in response.headers.items())
    return f"{headers}\n{response.text}"


def test_router_factory_matches_frozen_model_paths_without_registering_itself() -> None:
    router = create_model_observability_router(
        query_service=_UnusedQueryService(),
        authorizer=_UnusedAuthorizer(),
    )
    assert {route.path for route in router.routes} == {
        "/internal/observability/v1/model-invocations",
        "/internal/observability/v1/model-invocation-events",
        "/internal/observability/v1/model-invocations/{invocationId}",
        "/internal/observability/v1/model-invocation-events/{eventId}",
        "/internal/observability/v1/model-summary",
    }

    app = FastAPI()
    assert not any(
        route.path.startswith("/internal/observability/v1/model")
        for route in app.routes
    )
    app.include_router(router)
    schema = app.openapi()
    assert set(schema["paths"]) == {
        "/internal/observability/v1/model-invocations",
        "/internal/observability/v1/model-invocation-events",
        "/internal/observability/v1/model-invocations/{invocationId}",
        "/internal/observability/v1/model-invocation-events/{eventId}",
        "/internal/observability/v1/model-summary",
    }
    expected_operation_ids = {
        "/internal/observability/v1/model-invocations": "internalListModelInvocations",
        "/internal/observability/v1/model-invocation-events": "internalListModelInvocationEvents",
        "/internal/observability/v1/model-invocations/{invocationId}": "internalGetModelInvocation",
        "/internal/observability/v1/model-invocation-events/{eventId}": "internalGetModelInvocationEvent",
        "/internal/observability/v1/model-summary": "internalGetModelSummary",
    }
    assert {
        path: schema["paths"][path]["get"]["operationId"]
        for path in expected_operation_ids
    } == expected_operation_ids


def test_list_contract_has_required_scope_and_fixed_sort() -> None:
    app = FastAPI()
    app.include_router(
        create_model_observability_router(
            query_service=_UnusedQueryService(),
            authorizer=_UnusedAuthorizer(),
        )
    )
    schema = app.openapi()
    operation = schema["paths"][
        "/internal/observability/v1/model-invocations"
    ]["get"]
    parameters = {(item["in"], item["name"]): item for item in operation["parameters"]}
    for key in {
        ("header", "X-Observability-Scope"),
        ("header", "X-Request-ID"),
        ("header", "X-Request-Deadline-Ms"),
        ("query", "from"),
        ("query", "to"),
        ("query", "snapshotTo"),
        ("query", "highWatermark"),
        ("query", "cursor"),
        ("query", "limit"),
        ("query", "dispatchStatus"),
        ("query", "lifecycleStatus"),
    }:
        assert key in parameters
    assert parameters[("header", "X-Observability-Scope")]["required"] is True
    assert parameters[("query", "from")]["required"] is True
    assert operation["x-mtls-required"] is True
    assert operation["x-fixed-sort"] == [
        {"field": "startedAt", "direction": "DESC"},
        {"field": "invocationId", "direction": "DESC"},
    ]
    # The router normalizes runtime validation failures to 400. FastAPI still
    # emits its framework-level 422 schema until page 8 applies the root
    # OpenAPI post-processing required for exact frozen-contract convergence.
    assert "422" in operation["responses"]
    assert {
        "200",
        "400",
        "401",
        "403",
        "429",
        "500",
        "503",
    }.issubset(operation["responses"])


def test_detail_uses_camel_case_path_parameter_and_latest_projection() -> None:
    app = FastAPI()
    app.include_router(
        create_model_observability_router(
            query_service=_UnusedQueryService(),
            authorizer=_UnusedAuthorizer(),
        )
    )
    operation = app.openapi()["paths"][
        "/internal/observability/v1/model-invocations/{invocationId}"
    ]["get"]
    path_parameters = [
        item for item in operation["parameters"] if item["in"] == "path"
    ]
    assert [(item["name"], item["required"]) for item in path_parameters] == [
        ("invocationId", True)
    ]
    assert operation["x-detail-consistency"] == "LATEST"


def test_router_errors_use_flat_redacted_contract_shape() -> None:
    app = FastAPI()
    app.include_router(
        create_model_observability_router(
            query_service=_UnusedQueryService(),
            authorizer=_RejectingAuthorizer(),
        )
    )
    scope_token = _SCOPE_TOKEN
    response = TestClient(app).get(
        "/internal/observability/v1/model-invocations/inv-secret",
        headers={
            "X-Observability-Scope": scope_token,
            "X-Request-ID": "request-1",
            "X-Request-Deadline-Ms": "1000",
        },
    )
    assert response.status_code == 403
    _assert_protected_headers(response, "request-1")
    assert response.json() == {
        "code": "SCOPE_DENIED",
        "requestId": "request-1",
        "retryable": False,
    }
    assert scope_token not in _serialized_response(response)


@pytest.mark.parametrize(
    ("source", "expected_status", "expected_code"),
    [
        pytest.param("http", 403, "FORBIDDEN", id="http-detail"),
        pytest.param("domain", 400, "OBSERVABILITY_QUERY_INVALID", id="domain-code"),
    ],
)
@pytest.mark.parametrize(
    "unsafe_code",
    [
        *_SENSITIVE_ERROR_CODES,
        pytest.param("not-a-registered-code", id="invalid"),
        pytest.param("UNREGISTERED_UPPERCASE_CODE", id="unregistered-stable"),
    ],
)
def test_router_replaces_unregistered_or_sensitive_source_error_codes(
    source: str,
    expected_status: int,
    expected_code: str,
    unsafe_code: str,
) -> None:
    if source == "http":
        query_service = _UnusedQueryService()
        authorizer = _CodedRejectingAuthorizer(unsafe_code)
    else:
        query_service = _RejectingQueryService(unsafe_code)
        authorizer = _AllowingAuthorizer()
    app = FastAPI()
    app.include_router(
        create_model_observability_router(
            query_service=query_service,
            authorizer=authorizer,
        )
    )
    response = TestClient(app).get(
        "/internal/observability/v1/model-invocations/inv-code",
        headers={
            "X-Observability-Scope": _SCOPE_TOKEN,
            "X-Request-ID": "request-code",
            "X-Request-Deadline-Ms": "1000",
        },
    )

    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code
    assert unsafe_code not in response.text
    assert all(
        unsafe_code not in value for value in response.headers.values()
    )
    assert unsafe_code not in _serialized_response(response)


def test_router_enforces_deadline_and_returns_retryable_503() -> None:
    app = FastAPI()
    app.include_router(
        create_model_observability_router(
            query_service=_UnusedQueryService(),
            authorizer=_SlowAuthorizer(),
        )
    )
    scope_token = "scope-token-timeout-sentinel"
    response = TestClient(app).get(
        "/internal/observability/v1/model-invocations/inv-timeout",
        headers={
            "X-Observability-Scope": scope_token,
            "X-Request-ID": "request-timeout",
            "X-Request-Deadline-Ms": "100",
        },
    )

    assert response.status_code == 503
    _assert_protected_headers(response, "request-timeout")
    assert response.json() == {
        "code": "OBSERVABILITY_SOURCE_TIMEOUT",
        "requestId": "request-timeout",
        "retryable": True,
    }
    assert scope_token not in _serialized_response(response)


def test_router_success_has_protected_headers_without_scope_token_echo() -> None:
    app = FastAPI()
    app.include_router(
        create_model_observability_router(
            query_service=_SuccessfulQueryService(),
            authorizer=_AllowingAuthorizer(),
        )
    )
    scope_token = "scope-token-success-sentinel"
    response = TestClient(app).get(
        "/internal/observability/v1/model-invocations/inv-success",
        headers={
            "X-Observability-Scope": scope_token,
            "X-Request-ID": "request-success",
            "X-Request-Deadline-Ms": "1000",
        },
    )

    assert response.status_code == 200
    _assert_protected_headers(response, "request-success")
    assert response.json()["invocation"]["invocationId"] == "inv-success"
    assert scope_token not in _serialized_response(response)


def test_framework_validation_returns_flat_400_without_input_echo() -> None:
    app = FastAPI()
    app.include_router(
        create_model_observability_router(
            query_service=_UnusedQueryService(),
            authorizer=_UnusedAuthorizer(),
        )
    )
    response = TestClient(app).get(
        "/internal/observability/v1/model-invocations",
        params={
            "from": "2026-07-31T00:00:00Z",
            "to": "2026-07-31T12:00:00Z",
            "snapshotTo": "2026-07-31T12:00:00Z",
            "highWatermark": _QTK,
        },
        headers={
            "X-Observability-Scope": _SCOPE_TOKEN,
            "X-Request-ID": "request-validation",
            "X-Request-Deadline-Ms": "1000",
        },
    )

    assert response.status_code == 400
    _assert_protected_headers(response, "request-validation")
    assert response.json() == {
        "code": "OBSERVABILITY_QUERY_INVALID",
        "requestId": "request-validation",
        "retryable": False,
    }
    serialized = _serialized_response(response)
    assert _QTK not in serialized
    assert _SCOPE_TOKEN not in serialized
    assert "input" not in response.json()
    assert "detail" not in response.json()


@pytest.mark.parametrize(
    "sensitive_request_id",
    [
        _QTK,
        _JWT,
        _SK_KEY,
        _BEARER_ID,
        _AUTHORIZATION_ID,
    ],
)
def test_framework_validation_replaces_sensitive_request_id(
    sensitive_request_id: str,
) -> None:
    app = FastAPI()
    app.include_router(
        create_model_observability_router(
            query_service=_UnusedQueryService(),
            authorizer=_UnusedAuthorizer(),
        )
    )
    response = TestClient(app).get(
        "/internal/observability/v1/model-invocations/inv-validation",
        headers={
            "X-Observability-Scope": _SCOPE_TOKEN,
            "X-Request-ID": sensitive_request_id,
            "X-Request-Deadline-Ms": "99",
        },
    )

    assert response.status_code == 400
    response_request_id = response.json()["requestId"]
    assert response_request_id.startswith("request_")
    assert response_request_id != sensitive_request_id
    _assert_protected_headers(response, response_request_id)
    serialized = _serialized_response(response)
    assert sensitive_request_id not in serialized
    assert _SCOPE_TOKEN not in serialized


def test_ordinary_filter_capability_returns_400_without_echo() -> None:
    app = FastAPI()
    app.include_router(
        create_model_observability_router(
            query_service=ModelInvocationQueryService(
                _unexpected_session_factory
            ),
            authorizer=_AllowingAuthorizer(),
        )
    )
    response = TestClient(app).get(
        "/internal/observability/v1/model-invocations",
        params={
            "from": "2026-07-31T00:00:00Z",
            "to": "2026-07-31T12:00:00Z",
            "snapshotTo": "2026-07-31T12:00:00Z",
            "modelName": _CAP,
        },
        headers={
            "X-Observability-Scope": _SCOPE_TOKEN,
            "X-Request-ID": "request-filter",
            "X-Request-Deadline-Ms": "1000",
        },
    )

    assert response.status_code == 400
    _assert_protected_headers(response, "request-filter")
    assert response.json()["code"] == "OBSERVABILITY_QUERY_INVALID"
    serialized = _serialized_response(response)
    assert _CAP not in serialized
    assert _SCOPE_TOKEN not in serialized


def test_detail_capability_returns_400_without_path_echo() -> None:
    app = FastAPI()
    app.include_router(
        create_model_observability_router(
            query_service=ModelInvocationQueryService(
                _unexpected_session_factory
            ),
            authorizer=_AllowingAuthorizer(),
        )
    )
    response = TestClient(app).get(
        f"/internal/observability/v1/model-invocations/{_HWM}",
        headers={
            "X-Observability-Scope": _SCOPE_TOKEN,
            "X-Request-ID": "request-detail",
            "X-Request-Deadline-Ms": "1000",
        },
    )

    assert response.status_code == 400
    _assert_protected_headers(response, "request-detail")
    assert response.json()["code"] == "OBSERVABILITY_QUERY_INVALID"
    serialized = _serialized_response(response)
    assert _HWM not in serialized
    assert _SCOPE_TOKEN not in serialized
