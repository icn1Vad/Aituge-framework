"""One command-producing tool for both single and batch field changes."""

from __future__ import annotations

import json
from typing import Any, Literal

from llama_index.core.tools.function_tool import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tool import ToolBundle


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

    workflow_type: Literal["TRAVEL_APPLICATION", "TRAVEL_REIMBURSEMENT"]
    changes: list[FormFieldChange] = Field(default_factory=list, max_length=100)


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
            "TRAVEL_APPLICATION starts a travel request and TRAVEL_REIMBURSEMENT starts "
            "reimbursement selection. Include any fields already stated by the user in changes. "
            "The Java business boundary creates and persists the draft."
        ),
        fn_schema=StartWorkflowInput,
        return_direct=False,
    )
    return ToolBundle.from_tools([tool])
