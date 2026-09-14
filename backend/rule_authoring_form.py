"""Rule fields registered in the same form machinery as travel reimbursement."""
from service.structured_form.models import FormFieldDefinition, FormWorkflowDefinition
from service.structured_form.registry import register_workflow_definition
from service.structured_form.fast_path import match_explicit_form_change


RULE_FORM = FormWorkflowDefinition(
    workflow_type="REVIEW_RULE_AUTHORING", resource_type="REVIEW_RULE",
    fields=(
        FormFieldDefinition("name", "规则名称", ("名称",)),
        FormFieldDefinition("reviewDirection", "审查方向"),
        FormFieldDefinition("partyStance", "适用立场", ("立场",)),
        FormFieldDefinition("reviewStandard", "审查标准", field_type="enum", enum_values=("强势", "中立", "弱势", "strong", "neutral", "weak")),
        FormFieldDefinition("content", "规则内容", ("审查内容",)),
        FormFieldDefinition("reviewMethod", "审查方式"),
        FormFieldDefinition("referenceBasis", "参考依据", ("依据",)),
        FormFieldDefinition("jurisdiction", "适用地域"),
        # Dates deliberately require a full year in the rule-specific validator.
        FormFieldDefinition("effectiveFrom", "生效日期"),
        FormFieldDefinition("effectiveTo", "失效日期"),
        FormFieldDefinition("status", "状态", ai_writable=False),
        FormFieldDefinition("source", "来源", ai_writable=False),
    ),
)


def explicit_change(draft, message):
    register_workflow_definition(RULE_FORM)
    change = match_explicit_form_change({"active_workflow": RULE_FORM.workflow_type,
        "active_resource_id": "unsaved-rule", "draft_version": 1}, message)
    if change is None:
        return None
    value = change.value
    if change.field_key == "reviewStandard":
        value = {"强势": "strong", "中立": "neutral", "弱势": "weak"}.get(value, value)
    updated = draft.model_dump()
    updated[change.field_key] = value
    return updated, change.field_label
