"""Reusable structured-form capability for Single Agent workflows."""

from .fast_path import FastFormChange, match_explicit_form_change
from .models import FormFieldDefinition, FormWorkflowDefinition
from .registry import get_workflow_definition, register_workflow_definition
def __getattr__(name):
    # Field definitions and deterministic edits do not need the agent/tool runtime.
    if name in {"ApplyFormChangesInput", "StartWorkflowInput", "create_apply_form_changes_bundle", "create_start_workflow_bundle"}:
        from . import tool
        return getattr(tool, name)
    raise AttributeError(name)

__all__ = [
    "ApplyFormChangesInput",
    "FastFormChange",
    "FormFieldDefinition",
    "FormWorkflowDefinition",
    "StartWorkflowInput",
    "create_apply_form_changes_bundle",
    "get_workflow_definition",
    "create_start_workflow_bundle",
    "match_explicit_form_change",
    "register_workflow_definition",
]
