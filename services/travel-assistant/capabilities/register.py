"""Travel business adapter for the generic Single Agent form capability."""

from __future__ import annotations

from typing import Any

from aituge_model.config import ModelRuntimeProvider
from pydantic import BaseModel, ConfigDict, Field

from service.structured_form import (
    FormFieldDefinition,
    FormWorkflowDefinition,
    create_apply_form_changes_bundle,
    register_workflow_definition,
)

CAPABILITY_ID = "travel-assistant"


class WorkflowAssistantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    active_workflow: str = Field(min_length=1, max_length=64)
    active_resource_type: str = Field(min_length=1, max_length=64)
    active_resource_id: str = Field(min_length=1, max_length=64)
    draft_version: int = Field(ge=1)
    form: dict[str, Any]
    field_sources: dict[str, str] = Field(default_factory=dict)
    model_id: str | None = Field(default=None, min_length=1, max_length=64)


def _field(
    key: str,
    label: str,
    *aliases: str,
    field_type: str = "text",
    enum_values: tuple[str, ...] = (),
    ai_writable: bool = True,
) -> FormFieldDefinition:
    return FormFieldDefinition(
        key=key,
        label=label,
        aliases=tuple(aliases),
        field_type=field_type,
        enum_values=enum_values,
        ai_writable=ai_writable,
    )


TRIP_WORKFLOW = FormWorkflowDefinition(
    workflow_type="TRAVEL_APPLICATION",
    resource_type="TRAVEL_APPLICATION",
    fields=(
        _field("company", "公司", "申请公司"),
        _field("department", "部门", "申请部门"),
        _field("expenseType", "费用类型", field_type="enum", enum_values=("差旅费_差旅交通",)),
        _field("applicationDate", "申请日期", field_type="date"),
        _field("budgetYear", "预算占用年度", "预算年度", field_type="enum", enum_values=("2026",)),
        _field("budgetSubject", "预算科目", ai_writable=False),
        _field("applicationAmount", "申请金额", "出差金额", field_type="number"),
        _field("travelMode", "出行方式", "交通方式", field_type="enum", enum_values=("机票", "高铁", "汽车")),
        _field("cabin", "舱位", "座席", field_type="enum", enum_values=("经济舱", "商务舱", "一等座", "二等座")),
        _field("departureDate", "出发日期", "启程日期", field_type="date"),
        _field("departureCity", "出发城市", "出发地"),
        _field("arrivalCity", "到达城市", "目的地"),
        _field("passenger", "乘机人", "出差人"),
        _field("overStandardReason", "超标原因"),
        _field("product", "产品", field_type="enum", enum_values=("缺省",)),
        _field("channel", "渠道", field_type="enum", enum_values=("缺省",)),
        _field("budgetDetail", "预算细项", field_type="enum", enum_values=("差旅交通费",)),
        _field("counterpartySegment", "往来段", field_type="enum", enum_values=("缺省",)),
        _field("newContract", "是否新签合同", "新签合同", field_type="enum", enum_values=("是", "否")),
        _field("notes", "备注", "出差事由"),
    ),
)

REIMBURSEMENT_WORKFLOW = FormWorkflowDefinition(
    workflow_type="TRAVEL_REIMBURSEMENT",
    resource_type="TRAVEL_REIMBURSEMENT",
    fields=(
        _field("expenseType", "费用类型", field_type="enum", enum_values=("差旅费", "住宿费", "交通费")),
        _field("occurrenceDate", "发生日期", "费用日期", field_type="date"),
        _field("invoiceBuyerName", "发票购方名称", ai_writable=False),
        _field("reimbursementCompany", "发票报销公司", "报销公司", field_type="enum", enum_values=("华泰保险集团本部",)),
        _field("taxInclusiveAmount", "报销含税金额", "含税金额", field_type="number"),
        _field("taxExclusiveAmount", "报销合计不含税金额", "不含税金额", field_type="number", ai_writable=False),
        _field("taxAmount", "报销合计税额", "税额", field_type="number", ai_writable=False),
        _field("notes", "备注", "报销说明"),
    ),
)


def _register_workflows() -> None:
    register_workflow_definition(TRIP_WORKFLOW)
    register_workflow_definition(REIMBURSEMENT_WORKFLOW)


async def register(registry, settings) -> None:
    _register_workflows()
    runtime = ModelRuntimeProvider.from_environment(
        directory=settings.get("MODEL_CONFIG_DIR"),
        pack_id=settings.get("MODEL_PACK_ID"),
    )
    model_id = runtime.active_pack.llm.id

    registry.register_local_tool(
        tool_name="apply_form_changes",
        provider="structured-form-command",
        display_name="Apply Form Changes",
        description="Validate and emit one or more changes for the Java form boundary.",
        factory=create_apply_form_changes_bundle,
    )
    registry.register_agent(
        agent_id="workflow-assistant-agent",
        name="Structured Workflow Assistant",
        description="Handles structured business forms through reusable tools.",
        model_id=model_id,
        system_prompt=(
            "你是企业事务办理助手。当前表单、字段来源、草稿编号和版本均在任务上下文中。"
            "明确的字段修改必须调用 apply_form_changes，不得只用文字声称已经修改。"
            "一次可提交一个或多个 changes；field_key 只能使用上下文提供的字段键。"
            "如果用户没有说清楚要改哪个字段，先追问，禁止猜测。"
            "不要提交正式业务单据，最终提交仍由用户确认和 Java 业务服务完成。"
        ),
        default_tools=["apply_form_changes"],
        default_datasets=[],
    )
    registry.register_resource_task(
        resource_pool="workflow-draft",
        access_mode="write",
        task_type="workflow.assistant.chat",
        name="Structured Workflow Assistant",
        description="Continue a business form conversation and emit validated field changes.",
        handler="scheduler",
        default_agent_id="workflow-assistant-agent",
        default_tools=["apply_form_changes"],
        default_datasets=[],
        conversation_message_field="question",
        input_model=WorkflowAssistantInput,
        stream_chunk_chars=24,
    )
