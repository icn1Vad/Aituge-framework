from __future__ import annotations

import pytest
from pydantic import ValidationError

from service.structured_form.tool import ApplyFormChangesInput, StartWorkflowInput


def test_start_workflow_accepts_initial_trip_fields() -> None:
    command = StartWorkflowInput.model_validate(
        {
            "workflow_type": "TRAVEL_APPLICATION",
            "changes": [
                {"field_key": "arrivalCity", "value": "北京", "source": "ai"},
                {"field_key": "notes", "value": "去北京出趟差", "source": "ai"},
            ],
        }
    )

    assert command.workflow_type == "TRAVEL_APPLICATION"
    assert [change.field_key for change in command.changes] == ["arrivalCity", "notes"]


def test_start_workflow_rejects_unknown_business_flow() -> None:
    with pytest.raises(ValidationError):
        StartWorkflowInput.model_validate(
            {
                "workflow_type": "PURCHASE_APPLICATION",
                "changes": [],
            }
        )


def test_apply_command_requires_version_and_at_least_one_change() -> None:
    with pytest.raises(ValidationError):
        ApplyFormChangesInput.model_validate(
            {
                "request_id": "req-1",
                "draft_id": "WD-1",
                "expected_version": 0,
                "changes": [],
            }
        )
