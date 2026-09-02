from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

from pydantic import BaseModel, model_validator

from task_manager.payload_schemas import (
    get_stage_json_schema,
    register_output_schema,
    validate_stage_payload,
    validate_output_payload,
)
from task_manager.pipeline.executor import _stage_message
from task_manager.pipeline.models import StageDefinition


class _OutputWithModelError(BaseModel):
    value: int

    @model_validator(mode="after")
    def reject_value(self):
        raise ValueError("invalid model value")


class _OutputWithDate(BaseModel):
    effective_from: date


def test_output_validation_errors_are_json_serializable() -> None:
    schema_name = "test_output_with_model_error"
    register_output_schema(schema_name, _OutputWithModelError)

    is_valid, error = validate_output_payload(schema_name, {"value": 1})

    assert is_valid is False
    assert error is not None
    assert error["errors"][0]["ctx"]["error"] == "invalid model value"
    json.dumps(error)


def test_registered_stage_schema_is_available_to_model_prompt() -> None:
    schema_name = "test_stage_schema_is_available_to_model_prompt"
    register_output_schema(schema_name, _OutputWithModelError)

    schema = get_stage_json_schema(schema_name)

    assert schema is not None
    assert schema["type"] == "object"
    assert schema["properties"]["value"]["type"] == "integer"

    message = _stage_message(
        SimpleNamespace(task_type="example.task"),
        StageDefinition(
            stage_id="structured_stage",
            name="Structured stage",
            stage_type="agent",
            output_schema=schema_name,
        ),
        {"source": "contract"},
    )
    assert f"Required output schema name: {schema_name}" in message
    assert '"properties":{"value":{"title":"Value","type":"integer"}}' in message


def test_stage_schema_validation_returns_json_native_dates() -> None:
    schema_name = "test_stage_schema_returns_json_native_dates"
    register_output_schema(schema_name, _OutputWithDate)

    output = validate_stage_payload(
        schema_name,
        {"effective_from": date(2026, 8, 31)},
    )

    assert output == {"effective_from": "2026-08-31"}
    assert json.loads(json.dumps(output)) == output
