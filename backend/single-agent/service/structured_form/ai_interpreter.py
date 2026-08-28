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
from .registry import get_workflow_definition, get_workflow_definitions


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_CLARIFICATION_CHARS = 240


@dataclass(frozen=True, slots=True)
class InterpretedFormChange:
    field_key: str
    field_label: str
    value: Any


@dataclass(frozen=True, slots=True)
class FormCommandDecision:
    action: Literal["start_workflow", "apply_changes", "clarify", "defer"]
    changes: tuple[InterpretedFormChange, ...] = ()
    workflow_type: str = ""
    clarification: str = ""
    model_duration_ms: int = 0
    time_to_first_token_ms: int | None = None
    usage: dict[str, int] | None = None

    def arguments(
        self,
        *,
        draft_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        changes = [
            {"field_key": item.field_key, "value": item.value, "source": "ai"}
            for item in self.changes
        ]
        if self.action == "start_workflow":
            return {"workflow_type": self.workflow_type, "changes": changes}
        if not draft_id or not isinstance(expected_version, int):
            raise ValueError("draft_id and expected_version are required for form edits")
        return {
            "request_id": f"ai-form-{uuid4().hex}",
            "draft_id": draft_id,
            "expected_version": expected_version,
            "changes": changes,
        }

    def success_message(self) -> str:
        if self.action == "start_workflow":
            if not self.changes:
                return "已为您打开新的业务申请草稿，请继续补充信息。"
            details = "、".join(
                f"{item.field_label}：{item.value}" for item in self.changes
            )
            return f"已为您生成申请草稿，并填写 {len(self.changes)} 项内容：{details}。"
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
        active_context = _active_form_context(payload)
        start_context = _start_workflow_context(payload)
        if active_context is None and start_context is None:
            return None
        if active_context is not None:
            definition, current_form = active_context
            user_payload = _build_user_payload(message, definition, current_form, now=now)
            system_prompt = _EDIT_SYSTEM_PROMPT
        else:
            definition = None
            user_payload = _build_start_user_payload(message, start_context or (), now=now)
            system_prompt = _START_SYSTEM_PROMPT
        active_model_id = (
            model_id
            or self.llm_runtime.model_runtime_provider.active_pack.llm.id
        )
        completion = await self.llm_runtime.complete_with_usage(
            messages=[
                {
                    "role": "user",
                    "content": user_payload,
                }
            ],
            model_id=active_model_id,
            system_prompt=system_prompt,
            max_tokens=640,
            temperature=0,
            thinking_override=False,
            response_format={"type": "json_object"},
            review_unit_id="structured_form_interpreter",
            trace_id=trace_id,
            defer_terminal=True,
        )
        try:
            metrics = {
                "model_duration_ms": completion.model_duration_ms,
                "time_to_first_token_ms": completion.time_to_first_token_ms,
                "usage": _completion_usage(completion),
            }
            if definition is not None:
                decision = normalize_form_command(
                    completion.content,
                    definition,
                    **metrics,
                )
            else:
                decision = normalize_start_workflow_command(
                    completion.content,
                    start_context or (),
                    **metrics,
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
    return (
        _active_form_context(payload) is not None
        or _start_workflow_context(payload) is not None
    )


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

    changes = _normalize_changes(
        payload.get("changes"),
        definition,
        allow_empty=False,
    )
    return FormCommandDecision(
        action="apply_changes",
        changes=tuple(changes),
        model_duration_ms=model_duration_ms,
        time_to_first_token_ms=time_to_first_token_ms,
        usage=usage,
    )


def normalize_start_workflow_command(
    content: str,
    definitions: tuple[FormWorkflowDefinition, ...],
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

    metrics = {
        "model_duration_ms": model_duration_ms,
        "time_to_first_token_ms": time_to_first_token_ms,
        "usage": usage,
    }
    action = payload.get("action")
    if action == "defer":
        return FormCommandDecision(action="defer", **metrics)
    if action == "clarify":
        clarification = str(payload.get("clarification") or "").strip()
        if not clarification or len(clarification) > _MAX_CLARIFICATION_CHARS:
            raise ValueError("clarification is missing or too long")
        return FormCommandDecision(
            action="clarify",
            clarification=clarification,
            **metrics,
        )
    if action != "start_workflow":
        raise ValueError("unsupported action")

    workflow_type = str(payload.get("workflow_type") or "").strip().upper()
    definition = next(
        (item for item in definitions if item.workflow_type == workflow_type),
        None,
    )
    if definition is None:
        raise ValueError("unknown workflow_type")
    changes = _normalize_changes(
        payload.get("changes", []),
        definition,
        allow_empty=True,
    )
    return FormCommandDecision(
        action="start_workflow",
        workflow_type=workflow_type,
        changes=tuple(changes),
        **metrics,
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


def _start_workflow_context(
    payload: dict[str, Any],
) -> tuple[FormWorkflowDefinition, ...] | None:
    active_workflow = str(payload.get("active_workflow") or "").strip().upper()
    if (
        not active_workflow.endswith("_ASSISTANT")
        or payload.get("active_resource_id")
    ):
        return None
    if get_workflow_definition(active_workflow) is not None:
        return None
    definitions = get_workflow_definitions()
    return definitions or None


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
            "workflow_instructions": list(definition.instructions),
            "user_message": message,
            "writable_fields": fields,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _build_start_user_payload(
    message: str,
    definitions: tuple[FormWorkflowDefinition, ...],
    *,
    now: datetime | None,
) -> str:
    current = (now or datetime.now(_SHANGHAI)).astimezone(_SHANGHAI)
    workflows = []
    for definition in definitions:
        workflows.append(
            {
                "workflow_type": definition.workflow_type,
                "workflow_instructions": list(definition.instructions),
                "writable_fields": [
                    {
                        "field_key": field.key,
                        "label": field.label,
                        "aliases": list(field.aliases),
                        "type": field.field_type,
                        "enum_values": list(field.enum_values),
                    }
                    for field in definition.writable_fields()
                ],
            }
        )
    return json.dumps(
        {
            "current_time": current.isoformat(timespec="seconds"),
            "timezone": "Asia/Shanghai",
            "user_message": message,
            "available_workflows": workflows,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _normalize_changes(
    raw_changes: Any,
    definition: FormWorkflowDefinition,
    *,
    allow_empty: bool,
) -> list[InterpretedFormChange]:
    if not isinstance(raw_changes, list):
        raise ValueError("changes must be a list")
    minimum = 0 if allow_empty else 1
    if not minimum <= len(raw_changes) <= 100:
        raise ValueError(f"changes must contain between {minimum} and 100 items")
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
    return changes


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


_START_SYSTEM_PROMPT = """你是通用业务事务启动解析器，不是聊天助手。
判断用户当前这句话是否明确要求新建 available_workflows 中的一种业务事项。
明确要发起事项时返回：
{"action":"start_workflow","workflow_type":"业务类型","changes":[{"field_key":"字段键","value":"规范值"}]}
只要能够确定 workflow_type，就必须立即返回 start_workflow；即使用户没有提供任何表单字段，也返回空 changes 创建草稿。
费用类型、日期、金额等 writable_fields 缺失或未确定时，不得因此追问；这些信息应在草稿打开后继续补充。
例如“我要报销”应直接启动 TRAVEL_REIMBURSEMENT 且 changes 为空，不得追问报销费用类型。
用户明确要办理但业务类型无法确定时返回：
{"action":"clarify","clarification":"一句简短、具体的追问"}
制度咨询、费用标准查询、闲聊、上传附件、修改旧事项等不应新建事项的请求返回：
{"action":"defer"}
只能选择 available_workflows 中的 workflow_type 和 writable_fields，不得创造字段。
用户已经明确提供的全部字段要一次提取；必须遵守 workflow_instructions，相关语义独立的字段要同时输出。
日期必须结合 current_time 和 timezone 解析为 YYYY-MM-DD；“明天”“后天”等不得按 UTC 计算。
枚举值必须严格使用 enum_values 中的值。交通方式与舱位是独立字段，语义一致时必须同时输出。
二者明显冲突时保留用户明确说出的交通方式，只省略不匹配的舱位；例如“飞机二等座”输出 travelMode=机票，不输出 cabin。
数字输出 JSON 数字。不要输出 Markdown、解释或工具调用，只输出一个 JSON 对象。"""


_EDIT_SYSTEM_PROMPT = """你是通用业务表单指令解析器，不是聊天助手。
只判断用户当前这句话是否要求修改已经打开的表单。
字段只能从 writable_fields 中按语义选择，不得创造字段，不得修改只读字段。
必须遵守 workflow_instructions 中的业务语义要求；相关字段语义独立时要同时输出，不得用一个字段代替另一个字段。
明确修改一个或多个字段时返回：
{"action":"apply_changes","changes":[{"field_key":"字段键","value":"规范值"}]}
用户确实想修改但字段或取值有歧义时返回：
{"action":"clarify","clarification":"一句简短、具体的追问"}
制度咨询、新建事项、闲聊、确认提交、上传附件等非当前表单字段修改返回：
{"action":"defer"}
日期必须结合 current_time 和 timezone 解析为 YYYY-MM-DD；“明天”等相对日期不得按 UTC 计算。
枚举值必须严格使用 enum_values 中的值，数字输出 JSON 数字。
不要输出 Markdown、解释、工具调用、request_id、draft_id 或版本号，只输出一个 JSON 对象。"""
