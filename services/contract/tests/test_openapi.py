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


def test_protocol_v1_reserved_arrays_have_zero_max_items() -> None:
    schema = create_app(Settings(internal_auth_enabled=False)).openapi()
    components = schema["components"]["schemas"]

    assert components["ReviewResultData"]["properties"]["relationships"]["maxItems"] == 0
    assert components["Evidence"]["properties"]["bounding_boxes"]["maxItems"] == 0
