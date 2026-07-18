from __future__ import annotations

import json

from pydantic import BaseModel, model_validator

from task_manager.payload_schemas import register_output_schema, validate_output_payload


class _OutputWithModelError(BaseModel):
    value: int

    @model_validator(mode="after")
    def reject_value(self):
        raise ValueError("invalid model value")


def test_output_validation_errors_are_json_serializable() -> None:
    schema_name = "test_output_with_model_error"
    register_output_schema(schema_name, _OutputWithModelError)

    is_valid, error = validate_output_payload(schema_name, {"value": 1})

    assert is_valid is False
    assert error is not None
    assert error["errors"][0]["ctx"]["error"] == "invalid model value"
    json.dumps(error)
