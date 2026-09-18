"""One command-producing tool for both single and batch field changes."""

from __future__ import annotations

import json
from typing import Any, Literal

from llama_index.core.tools.function_tool import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tool import ToolBundle
from .registry import get_workflow_definition


class FormFieldChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(min_length=1, max_length=128)
    value: Any
    source: Literal["ai"] = "ai"

    @field_validator("field_key")
    @classmethod
    def normalize_field_key(cls, value: str) -> str:
        return value.strip()


class ApplyFormChangesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    draft_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    changes: list[FormFieldChange] = Field(min_length=1, max_length=100)


class StartWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_type: str = Field(min_length=1, max_length=64)
    changes: list[FormFieldChange] = Field(default_factory=list, max_length=100)

    @field_validator("workflow_type")
    @classmethod
    def require_registered_workflow(cls, value: str) -> str:
        normalized = value.strip().upper()
        if get_workflow_definition(normalized) is None:
            raise ValueError("Workflow is not registered")
        return normalized


def create_apply_form_changes_bundle(_config) -> ToolBundle:
    async def apply_form_changes(
        request_id: str,
        draft_id: str,
        expected_version: int,
        changes: list[dict[str, Any]],
    ) -> str:
        command = ApplyFormChangesInput.model_validate(
            {
                "request_id": request_id,
                "draft_id": draft_id,
                "expected_version": expected_version,
                "changes": changes,
            }
        )
        return json.dumps(
            {
                "accepted": True,
                "requestId": command.request_id,
                "draftId": command.draft_id,
                "expectedVersion": command.expected_version,
                "changeCount": len(command.changes),
            },
            ensure_ascii=False,
        )

    tool = FunctionTool.from_defaults(
        async_fn=apply_form_changes,
        name="apply_form_changes",
        description=(
            "Apply one or more validated changes to the currently bound business form. "
            "Use only field keys listed in the task context. This tool emits a command; "
            "the Java business boundary persists it."
        ),
        fn_schema=ApplyFormChangesInput,
        return_direct=False,
    )
    return ToolBundle.from_tools([tool])


def create_start_workflow_bundle(_config) -> ToolBundle:
    async def start_workflow(
        workflow_type: str,
        changes: list[dict[str, Any]] | None = None,
    ) -> str:
        command = StartWorkflowInput.model_validate(
            {
                "workflow_type": workflow_type,
                "changes": changes or [],
            }
        )
        return json.dumps(
            {
                "accepted": True,
                "workflowType": command.workflow_type,
                "changeCount": len(command.changes),
            },
            ensure_ascii=False,
        )

    tool = FunctionTool.from_defaults(
        async_fn=start_workflow,
        name="start_workflow",
        description=(
            "Start a supported business workflow when no form draft is currently bound. "
            "Choose only a registered workflow listed in this scene's task context. "
            "Include any fields already stated by the user in changes. "
            "The Java business boundary creates and persists the draft."
        ),
        fn_schema=StartWorkflowInput,
        return_direct=False,
    )
    return ToolBundle.from_tools([tool])
