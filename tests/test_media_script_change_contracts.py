import asyncio

import pytest

from task_manager.payload_schemas import validate_input_payload, validate_output_payload
from task_manager.pipeline.media_script_change_proposal import await_change_proposal_confirmation
from task_manager.registry import get_task_definition


def test_change_proposal_task_contract_is_explicit_and_tool_free():
    definition = get_task_definition("media.script.change.propose")

    assert definition.handler == "pipeline"
    assert definition.pipeline_id == "media-script-change-proposal-v1"
    assert definition.default_agent_id == "media-writer-agent"
    assert definition.default_skill_package == "media-script-change-proposal-package"
    assert definition.default_primary_skill == "media-script-change-proposal"
    assert definition.default_tools == []
    assert definition.default_datasets == []
    assert definition.input_schema_name == "media_script_change_proposal_input"
    assert definition.output_schema_name == "media_script_change_proposal_output"


def test_change_proposal_input_requires_user_request_and_base_script():
    payload = validate_input_payload(
        "media_script_change_proposal_input",
        {
            "message": "Make the opening more direct.",
            "base_script_id": "script-1",
            "current_script": {"hook": "Old hook", "voiceover": "Old script"},
        },
    )

    assert payload["message"] == "Make the opening more direct."
    assert payload["base_script_id"] == "script-1"
    assert payload["recent_messages"] == []

    with pytest.raises(ValueError, match="media_script_change_proposal_input"):
        validate_input_payload(
            "media_script_change_proposal_input",
            {"message": "Make the opening more direct."},
        )


def test_change_proposal_output_requires_actionable_changes_or_clarification():
    valid, error = validate_output_payload(
        "media_script_change_proposal_output",
        {
            "status": "pending_confirmation",
            "summary": "Rewrite only the opening hook.",
            "target_fields": ["hook_3s"],
            "changes": [{"field": "hook_3s", "instruction": "Lead with the policy conclusion."}],
            "preserve_fields": ["persona_name", "evidence"],
            "storyboard_regeneration_required": True,
        },
    )
    assert valid is True
    assert error is None

    valid, error = validate_output_payload(
        "media_script_change_proposal_output",
        {
            "status": "pending_confirmation",
            "summary": "No concrete changes.",
        },
    )
    assert valid is False
    assert error["schema_name"] == "media_script_change_proposal_output"

    valid, _ = validate_output_payload(
        "media_script_change_proposal_output",
        {
            "status": "needs_clarification",
            "summary": "The requested scope is ambiguous.",
            "clarification_question": "Should the body remain unchanged?",
        },
    )
    assert valid is True


def test_revision_mode_requires_immutable_source_context():
    normal = validate_input_payload(
        "media_script_generate_input",
        {"topic": "Veteran employment", "duration_seconds": 60},
    )
    assert normal["revision_mode"] is False

    with pytest.raises(ValueError, match="Revision mode requires"):
        validate_input_payload(
            "media_script_generate_input",
            {"topic": "Veteran employment", "revision_mode": True},
        )

    revision = validate_input_payload(
        "media_script_generate_input",
        {
            "topic": "Veteran employment",
            "revision_mode": True,
            "base_script_id": "script-1",
            "proposal_artifact_id": "artifact-proposal-1",
            "previous_script": {"hook_3s": "Old hook", "voiceover": "Old script"},
            "change_proposal": {
                "target_fields": ["hook_3s"],
                "changes": [{"field": "hook_3s", "instruction": "Make it direct."}],
            },
            "preserve_fields": ["persona_name", "evidence"],
        },
    )
    assert revision["revision_mode"] is True
    assert revision["base_script_id"] == "script-1"
    assert revision["proposal_artifact_id"] == "artifact-proposal-1"


def test_clarification_proposal_cannot_be_approved():
    class Context:
        stage_input = {
            "status": "needs_clarification",
            "summary": "The requested scope is ambiguous.",
            "clarification_question": "Should the body remain unchanged?",
        }

    result = asyncio.run(await_change_proposal_confirmation(Context()))

    assert result.pause_payload["allowed_actions"] == ["revise_input", "reject"]
