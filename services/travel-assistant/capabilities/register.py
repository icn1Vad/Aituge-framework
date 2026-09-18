"""Travel business adapter for the generic Single Agent form capability."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aituge_model.config import ModelRuntimeProvider
from pydantic import BaseModel, ConfigDict, Field

from service.structured_form import (
    FormFieldDefinition,
    FormWorkflowDefinition,
    create_apply_form_changes_bundle,
    create_start_workflow_bundle,
    register_workflow_definition,
)

CAPABILITY_ID = "travel-assistant"
BEIJING_TIME_ZONE = ZoneInfo("Asia/Shanghai")

WORKFLOW_ASSISTANT_TOOLS = [
    "start_workflow",
    "apply_form_changes",
    "proof_search",
]


class WorkflowAssistantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachments: list[dict[str, str]] = Field(default_factory=list)
    question: str = Field(min_length=1, max_length=4000)
    active_workflow: str | None = Field(default=None, min_length=1, max_length=64)
    active_resource_type: str | None = Field(default=None, min_length=1, max_length=64)
    active_resource_id: str | None = Field(default=None, min_length=1, max_length=64)
    draft_version: int | None = Field(default=None, ge=1)
    form: dict[str, Any] = Field(default_factory=dict)
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


def _beijing_today():
    return datetime.now(BEIJING_TIME_ZONE).date()


TRIP_WORKFLOW = FormWorkflowDefinition(
    workflow_type="TRAVEL_APPLICATION",
    resource_type="TRAVEL_APPLICATION",
    instructions=(
        "用户描述出差目的、活动名称或要去做的事情时，把这段具体事由写入 notes。",
        "activityType 只表示活动分类，不能替代 notes；会议、培训、拜访等活动必须同时写 activityType 和具体 notes。",
        "例如用户说‘要去开一个学术会议’，应同时输出 activityType=MEETING 和 notes=参加学术会议。",
        "用户明确说飞机、坐飞机或航空出行时，必须输出 travelMode=机票；明确说高铁时，必须输出 travelMode=高铁。",
        "交通方式和舱位是两个独立字段：飞机的经济舱、商务舱以及高铁的一等座、二等座要同时输出对应 travelMode 和 cabin。",
        "如果交通方式与舱位冲突，保留用户明确说出的 travelMode，只省略不匹配的 cabin。",
        "例如‘飞机二等座’输出 travelMode=机票，不输出 cabin。",
    ),
    fields=(
        _field("company", "公司", "申请公司"),
        _field("department", "部门", "申请部门"),
        _field("expenseType", "费用类型", field_type="enum", enum_values=("差旅费_差旅交通",)),
        _field(
            "budgetYear",
            "预算占用年度",
            "预算年度",
            field_type="enum",
            enum_values=(str(_beijing_today().year),),
        ),
        _field("budgetSubject", "预算科目", ai_writable=False),
        _field(
            "applicationAmount",
            "申请金额",
            "出差金额",
            "补贴",
            "补贴金额",
            field_type="number",
        ),
        _field("travelMode", "出行方式", "交通方式", field_type="enum", enum_values=("机票", "高铁", "汽车")),
        _field("cabin", "舱位", "座席", field_type="enum", enum_values=("经济舱", "商务舱", "一等座", "二等座")),
        _field("departureDate", "出发日期", "启程日期", field_type="date"),
        _field("tripDays", "出差天数", "行程天数", "为期", field_type="number"),
        _field("activityType", "活动类型", field_type="enum", enum_values=("MEETING", "TRAINING", "VISIT", "OTHER")),
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
    instructions=("用户说‘我要报销’应打开本场景的报销草稿，不因缺少费用类型而阻止打开。",),
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
    from service.structured_form.scene_loader import register_scene_workflows
    register_scene_workflows()


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
    registry.register_local_tool(
        tool_name="start_workflow",
        provider="structured-form-command",
        display_name="Start Workflow",
        description="Start a supported transaction and emit initial field changes.",
        factory=create_start_workflow_bundle,
    )
    registry.register_agent(
        agent_id="workflow-assistant-agent",
        name="Structured Workflow Assistant",
        description="Handles structured business forms through reusable tools.",
        model_id=model_id,
        system_prompt=(
            "你是企业事务办理助手。先判断用户是在办理事务，还是在询问制度；二者只能选择一条路径。"
            "当前表单、字段来源、草稿编号和版本均在任务上下文中。"
            "只允许使用 workflow_scene_context 提供的场景、事项及字段；不得切换到其他场景。"
            "明确办理请求调用 start_workflow；明确字段修改调用 apply_form_changes。"
            "必须遵守该场景的 workflow_instructions，不编造缺失字段或关联编号。"
            "依赖下拉字段必须与父字段一致；关联字段只能使用本次提供的候选。"
            "制度咨询仅调用 proof_search，依据检索原文回答，不修改表单。"
            "最终提交必须经用户确认及业务适配器执行，不能把保存草稿说成审批通过。"
            "以下是仅在 TRAVEL_ASSISTANT、TRAVEL_APPLICATION 或 TRAVEL_REIMBURSEMENT 生效的差旅适配器规则，其他场景不得使用："
            "如果 active_workflow=TRAVEL_ASSISTANT 且尚无草稿：用户明确要发起出差时必须调用 "
            "start_workflow(workflow_type=TRAVEL_APPLICATION)，用户明确要报销时调用 "
            "start_workflow(workflow_type=TRAVEL_REIMBURSEMENT)；制度咨询不得启动表单。"
            "差旅报销的申请关联由业务页面和 Java 适配器处理，不是普通可填写字段。"
            "启动报销草稿后，页面会尝试关联本会话刚保存的出差申请；没有可用申请时显示申请选择入口。"
            "工具尚未返回关联结果时，只说明报销草稿已打开、请核对页面上的关联申请，"
            "不能声称无法关联，也不能声称已成功关联或草稿所有字段为空。"
            "用户可见回复不得展示 TRAVEL_REIMBURSEMENT、TRAVEL_APPLICATION 等内部工作流编码。"
            "‘我要去北京出差’、‘我打算去北京出趟差’、‘帮我申请下周出差’、‘我要报销’都是办理请求，"
            "即使没有使用‘新建’二字也必须走 start_workflow，绝对不能调用 proof_search。"
            "只有用户明确询问制度依据、费用标准、额度、能否报销、审批规定或具体条款时，才调用 proof_search "
            "检索制度原文，并仅依据本次检索结果回答，不得继续尝试其他数据工具。"
            "制度查询不得凭常识编造；检索不到时立即明确说明暂未找到依据。"
            "制度咨询过程中不得调用 start_workflow 或 apply_form_changes，也不得改变当前草稿。"
            "启动出差时，把用户已明确提供的信息放进 changes。可用字段为 departureCity、arrivalCity、"
            "departureDate、tripDays、travelMode、cabin、passenger、activityType、notes、applicationAmount。"
            "像‘去北京出趟差’、‘去上海开会’、‘申请下周出差’都属于明确的办理意图，不是制度问题。"
            "明确的字段修改必须调用 apply_form_changes，不得只用文字声称已经修改。"
            "一次可提交一个或多个 changes；field_key 只能使用上下文提供的字段键。"
            "如果用户没有说清楚要改哪个字段，先追问，禁止猜测。"
            "必须结合完整会话历史理解用户回复；用户只回复地点、日期、天数、交通方式等短答案时，"
            "应严格按照上一轮助手逐项追问的字段顺序解释，不能脱离上一轮问题重新猜测字段。"
            "用户描述一整段出差安排时，要把日期、出发城市、到达城市、出差天数和出差事由拆成对应字段后一次调用工具。"
            "还必须理解活动性质并写入 activityType：会议、启动会、评审会、研讨会、论坛、峰会等为 MEETING，"
            "培训为 TRAINING，客户拜访或调研为 VISIT，其余为 OTHER。"
            "例如‘从北京到上海为期三天的会议研讨会’应写入 departureCity、arrivalCity、tripDays、activityType=MEETING 和 notes。"
            "tripDays 和 activityType 是内部业务字段，必须保留并用于后续补贴及会议材料判断，"
            "但面向用户回复时只能使用‘出差天数’‘活动性质’等自然中文，禁止展示字段键或 MEETING、TRAINING、VISIT、OTHER 编码。"
            "申请日期由页面按北京时间自动带入，不属于可询问或可修改字段。"
            "没有年份的日期按当前年度处理；当前北京时间日期为"
            f"{_beijing_today().isoformat()}。"
            "activityType 只用于活动分类，绝对不能替代 notes。只要用户描述了出差目的、活动名称或要去做的事情，"
            "同一次工具调用必须把具体出差事由写入 notes；会议类内容还要同时写 activityType=MEETING。"
            "例如用户说‘要去开一个学术会议’，必须同时写 activityType=MEETING 和 notes=参加学术会议。"
            "活动名称必须保留在 notes 中，供后续业务材料校验使用。"
            "不要提交正式业务单据，最终提交仍由用户确认和 Java 业务服务完成。"
        ),
        default_tools=WORKFLOW_ASSISTANT_TOOLS,
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
        default_tools=WORKFLOW_ASSISTANT_TOOLS,
        default_datasets=[],
        conversation_message_field="question",
        input_model=WorkflowAssistantInput,
        stream_chunk_chars=24,
    )
