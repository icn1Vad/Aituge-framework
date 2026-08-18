from __future__ import annotations

import pytest
from pydantic import ValidationError

from service.structured_form.fast_path import match_explicit_form_change
from service.structured_form.models import FormFieldDefinition, FormWorkflowDefinition
from service.structured_form.registry import (
    clear_workflow_definitions,
    register_workflow_definition,
)
from service.structured_form.tool import ApplyFormChangesInput, StartWorkflowInput


@pytest.fixture(autouse=True)
def workflow_registry() -> None:
    clear_workflow_definitions()
    register_workflow_definition(
        FormWorkflowDefinition(
            workflow_type="TRAVEL_APPLICATION",
            resource_type="TRAVEL_APPLICATION",
            fields=(
                FormFieldDefinition(
                    key="departureDate",
                    label="出发日期",
                    aliases=("启程日期",),
                    field_type="date",
                ),
                FormFieldDefinition(
                    key="travelMode",
                    label="出行方式",
                    aliases=("交通方式",),
                    field_type="enum",
                    enum_values=("机票", "高铁", "汽车"),
                ),
                FormFieldDefinition(
                    key="applicationAmount",
                    label="申请金额",
                    field_type="number",
                ),
                FormFieldDefinition(
                    key="budgetSubject",
                    label="预算科目",
                    ai_writable=False,
                ),
            ),
        )
    )
    yield
    clear_workflow_definitions()


def payload(**overrides):
    value = {
        "active_workflow": "TRAVEL_APPLICATION",
        "active_resource_id": "WD-20260816-001",
        "draft_version": 3,
    }
    value.update(overrides)
    return value


def test_explicit_date_edit_uses_deterministic_change() -> None:
    change = match_explicit_form_change(payload(), "把出发日期改成8月20日")

    assert change is not None
    assert change.field_key == "departureDate"
    assert change.value.endswith("-08-20")
    assert change.expected_version == 3
    assert change.arguments()["changes"] == [
        {"field_key": "departureDate", "value": change.value, "source": "ai"}
    ]


def test_explicit_enum_alias_is_supported() -> None:
    change = match_explicit_form_change(payload(), "将交通方式修改为高铁")

    assert change is not None
    assert change.field_key == "travelMode"
    assert change.value == "高铁"


@pytest.mark.parametrize(
    "message",
    (
        "把出发日期和交通方式都改一下",
        "把相关日期全部顺延两天",
        "日期改一下",
        "把交通方式改成轮船",
        "把预算科目改成差旅费",
    ),
)
def test_ambiguous_complex_or_disallowed_edits_do_not_use_fast_path(message: str) -> None:
    assert match_explicit_form_change(payload(), message) is None


def test_missing_or_stale_draft_context_does_not_use_fast_path() -> None:
    assert match_explicit_form_change(payload(draft_version=None), "把出发日期改成8月20日") is None
    assert match_explicit_form_change(payload(active_resource_id=""), "把出发日期改成8月20日") is None

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
