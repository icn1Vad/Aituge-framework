from __future__ import annotations

import json
from pathlib import Path

from contract.api.app import create_app
from contract.config import Settings


OPENAPI_FILE = Path(__file__).parents[1] / "openapi" / "contract-agent-openapi-v1.json"


def test_runtime_openapi_matches_frozen_file() -> None:
    expected = json.loads(OPENAPI_FILE.read_text("utf-8"))
    actual = create_app(Settings(internal_auth_enabled=False)).openapi()

    assert actual == expected


def test_every_json_response_has_an_explicit_schema() -> None:
    schema = create_app(Settings(internal_auth_enabled=False)).openapi()

    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for status_code, response in operation["responses"].items():
                content = response.get("content", {})
                if "application/json" not in content:
                    continue
                response_schema = content["application/json"].get("schema")
                assert response_schema, f"{method.upper()} {path} response {status_code} has no schema"


def test_multipart_request_part_is_declared_as_json() -> None:
    schema = create_app(Settings(internal_auth_enabled=False)).openapi()
    multipart = schema["paths"]["/v1/contract-reviews"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]

    assert multipart["encoding"]["request"]["contentType"] == "application/json"
    body_schema_name = multipart["schema"]["$ref"].rsplit("/", 1)[-1]
    body_schema = schema["components"]["schemas"][body_schema_name]
    assert set(body_schema["required"]) == {"file", "request"}
    assert "request" in body_schema["properties"]
    assert "request_payload" not in body_schema["properties"]


def test_idempotency_key_is_required_only_for_create() -> None:
    schema = create_app(Settings(internal_auth_enabled=False)).openapi()
    paths = schema["paths"]
    create_parameters = paths["/v1/contract-reviews"]["post"]["parameters"]
    idempotency = next(
        parameter
        for parameter in create_parameters
        if parameter["name"] == "Idempotency-Key" and parameter["in"] == "header"
    )

    assert idempotency["required"] is True
    assert idempotency["schema"]["minLength"] == 1
    assert idempotency["schema"]["maxLength"] == 200

    for path, method in (
        ("/v1/contract-reviews/{review_id}", "get"),
        ("/v1/contract-reviews/{review_id}/result", "get"),
        ("/v1/contract-reviews/{review_id}/cancel", "post"),
    ):
        assert all(
            parameter["name"] != "Idempotency-Key"
            for parameter in paths[path][method]["parameters"]
        )


def test_protocol_v1_reserved_arrays_have_zero_max_items() -> None:
    schema = create_app(Settings(internal_auth_enabled=False)).openapi()
    components = schema["components"]["schemas"]

    assert components["ReviewResultData"]["properties"]["relationships"]["maxItems"] == 0
    assert components["Evidence"]["properties"]["bounding_boxes"]["maxItems"] == 0
    assert "quoted_text_hash" not in components["EvidenceCandidate"]["required"]


def test_framework_callback_openapi_exposes_both_discriminators() -> None:
    schema = create_app(Settings(internal_auth_enabled=False)).openapi()
    callback_schema = schema["paths"][
        "/v1/internal/contract-reviews/{review_id}/framework-result"
    ]["post"]["requestBody"]["content"]["application/json"]["schema"]
    stage_result_schema = schema["components"]["schemas"]["StageResultCallback"]["properties"][
        "result"
    ]

    assert callback_schema["discriminator"]["propertyName"] == "callback_type"
    assert len(callback_schema["oneOf"]) == 3
    assert stage_result_schema["discriminator"]["propertyName"] == "result_type"
    assert len(stage_result_schema["oneOf"]) == 10


def test_hidden_risk_plan_interface_does_not_change_frozen_openapi() -> None:
    schema = create_app(Settings(internal_auth_enabled=False)).openapi()

    assert "/v1/internal/contract-reviews/{review_id}/risk-plan" not in schema["paths"]
    assert "/v1/internal/contract-reviews/{review_id}/revision-drafts" not in schema["paths"]
    assert "/v1/internal/contract-reviews/{review_id}/revision-drafts:generate" not in schema["paths"]
