from __future__ import annotations

import json
from typing import Any

from .contract_loader import resolve_local_ref


EXTERNAL_PAGED_GETS = [
    "/events",
    "/tasks",
    "/tasks/{taskId}/timeline",
    "/runs/{runId}/events",
    "/model-invocations",
    "/security-events",
    "/traces",
    "/timeline",
    "/alerts",
]


def _parameter_names(
    document: dict[str, Any],
    path: str,
    method: str,
) -> set[str]:
    operation = document["paths"][path][method]
    parameters = [
        *document["paths"][path].get("parameters", []),
        *operation.get("parameters", []),
    ]
    names = set()
    for parameter in parameters:
        if "$ref" in parameter:
            parameter = resolve_local_ref(document, parameter["$ref"])
        names.add(parameter["name"])
    return names


def test_cursor_and_retry_token_form_a_closed_protocol(external_spec: dict) -> None:
    for path in EXTERNAL_PAGED_GETS:
        names = _parameter_names(external_spec, path, "get")
        assert {"cursor", "retryToken", "limit"} <= names, path
        assert "400" in external_spec["paths"][path]["get"]["responses"], path

    parameters = external_spec["components"]["parameters"]
    assert "与 retryToken 互斥" in parameters["Cursor"]["description"]
    assert "与 cursor 互斥" in parameters["RetryToken"]["description"]
    assert "进入下一页" in parameters["Cursor"]["description"]
    assert "当前未完成页" in parameters["RetryToken"]["description"]

    runtime_request = external_spec["components"]["schemas"]["RuntimeLogSearchRequest"][
        "properties"
    ]
    assert {"cursor", "retryToken", "limit"} <= set(runtime_request)
    assert "互斥" in runtime_request["cursor"]["description"]
    assert "互斥" in runtime_request["retryToken"]["description"]
    assert "400" in external_spec["paths"]["/runtime-logs/search"]["post"]["responses"]

    page = external_spec["components"]["schemas"]["PageInfo"]
    assert {"nextCursor", "retryToken", "complete", "hasMore", "limit"} <= set(
        page["properties"]
    )
    assert {"complete", "hasMore", "limit"} <= set(page["required"])
    assert page["properties"]["hasMore"].get("nullable") is True
    assert "complete=false" in page["properties"]["hasMore"]["description"]
    retry_description = page["properties"]["retryToken"]["description"]
    assert "重试当前页" in retry_description
    assert "nextCursor 必须为空" in retry_description


def test_correlation_timeline_requires_an_id_and_has_stable_sort(
    external_spec: dict,
) -> None:
    timeline = external_spec["paths"]["/timeline"]["get"]
    assert timeline["x-at-least-one-parameter"] == [
        "taskId",
        "runId",
        "requestId",
        "traceId",
        "reviewId",
    ]
    assert timeline["x-fixed-sort"] == [
        {"field": "occurredAt", "direction": "ASC"},
        {"field": "sourceOrder", "direction": "ASC"},
        {"field": "eventId", "direction": "ASC"},
    ]
    assert "400" in timeline["responses"]


def test_task_and_run_timelines_are_forward_sequence_ordered(
    external_spec: dict,
    internal_spec: dict,
) -> None:
    expected = [
        {"field": "sequence", "direction": "ASC"},
        {"field": "eventId", "direction": "ASC"},
    ]
    for document, paths in [
        (external_spec, ["/tasks/{taskId}/timeline", "/runs/{runId}/events"]),
        (internal_spec, ["/tasks/{taskId}/timeline", "/runs/{runId}/events"]),
    ]:
        for path in paths:
            assert document["paths"][path]["get"]["x-fixed-sort"] == expected


def test_runtime_locator_and_python_security_failure_statuses(
    external_spec: dict,
) -> None:
    runtime = external_spec["paths"]["/runtime-logs/detail"]["post"]["responses"]
    assert {"404", "410"} <= set(runtime)
    security = external_spec["paths"]["/security-events/{eventId}"]["get"]["responses"]
    assert "503" in security


def test_model_dispatch_lifecycle_and_cost_semantics(
    external_spec: dict,
    internal_spec: dict,
) -> None:
    for document in [external_spec, internal_spec]:
        schemas = document["components"]["schemas"]
        assert schemas["DispatchStatus"]["enum"] == [
            "NOT_DISPATCHED",
            "DISPATCHED",
            "DISPATCH_UNKNOWN",
        ]
        assert schemas["InvocationLifecycleStatus"]["enum"] == [
            "RUNNING",
            "TERMINAL",
        ]
        assert "UNKNOWN" in schemas["Outcome"]["enum"]
        assert "OUTCOME_UNKNOWN" not in schemas["Outcome"]["enum"]
        currency = schemas["CurrencyAmount"]
        assert currency["properties"]["currency"]["pattern"] == "^[A-Z]{3}$"
        assert currency["properties"]["amount"]["minimum"] == 0

    event_types = internal_spec["components"]["schemas"][
        "InternalModelInvocationEvent"
    ]["properties"]["eventType"]["enum"]
    assert "MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED" in event_types
    assert "MODEL_INVOCATION_OUTCOME_UNKNOWN" in event_types
    assert "MODEL_INVOCATION_INPUT_GUARDRAIL_REJECTED" not in event_types

    attempts = external_spec["components"]["schemas"]["ModelAttemptMetrics"]
    assert "costs" in attempts["required"]
    assert "costAmount" not in attempts["properties"]


def test_access_session_and_context_are_capability_tokens(
    external_spec: dict,
) -> None:
    policy = external_spec["x-observability-capability-policy"]
    sensitive = set(policy["sensitiveNames"])
    assert {
        "downloadToken",
        "cursor",
        "retryToken",
        "locator",
        "accessContext",
        "accessSession",
        "X-Observability-Access-Context",
        "X-Observability-Access-Session",
        "highWatermark",
    } <= sensitive
    assert policy["requiredResponseHeaders"] == {
        "Cache-Control": "no-store, private",
        "Referrer-Policy": "no-referrer",
        "Vary": "Authorization",
    }


def test_sse_has_replay_gap_and_reset_contract(
    external_spec: dict,
    internal_spec: dict,
) -> None:
    for document in [external_spec, internal_spec]:
        operation = document["paths"]["/runs/{runId}/events/stream"]["get"]
        assert {"409", "410"} <= set(operation["responses"])
        description = operation["description"]
        assert "eventId + sequence" in description
        assert "Last-Event-ID" in description
        assert "reset-required" in description
        assert "EVENT_STREAM_REPLAY_GAP" in description
        assert "EVENT_STREAM_RESET_REQUIRED" in description

    example = external_spec["paths"]["/runs/{runId}/events/stream"]["get"]["responses"][
        "200"
    ]["content"]["text/event-stream"]["example"]
    assert '"sequence":42' in example
    assert '"seq":42' not in example


def test_internal_scope_tokens_are_bound_per_call_not_one_time_service_jwt(
    internal_spec: dict,
) -> None:
    rendered = json.dumps(internal_spec, ensure_ascii=False)
    assert "Service JWT 短期可复用" in rendered
    assert "jti 用于追踪和撤销，不按一次使用处理" in rendered
    assert "绑定 method、path、queryHash、requestId" in rendered
    assert "每个调用使用独立 jti" in rendered

    for path_item in internal_spec["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            assert operation.get("x-mtls-required") is True


def test_internal_high_watermark_is_a_short_opaque_handle(
    internal_spec: dict,
) -> None:
    handle = internal_spec["components"]["schemas"]["SourceWatermark"]["properties"][
        "highWatermarkHandle"
    ]
    assert handle["maxLength"] <= 84
    assert handle["pattern"].startswith("^hwm_")
    assert "PIT" in handle["description"]
    assert "JWT" in handle["description"]
