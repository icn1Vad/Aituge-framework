"""A small AI boundary for understanding edits to the currently bound form."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from loguru import logger
from model_observability.runtime import (
    finalize_deferred_completion_success,
    finalize_deferred_completion_validation_failed,
)
from service.conversation.llm_runner import LlmRuntime

from .models import FormFieldDefinition, FormWorkflowDefinition
from .registry import get_workflow_definition


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_CLARIFICATION_CHARS = 240


@dataclass(frozen=True, slots=True)
class InterpretedFormChange:
    field_key: str
    field_label: str
    value: Any


@dataclass(frozen=True, slots=True)
class FormCommandDecision:
    action: Literal["apply_changes", "clarify", "defer"]
    changes: tuple[InterpretedFormChange, ...] = ()
    clarification: str = ""
    model_duration_ms: int = 0
    time_to_first_token_ms: int | None = None
    usage: dict[str, int] | None = None

    def arguments(self, *, draft_id: str, expected_version: int) -> dict[str, Any]:
        return {
            "request_id": f"ai-form-{uuid4().hex}",
            "draft_id": draft_id,
            "expected_version": expected_version,
            "changes": [
                {
                    "field_key": item.field_key,
                    "value": item.value,
                    "source": "ai",
                }
                for item in self.changes
            ],
        }

    def success_message(self) -> str:
        if len(self.changes) == 1:
            item = self.changes[0]
            return f"已将{item.field_label}修改为{item.value}。"
        details = "、".join(
            f"{item.field_label}：{item.value}" for item in self.changes
        )
        return f"已更新 {len(self.changes)} 项内容：{details}。"


class AiFormCommandInterpreter:
    """Interprets one form-edit turn without entering the ReAct tool loop."""

    def __init__(self, llm_runtime: LlmRuntime):
        self.llm_runtime = llm_runtime

    async def interpret(
        self,
        payload: dict[str, Any],
        message: str,
        *,
        model_id: str | None = None,
        trace_id: str | None = None,
        now: datetime | None = None,
    ) -> FormCommandDecision | None:
        context = _active_form_context(payload)
        if context is None:
            return None
        definition, current_form = context
        active_model_id = (
            model_id
            or self.llm_runtime.model_runtime_provider.active_pack.llm.id
        )
        completion = await self.llm_runtime.complete_with_usage(
            messages=[
                {
                    "role": "user",
                    "content": _build_user_payload(
                        message,
                        definition,
                        current_form,
                        now=now,
                    ),
                }
            ],
            model_id=active_model_id,
            system_prompt=_SYSTEM_PROMPT,
            max_tokens=480,
            temperature=0,
            thinking_override=False,
            response_format={"type": "json_object"},
            review_unit_id="structured_form_interpreter",
            trace_id=trace_id,
            defer_terminal=True,
        )
        try:
            decision = normalize_form_command(
                completion.content,
                definition,
                model_duration_ms=completion.model_duration_ms,
                time_to_first_token_ms=completion.time_to_first_token_ms,
                usage=_completion_usage(completion),
            )
        except asyncio.CancelledError:
            await finalize_deferred_completion_validation_failed(
                completion,
                "FORM_COMMAND_PROCESSING_CANCELLED",
            )
            raise
        except ValueError as exc:
            await finalize_deferred_completion_validation_failed(
                completion,
                "FORM_COMMAND_OUTPUT_INVALID",
            )
            logger.warning("Form command model output rejected: {}", str(exc))
            return FormCommandDecision(action="defer")
        except Exception:
            await finalize_deferred_completion_validation_failed(
                completion,
                "FORM_COMMAND_PROCESSING_FAILED",
            )
            raise
        await finalize_deferred_completion_success(completion)
        return decision


def can_interpret_form_command(payload: dict[str, Any]) -> bool:
    return _active_form_context(payload) is not None


def normalize_form_command(
    content: str,
    definition: FormWorkflowDefinition,
    *,
    model_duration_ms: int = 0,
    time_to_first_token_ms: int | None = None,
    usage: dict[str, int] | None = None,
) -> FormCommandDecision:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("response root must be an object")

    action = payload.get("action")
    if action == "defer":
        return FormCommandDecision(
            action="defer",
            model_duration_ms=model_duration_ms,
            time_to_first_token_ms=time_to_first_token_ms,
            usage=usage,
        )
    if action == "clarify":
        clarification = str(payload.get("clarification") or "").strip()
        if not clarification or len(clarification) > _MAX_CLARIFICATION_CHARS:
            raise ValueError("clarification is missing or too long")
        return FormCommandDecision(
            action="clarify",
            clarification=clarification,
            model_duration_ms=model_duration_ms,
            time_to_first_token_ms=time_to_first_token_ms,
            usage=usage,
        )
    if action != "apply_changes":
        raise ValueError("unsupported action")

    raw_changes = payload.get("changes")
    if not isinstance(raw_changes, list) or not 1 <= len(raw_changes) <= 100:
        raise ValueError("changes must contain between 1 and 100 items")
    fields = {field.key: field for field in definition.writable_fields()}
    seen: set[str] = set()
    changes: list[InterpretedFormChange] = []
    for raw_change in raw_changes:
        if not isinstance(raw_change, dict):
            raise ValueError("change must be an object")
        field_key = str(raw_change.get("field_key") or "").strip()
        field = fields.get(field_key)
        if field is None:
            raise ValueError(f"unknown or non-writable field: {field_key}")
        if field_key in seen:
            raise ValueError(f"duplicate field: {field_key}")
        seen.add(field_key)
        changes.append(
            InterpretedFormChange(
                field_key=field.key,
                field_label=field.label,
                value=_normalize_value(field, raw_change.get("value")),
            )
        )
    return FormCommandDecision(
        action="apply_changes",
        changes=tuple(changes),
        model_duration_ms=model_duration_ms,
        time_to_first_token_ms=time_to_first_token_ms,
        usage=usage,
    )


def _active_form_context(
    payload: dict[str, Any],
) -> tuple[FormWorkflowDefinition, dict[str, Any]] | None:
    draft_id = str(payload.get("active_resource_id") or "").strip()
    version = payload.get("draft_version")
    definition = get_workflow_definition(payload.get("active_workflow"))
    if definition is None or not draft_id or not isinstance(version, int) or version < 1:
        return None
    form = payload.get("form")
    return definition, form if isinstance(form, dict) else {}


def _build_user_payload(
    message: str,
    definition: FormWorkflowDefinition,
    current_form: dict[str, Any],
    *,
    now: datetime | None,
) -> str:
    current = (now or datetime.now(_SHANGHAI)).astimezone(_SHANGHAI)
    fields = [
        {
            "field_key": field.key,
            "label": field.label,
            "aliases": list(field.aliases),
            "type": field.field_type,
            "enum_values": list(field.enum_values),
            "current_value": current_form.get(field.key),
        }
        for field in definition.writable_fields()
    ]
    return json.dumps(
        {
            "current_time": current.isoformat(timespec="seconds"),
            "timezone": "Asia/Shanghai",
            "workflow_type": definition.workflow_type,
            "user_message": message,
            "writable_fields": fields,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _normalize_value(field: FormFieldDefinition, value: Any) -> Any:
    if value is None:
        raise ValueError(f"value is missing for {field.key}")
    if field.field_type == "text":
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"text value is empty for {field.key}")
        return normalized
    if field.field_type == "date":
        normalized = str(value).strip()
        try:
            return datetime.strptime(normalized, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise ValueError(f"date must use YYYY-MM-DD for {field.key}") from exc
    if field.field_type == "enum":
        normalized = str(value).strip()
        if normalized not in field.enum_values:
            raise ValueError(f"enum value is invalid for {field.key}")
        return normalized
    if field.field_type == "number":
        if isinstance(value, bool):
            raise ValueError(f"boolean is not a number for {field.key}")
        try:
            number = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"number is invalid for {field.key}") from exc
        if not number.is_finite():
            raise ValueError(f"number is not finite for {field.key}")
        return int(number) if number == number.to_integral_value() else float(number)
    raise ValueError(f"unsupported field type: {field.field_type}")


def _completion_usage(completion: Any) -> dict[str, int] | None:
    values = {
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
        "total_tokens": completion.total_tokens,
    }
    normalized = {
        key: int(value)
        for key, value in values.items()
        if isinstance(value, int) and value >= 0
    }
    return normalized or None


_SYSTEM_PROMPT = """你是通用业务表单指令解析器，不是聊天助手。
只判断用户当前这句话是否要求修改已经打开的表单。
字段只能从 writable_fields 中按语义选择，不得创造字段，不得修改只读字段。
明确修改一个或多个字段时返回：
{"action":"apply_changes","changes":[{"field_key":"字段键","value":"规范值"}]}
用户确实想修改但字段或取值有歧义时返回：
{"action":"clarify","clarification":"一句简短、具体的追问"}
制度咨询、新建事项、闲聊、确认提交、上传附件等非当前表单字段修改返回：
{"action":"defer"}
日期必须结合 current_time 和 timezone 解析为 YYYY-MM-DD；“明天”等相对日期不得按 UTC 计算。
枚举值必须严格使用 enum_values 中的值，数字输出 JSON 数字。
不要输出 Markdown、解释、工具调用、request_id、draft_id 或版本号，只输出一个 JSON 对象。"""
