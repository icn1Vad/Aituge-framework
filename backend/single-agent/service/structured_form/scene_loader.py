"""Adapt versioned scene manifests to the existing AI form interpreter."""
import json
from dataclasses import asdict
from business_workflow_kit import load_builtin_scenes
from .models import FormFieldDefinition, FormWorkflowDefinition
from .registry import register_workflow_definition, get_workflow_definition, get_workflow_definitions


def register_scene_workflows() -> None:
    for scene in load_builtin_scenes().scenes():
        for workflow in scene["workflows"]:
            if workflow["validationMode"] == "legacy-adapter":
                # Existing business definitions remain the compatibility authority.
                if get_workflow_definition(workflow["workflowType"]) is None:
                    raise ValueError("Legacy scene adapter was not registered")
                continue
            instructions = ("业务事项名称：" + workflow["title"],) + tuple(workflow.get("instructions", [])) + (
                "先执行本次已明确且有依据的字段修改；其他必填项缺失不能阻止本次回填。只有本次修改所需值缺失时才询问，不得编造。",
                "当前表单已选中的关联以 selected_references 为准，无需重复确认；只有新选或更换关联时从 reference_candidates 中确定目标。",
            )
            register_workflow_definition(FormWorkflowDefinition(
                workflow_type=workflow["workflowType"], resource_type=workflow["resourceType"],
                assistant_mode=scene["assistantMode"], instructions=instructions,
                fields=tuple(FormFieldDefinition(
                    key=f["key"], label=f["label"], aliases=tuple(f.get("aliases", [])),
                    field_type=f.get("type", "text"), enum_values=tuple(f.get("options", [])),
                    ai_writable=f.get("aiWritable", True),
                    required=f.get("required", False), depends_on=f.get("dependsOn"),
                    options_by_parent=tuple((parent, tuple(options)) for parent, options in f.get("optionsByParent", {}).items()),
                ) for f in workflow["fields"]),
            ))


def build_scene_runtime_context(payload: dict) -> str | None:
    """The fallback agent receives the same scoped definitions as the interpreter."""
    active = payload.get("active_workflow")
    current = get_workflow_definition(active)
    definitions = (current,) if current else get_workflow_definitions(assistant_mode=active)
    if not active or not definitions:
        return None
    return json.dumps({
        "active_workflow": active,
        "available_workflows": [asdict(definition) for definition in definitions],
        "reference_candidates": (payload.get("form") or {}).get("_referenceCandidates", {}),
        "selected_references": (payload.get("form") or {}).get("_selectedReferences", {}),
        "reference_candidates_by_workflow": (payload.get("form") or {}).get("_workflowReferenceCandidates", {}),
    }, ensure_ascii=False)
