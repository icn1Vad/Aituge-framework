from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from service.structured_form.ai_interpreter import (
    AiFormCommandInterpreter,
    can_interpret_form_command,
    normalize_form_command,
)
from service.structured_form.models import FormFieldDefinition, FormWorkflowDefinition
from service.structured_form.registry import (
    clear_workflow_definitions,
    register_workflow_definition,
)


WORKFLOW = FormWorkflowDefinition(
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
            key="cabin",
            label="舱位",
            aliases=("座席",),
            field_type="enum",
            enum_values=("经济舱", "商务舱", "一等座", "二等座"),
        ),
        FormFieldDefinition(
            key="passenger",
            label="乘机人",
            aliases=("出差人", "出行人"),
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


@pytest.fixture(autouse=True)
def workflow_registry() -> None:
    clear_workflow_definitions()
    register_workflow_definition(WORKFLOW)
    yield
    clear_workflow_definitions()


def form_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "active_workflow": "TRAVEL_APPLICATION",
        "active_resource_id": "WD-20260826-001",
        "draft_version": 7,
        "form": {
            "departureDate": "2026-08-26",
            "travelMode": "机票",
            "cabin": None,
            "passenger": "",
        },
    }
    payload.update(overrides)
    return payload


def test_normalizes_single_field_without_alias_hardcoding() -> None:
    decision = normalize_form_command(
        json.dumps(
            {
                "action": "apply_changes",
                "changes": [{"field_key": "passenger", "value": "商泽涛"}],
            },
            ensure_ascii=False,
        ),
        WORKFLOW,
    )

    assert decision.action == "apply_changes"
    assert [(item.field_key, item.value) for item in decision.changes] == [
        ("passenger", "商泽涛")
    ]
    arguments = decision.arguments(
        draft_id="WD-20260826-001",
        expected_version=7,
    )
    assert arguments["draft_id"] == "WD-20260826-001"
    assert arguments["expected_version"] == 7
    assert arguments["changes"] == [
        {"field_key": "passenger", "value": "商泽涛", "source": "ai"}
    ]


def test_normalizes_multi_field_date_enum_and_number() -> None:
    decision = normalize_form_command(
        json.dumps(
            {
                "action": "apply_changes",
                "changes": [
                    {"field_key": "departureDate", "value": "2026-08-27"},
                    {"field_key": "travelMode", "value": "高铁"},
                    {"field_key": "cabin", "value": "二等座"},
                    {"field_key": "applicationAmount", "value": "2999.91"},
                ],
            },
            ensure_ascii=False,
        ),
        WORKFLOW,
    )

    values = {item.field_key: item.value for item in decision.changes}
    assert values == {
        "departureDate": "2026-08-27",
        "travelMode": "高铁",
        "cabin": "二等座",
        "applicationAmount": 2999.91,
    }


@pytest.mark.parametrize(
    "payload",
    (
        {"action": "apply_changes", "changes": [{"field_key": "unknown", "value": "x"}]},
        {
            "action": "apply_changes",
            "changes": [{"field_key": "budgetSubject", "value": "差旅费"}],
        },
        {
            "action": "apply_changes",
            "changes": [{"field_key": "travelMode", "value": "轮船"}],
        },
        {
            "action": "apply_changes",
            "changes": [{"field_key": "departureDate", "value": "明天"}],
        },
        {
            "action": "apply_changes",
            "changes": [
                {"field_key": "passenger", "value": "甲"},
                {"field_key": "passenger", "value": "乙"},
            ],
        },
    ),
)
def test_rejects_unknown_readonly_invalid_or_duplicate_changes(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        normalize_form_command(json.dumps(payload, ensure_ascii=False), WORKFLOW)


def test_clarification_and_defer_are_explicit_decisions() -> None:
    clarify = normalize_form_command(
        '{"action":"clarify","clarification":"您要修改出发日期还是申请日期？"}',
        WORKFLOW,
    )
    defer = normalize_form_command('{"action":"defer"}', WORKFLOW)

    assert clarify.action == "clarify"
    assert clarify.clarification == "您要修改出发日期还是申请日期？"
    assert defer.action == "defer"


def test_only_active_versioned_form_uses_interpreter() -> None:
    assert can_interpret_form_command(form_payload())
    assert not can_interpret_form_command(form_payload(active_resource_id=""))
    assert not can_interpret_form_command(form_payload(draft_version=None))
    assert not can_interpret_form_command(form_payload(active_workflow="UNKNOWN"))


@dataclass
class FakeCompletion:
    content: str
    prompt_tokens: int = 120
    completion_tokens: int = 35
    total_tokens: int = 155
    model_duration_ms: int = 900
    time_to_first_token_ms: int = 240
    terminal_finalizer: None = None


class FakeRuntime:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[dict[str, Any]] = []

    async def complete_with_usage(self, **kwargs: Any) -> FakeCompletion:
        self.calls.append(kwargs)
        return FakeCompletion(self.content)


@pytest.mark.asyncio
async def test_interpreter_uses_compact_catalog_shanghai_time_and_no_thinking() -> None:
    runtime = FakeRuntime(
        '{"action":"apply_changes","changes":'
        '[{"field_key":"departureDate","value":"2026-08-27"}]}'
    )
    interpreter = AiFormCommandInterpreter(runtime)  # type: ignore[arg-type]

    decision = await interpreter.interpret(
        form_payload(),
        "明天出发",
        model_id="deepseek-v4-flash",
        trace_id="trace-1",
        now=datetime(2026, 8, 26, 16, 30, tzinfo=timezone.utc),
    )

    assert decision is not None
    assert decision.changes[0].value == "2026-08-27"
    assert decision.model_duration_ms == 900
    call = runtime.calls[0]
    assert call["model_id"] == "deepseek-v4-flash"
    assert call["thinking_override"] is False
    assert call["response_format"] == {"type": "json_object"}
    request = json.loads(call["messages"][0]["content"])
    assert request["timezone"] == "Asia/Shanghai"
    assert request["current_time"].startswith("2026-08-27T00:30:00")
    assert request["user_message"] == "明天出发"
    assert {field["field_key"] for field in request["writable_fields"]} == {
        "departureDate",
        "travelMode",
        "cabin",
        "passenger",
        "applicationAmount",
    }
    assert "budgetSubject" not in {
        field["field_key"] for field in request["writable_fields"]
    }
