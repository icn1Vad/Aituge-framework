"""In-process registry populated by business capabilities at mount time."""

from __future__ import annotations

from .models import FormWorkflowDefinition


_WORKFLOWS: dict[str, FormWorkflowDefinition] = {}


def register_workflow_definition(definition: FormWorkflowDefinition) -> None:
    workflow_type = definition.workflow_type.strip().upper()
    if not workflow_type:
        raise ValueError("workflow_type is required")
    if workflow_type in _WORKFLOWS and _WORKFLOWS[workflow_type] != definition:
        raise ValueError(f"Workflow definition '{workflow_type}' is already registered")
    _WORKFLOWS[workflow_type] = definition


def get_workflow_definition(
    workflow_type: str | None,
) -> FormWorkflowDefinition | None:
    normalized = str(workflow_type or "").strip().upper()
    return _WORKFLOWS.get(normalized)


def get_workflow_definitions() -> tuple[FormWorkflowDefinition, ...]:
    return tuple(_WORKFLOWS.values())


def clear_workflow_definitions() -> None:
    """Test-only reset hook."""

    _WORKFLOWS.clear()
