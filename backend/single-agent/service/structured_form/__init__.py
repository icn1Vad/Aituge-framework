"""Reusable structured-form capability for Single Agent workflows."""

from .fast_path import FastFormChange, match_explicit_form_change
from .models import FormFieldDefinition, FormWorkflowDefinition
from .registry import get_workflow_definition, register_workflow_definition
from .tool import ApplyFormChangesInput, create_apply_form_changes_bundle

__all__ = [
    "ApplyFormChangesInput",
    "FastFormChange",
    "FormFieldDefinition",
    "FormWorkflowDefinition",
    "create_apply_form_changes_bundle",
    "get_workflow_definition",
    "match_explicit_form_change",
    "register_workflow_definition",
]
