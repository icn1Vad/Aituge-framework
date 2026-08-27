"""Reusable structured-form capability for Single Agent workflows."""

from .ai_interpreter import (
    AiFormCommandInterpreter,
    FormCommandDecision,
    InterpretedFormChange,
    can_interpret_form_command,
    normalize_form_command,
    normalize_start_workflow_command,
)
from .models import FormFieldDefinition, FormWorkflowDefinition
from .registry import (
    get_workflow_definition,
    get_workflow_definitions,
    register_workflow_definition,
)
from .tool import (
    ApplyFormChangesInput,
    StartWorkflowInput,
    create_apply_form_changes_bundle,
    create_start_workflow_bundle,
)

__all__ = [
    "AiFormCommandInterpreter",
    "ApplyFormChangesInput",
    "FormCommandDecision",
    "FormFieldDefinition",
    "FormWorkflowDefinition",
    "InterpretedFormChange",
    "StartWorkflowInput",
    "can_interpret_form_command",
    "create_apply_form_changes_bundle",
    "get_workflow_definition",
    "get_workflow_definitions",
    "create_start_workflow_bundle",
    "normalize_form_command",
    "normalize_start_workflow_command",
    "register_workflow_definition",
]
