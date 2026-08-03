from __future__ import annotations

import re
from typing import Any

import pytest

from .contract_loader import (
    iter_refs,
    operations,
    path_parameters,
    resolve_local_ref,
)


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


@pytest.mark.parametrize(
    ("fixture_name", "expected_paths", "expected_operations"),
    [
        ("external_spec", 25, 25),
        ("internal_spec", 15, 15),
    ],
)
def test_frozen_operation_inventory(
    request: pytest.FixtureRequest,
    fixture_name: str,
    expected_paths: int,
    expected_operations: int,
) -> None:
    document = request.getfixturevalue(fixture_name)
    found = list(operations(document))
    assert len(document["paths"]) == expected_paths
    assert len(found) == expected_operations
    operation_ids = [operation["operationId"] for _, _, operation in found]
    assert len(operation_ids) == len(set(operation_ids))
    assert all(operation_ids)


@pytest.mark.parametrize("fixture_name", ["external_spec", "internal_spec"])
def test_all_refs_resolve_and_path_parameters_are_declared(
    request: pytest.FixtureRequest,
    fixture_name: str,
) -> None:
    document = request.getfixturevalue(fixture_name)

    for pointer, ref in iter_refs(document):
        assert ref.startswith("#/"), f"{pointer} uses non-local ref {ref}"
        assert resolve_local_ref(document, ref) is not None

    for path, _, operation in operations(document):
        declared = path_parameters(document, path, operation)
        placeholders = set(re.findall(r"{([^{}]+)}", path))
        assert set(declared) == placeholders
        assert all(parameter.get("required") is True for parameter in declared.values())


@pytest.mark.parametrize("fixture_name", ["external_spec", "internal_spec"])
def test_environment_is_deployment_bound_not_a_request_parameter(
    request: pytest.FixtureRequest,
    fixture_name: str,
) -> None:
    document = request.getfixturevalue(fixture_name)
    for path, _, operation in operations(document):
        parameters: list[dict[str, Any]] = [
            *document["paths"][path].get("parameters", []),
            *operation.get("parameters", []),
        ]
        for parameter in parameters:
            if "$ref" in parameter:
                parameter = resolve_local_ref(document, parameter["$ref"])
            assert parameter.get("name") != "environment"


def test_export_discriminator_has_explicit_mapping(external_spec: dict) -> None:
    filter_schema = external_spec["components"]["schemas"]["ExportRequest"][
        "properties"
    ]["filter"]
    assert filter_schema["discriminator"] == {
        "propertyName": "filterType",
        "mapping": {
            "EVENTS": "#/components/schemas/EventExportFilter",
            "TASKS": "#/components/schemas/TaskExportFilter",
            "MODEL_INVOCATIONS": "#/components/schemas/ModelExportFilter",
            "SECURITY_EVENTS": "#/components/schemas/SecurityExportFilter",
            "RUNTIME_LOGS": "#/components/schemas/RuntimeLogExportFilter",
            "TRACES": "#/components/schemas/TraceExportFilter",
            "ALERTS": "#/components/schemas/AlertExportFilter",
        },
    }


def test_unified_event_contract_excludes_privileged_sources(
    external_spec: dict,
) -> None:
    schemas = external_spec["components"]["schemas"]
    assert schemas["UnifiedEvent"]["properties"]["source"]["$ref"].endswith(
        "/UnifiedEventSource"
    )
    assert set(schemas["UnifiedEventSource"]["enum"]) == {
        "HTTP_AUDIT",
        "BUSINESS_EVENT",
        "TASK_EVENT",
        "MODEL_EVENT",
    }
    properties = schemas["UnifiedEvent"]["properties"]
    assert "runtimeMessage" not in properties
    assert "locator" not in properties


def test_cross_tenant_rows_require_tenant_id(external_spec: dict) -> None:
    schemas = external_spec["components"]["schemas"]
    assert "tenantId" in schemas["TaskSummary"]["required"]
    assert "tenantId" in schemas["ModelInvocationSummary"]["required"]


def test_masked_source_ip_schema_accepts_hmac_sha256(
    external_spec: dict,
    internal_spec: dict,
) -> None:
    expected_width = len("hmac-sha256:") + 64
    external = external_spec["components"]["schemas"]["SecurityEvent"]["properties"][
        "sourceIpMasked"
    ]
    internal = next(
        parameter["schema"]
        for parameter in internal_spec["paths"]["/security-events"]["get"]["parameters"]
        if parameter.get("name") == "sourceIpMasked"
    )
    assert external.get("maxLength", 0) >= expected_width
    assert internal.get("maxLength", 0) >= expected_width


@pytest.mark.parametrize("fixture_name", ["external_spec", "internal_spec"])
def test_object_schemas_are_closed_and_define_required_fields(
    request: pytest.FixtureRequest,
    fixture_name: str,
) -> None:
    document = request.getfixturevalue(fixture_name)
    for name, schema in document["components"]["schemas"].items():
        if schema.get("type") != "object":
            continue
        assert schema.get("additionalProperties") is False, name
        assert schema.get("required"), name
