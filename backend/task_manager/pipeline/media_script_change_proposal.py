from __future__ import annotations

from .models import AgentStageConfig, PipelineDefinition, RetryPolicy, StageDefinition
from .registry import register_pipeline
from .stage_registry import StageExecutionContext, StageServiceResult, register_stage_handler


async def prepare_change_proposal_context(context: StageExecutionContext) -> StageServiceResult:
    return StageServiceResult(
        output=dict(context.stage_input),
        summary="Prepared the bounded current-script context for the proposal Agent.",
    )


async def await_change_proposal_confirmation(context: StageExecutionContext) -> StageServiceResult:
    proposal = dict(context.stage_input)
    allowed_actions = (
        ["revise_input", "reject"]
        if proposal.get("status") == "needs_clarification"
        else ["approve", "reject", "revise_input"]
    )
    return StageServiceResult(
        output=proposal,
        summary=proposal.get("summary") or "Script change proposal is ready for confirmation.",
        pause_payload={
            "reason_codes": ["script_change_proposal_confirmation_required"],
            "allowed_actions": allowed_actions,
            "proposal_status": proposal.get("status"),
            "target_fields": proposal.get("target_fields") or [],
        },
    )


register_stage_handler("media_script_change_context", prepare_change_proposal_context)
register_stage_handler("media_script_change_await_confirmation", await_change_proposal_confirmation)


MEDIA_SCRIPT_CHANGE_PROPOSAL_PIPELINE = PipelineDefinition(
    pipeline_id="media-script-change-proposal-v1",
    version="1.0",
    task_type="media.script.change.propose",
    description="Create an immutable script change proposal and pause for explicit human confirmation.",
    final_artifact_type="media_script_change_proposal",
    timeout_seconds=300,
    resumable=True,
    max_parallelism=1,
    stages=(
        StageDefinition(
            stage_id="context",
            name="Prepare current script context",
            stage_type="deterministic",
            input_schema="media_script_change_proposal_input",
            output_schema="media_script_change_proposal_input",
            input_adapter="task_input",
            artifact_type="media_script_change_context",
            service_handler="media_script_change_context",
        ),
        StageDefinition(
            stage_id="propose",
            name="Generate bounded change proposal",
            stage_type="agent",
            depends_on=("context",),
            output_schema="media_script_change_proposal_output",
            input_adapter="single_dependency",
            artifact_type="media_script_change_proposal_draft",
            timeout_seconds=180,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=1, retry_on=("invalid_output", "timeout")),
            agent_config=AgentStageConfig(
                agent_id="media-writer-agent",
                skill_package="media-script-change-proposal-package",
                tools=(),
                datasets=(),
                session_policy="isolated_stage",
                output_policy="repair_once",
            ),
        ),
        StageDefinition(
            stage_id="await_confirmation",
            name="Await explicit human confirmation",
            stage_type="finalizer",
            depends_on=("propose",),
            input_schema="media_script_change_proposal_output",
            output_schema="media_script_change_proposal_output",
            input_adapter="single_dependency",
            artifact_type="media_script_change_proposal",
            service_handler="media_script_change_await_confirmation",
            requires_human_review=True,
        ),
    ),
)

register_pipeline(MEDIA_SCRIPT_CHANGE_PROPOSAL_PIPELINE)
