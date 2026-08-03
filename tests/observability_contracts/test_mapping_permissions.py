from __future__ import annotations

import pytest

from .contract_loader import operations, resolve_local_ref
from .dto_mapping_matrix import (
    DTO_MAPPING_MATRIX,
    KNOWN_FROZEN_SCHEMA_GAPS,
    deterministic_trace_id,
)


INTERNAL_TO_EXTERNAL = {
    "/tasks": {"/tasks"},
    "/tasks/{taskId}": {"/tasks/{taskId}"},
    "/tasks/{taskId}/timeline": {"/tasks/{taskId}/timeline", "/timeline"},
    "/task-events": {"/events", "/timeline"},
    "/runs/{runId}/stages": {"/runs/{runId}/stages"},
    "/task-events/{eventId}": {"/events/{source}/{eventId}"},
    "/runs/{runId}/events": {"/runs/{runId}/events", "/timeline"},
    "/runs/{runId}/events/stream": {"/runs/{runId}/events/stream"},
    "/model-invocations": {"/model-invocations"},
    "/model-invocation-events": {"/events", "/timeline"},
    "/model-invocations/{invocationId}": {"/model-invocations/{invocationId}"},
    "/model-invocation-events/{eventId}": {"/events/{source}/{eventId}"},
    "/model-summary": {"/model-summary"},
    "/security-events": {"/security-events"},
    "/security-events/{eventId}": {"/security-events/{eventId}"},
}

RESOURCE_PERMISSIONS = {
    "EVENTS": ["monitor:observability:view"],
    "TASKS": ["monitor:observability:view"],
    "MODEL_INVOCATIONS": ["monitor:observability:view"],
    "SECURITY_EVENTS": ["monitor:observability:security"],
    "RUNTIME_LOGS": ["monitor:observability:runtime"],
    "TRACES": ["monitor:observability:detail"],
    "ALERTS": ["monitor:observability:operations"],
}


OPERATION_PERMISSIONS = {
    ("GET", "/overview"): ["monitor:observability:view"],
    ("GET", "/events"): ["monitor:observability:view"],
    ("GET", "/events/{source}/{eventId}"): ["monitor:observability:detail"],
    ("GET", "/tasks"): ["monitor:observability:view"],
    ("GET", "/tasks/{taskId}"): ["monitor:observability:detail"],
    ("GET", "/tasks/{taskId}/timeline"): ["monitor:observability:detail"],
    ("GET", "/runs/{runId}/stages"): ["monitor:observability:detail"],
    ("GET", "/runs/{runId}/events"): ["monitor:observability:detail"],
    ("GET", "/runs/{runId}/events/stream"): ["monitor:observability:detail"],
    ("GET", "/model-summary"): ["monitor:observability:view"],
    ("GET", "/model-invocations"): ["monitor:observability:view"],
    ("GET", "/model-invocations/{invocationId}"): ["monitor:observability:detail"],
    ("GET", "/security-events"): ["monitor:observability:security"],
    ("GET", "/security-events/{eventId}"): [
        "monitor:observability:security",
        "monitor:observability:detail",
    ],
    ("POST", "/runtime-logs/search"): ["monitor:observability:runtime"],
    ("POST", "/runtime-logs/detail"): [
        "monitor:observability:runtime",
        "monitor:observability:detail",
    ],
    ("GET", "/traces"): ["monitor:observability:view"],
    ("GET", "/traces/{traceId}"): ["monitor:observability:detail"],
    ("GET", "/timeline"): ["monitor:observability:detail"],
    ("GET", "/services"): ["monitor:observability:operations"],
    ("GET", "/alerts"): ["monitor:observability:operations"],
    ("GET", "/storage"): ["monitor:observability:operations"],
    ("POST", "/exports"): ["monitor:observability:export"],
    ("GET", "/exports/{exportId}"): ["monitor:observability:export"],
    ("GET", "/exports/{exportId}/download"): ["monitor:observability:export"],
}


def test_every_external_operation_has_the_exact_permission_matrix(
    external_spec: dict,
) -> None:
    observed = {
        (method.upper(), path): operation.get("x-required-permissions")
        for path, method, operation in operations(external_spec)
    }
    assert len(observed) == 25
    assert observed == OPERATION_PERMISSIONS


def test_all_internal_paths_have_an_external_consumer(
    external_spec: dict,
    internal_spec: dict,
) -> None:
    assert set(INTERNAL_TO_EXTERNAL) == set(internal_spec["paths"])
    external_paths = set(external_spec["paths"])
    for internal_path, consumers in INTERNAL_TO_EXTERNAL.items():
        assert consumers
        assert consumers <= external_paths, internal_path


def test_external_task_and_model_dto_can_carry_internal_projection_identity(
    external_spec: dict,
    internal_spec: dict,
) -> None:
    external = external_spec["components"]["schemas"]
    internal = internal_spec["components"]["schemas"]
    for external_name, internal_name in [
        ("TaskSummary", "InternalTask"),
        ("ModelInvocationSummary", "InternalModelInvocation"),
    ]:
        external_required = set(external[external_name]["required"])
        internal_required = set(internal[internal_name]["required"])
        assert {"tenantId", "projectionVersion", "dataAsOf"} <= external_required
        assert {"tenantId", "projectionVersion", "dataAsOf"} <= internal_required


def test_privileged_pages_require_dedicated_permissions(external_spec: dict) -> None:
    paths = external_spec["paths"]
    assert paths["/security-events"]["get"]["x-required-permissions"] == [
        "monitor:observability:security"
    ]
    assert paths["/security-events/{eventId}"]["get"]["x-required-permissions"] == [
        "monitor:observability:security",
        "monitor:observability:detail",
    ]
    assert paths["/runtime-logs/search"]["post"]["x-required-permissions"] == [
        "monitor:observability:runtime"
    ]
    assert paths["/runtime-logs/detail"]["post"]["x-required-permissions"] == [
        "monitor:observability:runtime",
        "monitor:observability:detail",
    ]
    for path in ["/services", "/alerts", "/storage"]:
        assert paths[path]["get"]["x-required-permissions"] == [
            "monitor:observability:operations"
        ]


def test_overview_clips_cards_by_dedicated_permission(external_spec: dict) -> None:
    conditioned = external_spec["paths"]["/overview"]["get"][
        "x-permission-conditioned-data"
    ]
    assert conditioned == {
        "infrastructureCards": "monitor:observability:operations",
        "securityCards": "monitor:observability:security",
        "runtimeCards": "monitor:observability:runtime",
    }


def test_export_rechecks_base_resource_and_cross_tenant_permissions(
    external_spec: dict,
) -> None:
    for path, method in [
        ("/exports", "post"),
        ("/exports/{exportId}", "get"),
        ("/exports/{exportId}/download", "get"),
    ]:
        operation = external_spec["paths"][path][method]
        assert operation["x-required-permissions"] == ["monitor:observability:export"]
        assert operation["x-resource-permission-rules"] == RESOURCE_PERMISSIONS

    create_description = external_spec["paths"]["/exports"]["post"]["description"]
    assert "跨租户权限" in create_description
    for path in ["/exports/{exportId}", "/exports/{exportId}/download"]:
        assert external_spec["paths"][path]["get"].get("x-authorization-rules")


def test_details_hide_forbidden_objects_as_not_found(external_spec: dict) -> None:
    detail_operations = [
        (path, method, operation)
        for path, method, operation in operations(external_spec)
        if ("{" in path or path == "/runtime-logs/detail")
    ]
    assert detail_operations
    for path, method, operation in detail_operations:
        assert "404" in operation["responses"], f"{method.upper()} {path}"


def test_list_and_detail_scope_capabilities_are_separate(external_spec: dict) -> None:
    parameters = external_spec["components"]["parameters"]
    assert parameters["AccessSession"]["name"] == "X-Observability-Access-Session"
    assert parameters["AccessContext"]["name"] == "X-Observability-Access-Context"
    assert "轮询" in parameters["AccessSession"]["description"]
    assert "跨租户详情必填" in parameters["AccessContext"]["description"]


def test_unified_event_routes_cannot_select_security_or_runtime(
    external_spec: dict,
) -> None:
    source = external_spec["components"]["schemas"]["UnifiedEventSource"]
    assert "SECURITY_EVENT" not in source["enum"]
    assert "RUNTIME_LOG" not in source["enum"]
    path_source_schema = external_spec["paths"]["/events/{source}/{eventId}"]["get"][
        "parameters"
    ][0]["schema"]
    path_source = external_spec["components"]["schemas"][
        path_source_schema["$ref"].rsplit("/", 1)[-1]
    ]["enum"]
    assert set(path_source) == set(source["enum"])


def test_event_detail_mapping_has_exact_internal_lookup(
    external_spec: dict,
    internal_spec: dict,
) -> None:
    external_sources = set(
        external_spec["components"]["schemas"]["UnifiedEventSource"]["enum"]
    )
    assert {"TASK_EVENT", "MODEL_EVENT"} <= external_sources
    for path in ["/task-events", "/model-invocation-events"]:
        operation = internal_spec["paths"][path]["get"]
        names = set()
        for parameter in operation.get("parameters", []):
            if "$ref" in parameter:
                parameter = resolve_local_ref(internal_spec, parameter["$ref"])
            names.add(parameter["name"])
        assert "eventId" in names

    for path in ["/task-events/{eventId}", "/model-invocation-events/{eventId}"]:
        names = set()
        for parameter in internal_spec["paths"][path]["get"].get("parameters", []):
            if "$ref" in parameter:
                parameter = resolve_local_ref(internal_spec, parameter["$ref"])
            names.add(parameter["name"])
        assert "eventId" in names

def _resolved_schema(document: dict, schema: dict) -> dict:
    while "$ref" in schema:
        schema = resolve_local_ref(document, schema["$ref"])
    return schema


def _field_contract(
    document: dict, schema_name: str, field_path: str
) -> tuple[dict, bool]:
    schema = document["components"]["schemas"][schema_name]
    required = False
    for name in field_path.split("."):
        schema = _resolved_schema(document, schema)
        assert schema.get("type") == "object", (schema_name, field_path, name)
        properties = schema.get("properties", {})
        assert name in properties, (schema_name, field_path, name)
        required = name in schema.get("required", [])
        schema = properties[name]
    return _resolved_schema(document, schema), required


def _root(path: str) -> str:
    return path.split(".", 1)[0]


def _roots(paths) -> set[str]:
    return {_root(path) for path in paths}


def _assert_partition(
    actual_fields: set[str],
    categories: dict[str, set[str]],
    domain: str,
    side: str,
) -> None:
    assert set().union(*categories.values()) == actual_fields, (
        domain,
        side,
        categories,
    )
    owners = {
        field: [name for name, fields in categories.items() if field in fields]
        for field in actual_fields
    }
    assert all(len(names) == 1 for names in owners.values()), (domain, side, owners)


def test_dto_field_mapping_matrix_is_bidirectionally_closed(
    external_spec: dict,
    internal_spec: dict,
) -> None:
    assert set(DTO_MAPPING_MATRIX) == {
        "TASK_SUMMARY",
        "TASK_EVENT",
        "MODEL_INVOCATION_SUMMARY",
        "MODEL_EVENT",
        "MODEL_SUMMARY",
        "SECURITY_EVENT",
        "STAGE_SUMMARY",
    }
    for domain, mapping in DTO_MAPPING_MATRIX.items():
        internal = internal_spec["components"]["schemas"][mapping.internal_schema]
        external = external_spec["components"]["schemas"][mapping.external_schema]

        transformed_sources = {
            path for transform in mapping.transformed for path in transform.sources
        }
        transformed_targets = {
            path for transform in mapping.transformed for path in transform.targets
        }
        _assert_partition(
            set(internal["properties"]),
            {
                "direct": _roots(mapping.direct),
                "renamed": _roots(mapping.renamed),
                "transformed": _roots(transformed_sources),
                "defaulted": _roots(mapping.defaulted),
                "consumed": _roots(mapping.consumed),
            },
            domain,
            "internal",
        )
        _assert_partition(
            set(external["properties"]),
            {
                "direct": _roots(mapping.direct),
                "renamed": _roots(mapping.renamed.values()),
                "transformed": _roots(transformed_targets),
                "synthesized": _roots(mapping.synthesized),
                "defaulted": _roots(mapping.defaulted),
                "defaulted_null": _roots(mapping.defaulted_null),
            },
            domain,
            "external",
        )

        for path in mapping.direct:
            _field_contract(internal_spec, mapping.internal_schema, path)
            _field_contract(external_spec, mapping.external_schema, path)
        for source, target in mapping.renamed.items():
            _field_contract(internal_spec, mapping.internal_schema, source)
            _field_contract(external_spec, mapping.external_schema, target)
        for transform in mapping.transformed:
            assert transform.sources and transform.targets
            assert transform.rule and transform.risk
            for source in transform.sources:
                _field_contract(internal_spec, mapping.internal_schema, source)
            for target in transform.targets:
                _field_contract(external_spec, mapping.external_schema, target)
        for path, fallback in mapping.defaulted.items():
            _field_contract(internal_spec, mapping.internal_schema, path)
            _field_contract(external_spec, mapping.external_schema, path)
            assert fallback
        for target, source_rule in mapping.synthesized.items():
            _field_contract(external_spec, mapping.external_schema, target)
            assert source_rule
        for source, use in mapping.consumed.items():
            _field_contract(internal_spec, mapping.internal_schema, source)
            assert use
        for target, reason in mapping.defaulted_null.items():
            _, required = _field_contract(
                external_spec, mapping.external_schema, target
            )
            assert not required, (domain, target)
            assert reason


def test_transforms_make_trace_selection_and_model_duration_semantics_explicit() -> (
    None
):
    task = DTO_MAPPING_MATRIX["TASK_SUMMARY"]
    trace_transform = next(
        transform for transform in task.transformed if "traceIds" in transform.sources
    )
    assert trace_transform.targets == ("correlation",)
    assert "ordering is not frozen" in trace_transform.risk
    assert deterministic_trace_id(["trace-z", "", "trace-a", "trace-z"]) == "trace-a"
    assert deterministic_trace_id(["trace-z", "trace-a"]) == deterministic_trace_id(
        ["trace-a", "trace-z"]
    )
    assert deterministic_trace_id([]) is None

    model = DTO_MAPPING_MATRIX["MODEL_EVENT"]
    duration_transform = next(
        transform
        for transform in model.transformed
        if transform.targets == ("durationMs",)
    )
    assert duration_transform.sources == ("latencyMs",)
    assert "end-to-end" in duration_transform.rule
    assert "terminal" in duration_transform.rule
    assert "timeToFirstTokenMs" in duration_transform.risk
    assert "timeToFirstTokenMs" in model.consumed


_SHAPE_KEYS = {
    "type",
    "format",
    "enum",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
}


def _assert_output_compatible(
    source_document: dict,
    source_schema: dict,
    source_required: bool,
    target_document: dict,
    target_schema: dict,
    target_required: bool,
    *,
    defaulted: bool = False,
) -> None:
    source = _resolved_schema(source_document, source_schema)
    target = _resolved_schema(target_document, target_schema)
    assert source.get("type") == target.get("type")
    if target_required and not defaulted:
        assert source_required
    if source.get("nullable") is True:
        assert target.get("nullable") is True or not target_required

    source_type = source.get("type")
    if source_type == "object":
        source_properties = source.get("properties", {})
        target_properties = target.get("properties", {})
        assert set(source_properties) <= set(target_properties)
        assert set(target.get("required", [])) <= set(source.get("required", []))
        for name, source_property in source_properties.items():
            _assert_output_compatible(
                source_document,
                source_property,
                name in source.get("required", []),
                target_document,
                target_properties[name],
                name in target.get("required", []),
            )
        return
    if source_type == "array":
        _assert_output_compatible(
            source_document,
            source["items"],
            True,
            target_document,
            target["items"],
            True,
        )

    if "format" in target:
        assert source.get("format") == target["format"]
    if "enum" in target:
        assert "enum" in source
        assert set(source["enum"]) <= set(target["enum"])
    for key in ("minimum", "exclusiveMinimum", "minLength", "minItems"):
        if key in target:
            assert key in source
            assert source[key] >= target[key]
    for key in ("maximum", "exclusiveMaximum", "maxLength", "maxItems"):
        if key in target:
            assert key in source
            assert source[key] <= target[key]
    if "pattern" in target:
        assert source.get("pattern") == target["pattern"]


def test_every_direct_defaulted_and_renamed_shape_is_fully_compatible(
    external_spec: dict,
    internal_spec: dict,
) -> None:
    observed_gaps: dict[tuple[str, str, str], str] = {}
    for domain, mapping in DTO_MAPPING_MATRIX.items():
        pairs = [(path, path, False) for path in mapping.direct]
        pairs.extend(
            (source, target, False) for source, target in mapping.renamed.items()
        )
        pairs.extend((path, path, True) for path in mapping.defaulted)
        for source_path, target_path, has_default in pairs:
            source, source_required = _field_contract(
                internal_spec, mapping.internal_schema, source_path
            )
            target, target_required = _field_contract(
                external_spec, mapping.external_schema, target_path
            )
            key = (domain, source_path, target_path)
            try:
                _assert_output_compatible(
                    internal_spec,
                    source,
                    source_required,
                    external_spec,
                    target,
                    target_required,
                    defaulted=has_default,
                )
            except AssertionError:
                assert key in KNOWN_FROZEN_SCHEMA_GAPS, key
                observed_gaps[key] = KNOWN_FROZEN_SCHEMA_GAPS[key]
    assert observed_gaps == KNOWN_FROZEN_SCHEMA_GAPS


def test_mapping_exposes_only_redacted_security_and_model_identifiers(
    external_spec: dict,
    internal_spec: dict,
) -> None:
    for document, schema_names in [
        (
            internal_spec,
            {mapping.internal_schema for mapping in DTO_MAPPING_MATRIX.values()},
        ),
        (
            external_spec,
            {mapping.external_schema for mapping in DTO_MAPPING_MATRIX.values()},
        ),
    ]:
        for schema_name in schema_names:
            fields = document["components"]["schemas"][schema_name]["properties"]
            assert "sourceIp" not in fields
            assert "providerRequestId" not in fields
            assert "authorization" not in fields
            assert "credential" not in fields
            assert "prompt" not in fields
            assert "response" not in fields

    internal_model = internal_spec["components"]["schemas"]["InternalModelInvocation"][
        "properties"
    ]
    assert "providerRequestIdHash" in internal_model
    external_model = external_spec["components"]["schemas"]["ModelMetadata"][
        "properties"
    ]
    assert "providerRequestIdHash" not in external_model
    for document in [external_spec, internal_spec]:
        metadata = document["components"]["schemas"]["MetadataField"]
        assert metadata["required"] == ["key", "value", "masked"]
        assert metadata["properties"]["masked"]["type"] == "boolean"
        assert metadata["properties"]["value"]["maxLength"] <= 512
