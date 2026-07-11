from __future__ import annotations

import os
from typing import Any

import httpx

from .errors import StageExecutionError
from .models import AgentStageConfig, PipelineDefinition, RetryPolicy, StageDefinition
from .registry import register_pipeline
from .stage_registry import StageExecutionContext, StageServiceResult, register_stage_handler


def _gateway_url(path: str) -> str:
    base = os.environ.get("MEDIA_MILITARY_GATEWAY_BASE_URL", "http://127.0.0.1:8010").rstrip("/")
    return f"{base}{path}"


async def _post_gateway(path: str, payload: dict[str, Any], context: StageExecutionContext) -> dict[str, Any]:
    headers = {
        "x-user-id": context.task.user_id,
        "x-tenant-id": context.task.tenant_id,
    }
    token = os.environ.get("MEDIA_MILITARY_GATEWAY_TOKEN", "").strip()
    if token:
        headers["x-media-gateway-token"] = token
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(_gateway_url(path), json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise StageExecutionError(
            f"Media gateway request failed: {exc}",
            code="temporary_network_error",
            retryable=True,
        ) from exc
    if not isinstance(data, dict):
        raise StageExecutionError("Media gateway returned a non-object payload.", code="invalid_gateway_output")
    return data


async def load_media_script_context(context: StageExecutionContext) -> StageServiceResult:
    output = await _post_gateway("/internal/aituge/script/context", context.stage_input, context)
    return StageServiceResult(output=output, summary="Loaded topic, material, persona, and master-library context.")


async def run_media_script_checks(context: StageExecutionContext) -> StageServiceResult:
    output = await _post_gateway("/internal/aituge/script/checks", context.stage_input, context)
    return StageServiceResult(output=output, summary="Completed deterministic script and storyboard checks.")


async def prepare_research_context(context: StageExecutionContext) -> StageServiceResult:
    source = context.stage_input
    output = {
        "topic_card": source.get("topic_card") or {},
        "source_brief": source.get("source_brief") or {},
        "material_full": source.get("material_full") or {},
        "material_comments": source.get("material_comments") or {},
        "current_persona": source.get("current_persona") or {},
        "persona_context": source.get("persona_context") or {},
        "user_constraints": source.get("user_constraints") or {},
        "warnings": source.get("warnings") or [],
    }
    return StageServiceResult(output=output, summary="Prepared the bounded research input.")


async def finalize_media_script(context: StageExecutionContext) -> StageServiceResult:
    artifacts = dict(context.stage_input.get("artifacts") or {})
    writer = dict(artifacts.get("media_script_writer_draft") or {})
    storyboard = dict(artifacts.get("media_storyboard_draft") or {})
    checks = dict(artifacts.get("media_script_check_result") or {})
    review = dict(artifacts.get("media_script_review_result") or {})
    research = dict(artifacts.get("media_script_research_bundle") or {})
    context_bundle = dict(artifacts.get("media_script_context_bundle") or {})

    final_script = dict(writer.get("final_script") or {})
    final_script["storyboard"] = storyboard.get("storyboard") or []
    final_script["storyboard_plan"] = storyboard.get("storyboard_plan") or {}
    if not final_script.get("visual_direction"):
        final_script["visual_direction"] = storyboard.get("visual_direction") or []

    task_input = dict(context.task.input_payload_json or {})
    human_review = dict(context.run.metadata_json or {}).get("human_review") or {}
    approved = human_review.get("action") == "approve"
    needs_review = bool(
        task_input.get("require_human_review")
        or checks.get("high_risk")
        or not checks.get("passed", False)
        or review.get("recommendation") != "pass"
    )
    workflow_status = "pass" if approved or not needs_review else "needs_human_review"
    hermes_result = {
        **dict(writer.get("hermes_agent_result") or {}),
        "status": workflow_status,
        "review_summary": review.get("summary") or "",
        "deterministic_score": checks.get("score"),
        "risks": list(dict.fromkeys([
            *[str(item) for item in (writer.get("hermes_agent_result") or {}).get("risks") or []],
            *[str(item.get("message") or item.get("code") or item) for item in checks.get("findings") or []],
            *[str(item.get("message") or item.get("code") or item) for item in review.get("compliance_findings") or []],
        ])),
        "human_override": approved,
    }
    workflow_state = {
        "workflow_status": workflow_status,
        "current_persona": context_bundle.get("current_persona") or {},
        "persona_context": context_bundle.get("persona_context") or {},
        "source_brief": context_bundle.get("source_brief") or {},
        "research_bundle": research,
        "script_stack_recommendation": context_bundle.get("script_stack_recommendation") or {},
        "hermes_agent_result": hermes_result,
        "storyboard_plan": storyboard.get("storyboard_plan") or {},
        "deterministic_check_result": checks,
        "review_result": review,
        "final_script": final_script,
        "readable_script": writer.get("readable_script") or final_script.get("voiceover") or "",
        "workflow_trace": [
            {"node": stage_id, "status": "done"}
            for stage_id in ("context", "research_context", "research", "writer", "storyboard", "deterministic_checks", "review", "finalize")
        ],
    }
    output = {
        "final_script": final_script,
        "readable_script": workflow_state["readable_script"],
        "hermes_agent_result": hermes_result,
        "workflow_state": workflow_state,
        "generation_meta": {
            "provider": "aituge_task_manager",
            "workflow": "media-script-pipeline-v1",
            "workflow_status": workflow_status,
            "pipeline_version": "1.0",
            "human_override": approved,
        },
    }
    if needs_review and not approved:
        return StageServiceResult(
            output=output,
            summary="Script requires human review.",
            pause=True,
            pause_reason=review.get("summary") or "Script checks require human review.",
            pause_payload={
                "reason_codes": [
                    "deterministic_high_risk" if checks.get("high_risk") else "review_not_passed"
                ],
                "allowed_actions": ["approve", "reject", "revise_input", "rerun_stage"],
            },
        )
    return StageServiceResult(output=output, summary="Finalized the media script package.")


register_stage_handler("media_script_context_gateway", load_media_script_context)
register_stage_handler("media_script_research_context", prepare_research_context)
register_stage_handler("media_script_checks_gateway", run_media_script_checks)
register_stage_handler("media_script_finalize", finalize_media_script)


MEDIA_SCRIPT_PIPELINE = PipelineDefinition(
    pipeline_id="media-script-pipeline-v1",
    version="1.0",
    task_type="media.script.pipeline.generate",
    description="Multi-agent script generation compatible with the media_military business workflow.",
    final_artifact_type="media_script_output",
    timeout_seconds=900,
    resumable=True,
    max_parallelism=1,
    stages=(
        StageDefinition(
            stage_id="context",
            name="Load media business context",
            stage_type="gateway",
            input_schema="media_script_generate_input",
            output_schema="media_script_context_bundle",
            input_adapter="task_input",
            artifact_type="media_script_context_bundle",
            timeout_seconds=90,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=1, retry_on=("temporary_network_error",)),
            service_handler="media_script_context_gateway",
        ),
        StageDefinition(
            stage_id="research_context",
            name="Prepare bounded research context",
            stage_type="deterministic",
            depends_on=("context",),
            input_schema="media_script_context_bundle",
            output_schema="media_script_research_context",
            input_adapter="single_dependency",
            artifact_type="media_script_research_context",
            service_handler="media_script_research_context",
        ),
        StageDefinition(
            stage_id="research",
            name="Research evidence and angle",
            stage_type="agent",
            depends_on=("research_context",),
            output_schema="media_script_research_bundle",
            input_adapter="single_dependency",
            artifact_type="media_script_research_bundle",
            timeout_seconds=240,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=1, retry_on=("invalid_output", "timeout")),
            agent_config=AgentStageConfig(
                agent_id="media-research-agent",
                skill_package="media-script-research-package",
                tools=("web_search",),
                session_policy="isolated_stage",
                output_policy="repair_once",
            ),
        ),
        StageDefinition(
            stage_id="writer",
            name="Write structured script draft",
            stage_type="agent",
            depends_on=("context", "research"),
            output_schema="media_script_writer_draft",
            input_adapter="pipeline_context",
            artifact_type="media_script_writer_draft",
            timeout_seconds=240,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=1, retry_on=("invalid_output", "timeout")),
            agent_config=AgentStageConfig(
                agent_id="media-writer-agent",
                skill_package="media-script-writer-package",
                tools=(),
                session_policy="isolated_stage",
                output_policy="repair_once",
            ),
        ),
        StageDefinition(
            stage_id="storyboard",
            name="Generate executable storyboard",
            stage_type="agent",
            depends_on=("context", "writer"),
            output_schema="media_storyboard_draft",
            input_adapter="pipeline_context",
            artifact_type="media_storyboard_draft",
            timeout_seconds=180,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=1, retry_on=("invalid_output", "timeout")),
            agent_config=AgentStageConfig(
                agent_id="media-storyboard-agent",
                skill_package="media-storyboard-package",
                tools=(),
                session_policy="isolated_stage",
                output_policy="repair_once",
            ),
        ),
        StageDefinition(
            stage_id="deterministic_checks",
            name="Run deterministic script checks",
            stage_type="gateway",
            depends_on=("context", "writer", "storyboard"),
            output_schema="media_script_check_result",
            input_adapter="pipeline_context",
            artifact_type="media_script_check_result",
            timeout_seconds=90,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=1, retry_on=("temporary_network_error",)),
            service_handler="media_script_checks_gateway",
        ),
        StageDefinition(
            stage_id="review",
            name="Review compliance and quality",
            stage_type="agent",
            depends_on=("context", "research", "writer", "storyboard", "deterministic_checks"),
            output_schema="media_script_review_result",
            input_adapter="pipeline_context",
            artifact_type="media_script_review_result",
            timeout_seconds=180,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=1, retry_on=("invalid_output", "timeout")),
            agent_config=AgentStageConfig(
                agent_id="media-review-agent",
                skill_package="media-script-review-package",
                tools=(),
                session_policy="isolated_stage",
                output_policy="repair_once",
            ),
        ),
        StageDefinition(
            stage_id="finalize",
            name="Finalize compatible script package",
            stage_type="finalizer",
            depends_on=("context", "research", "writer", "storyboard", "deterministic_checks", "review"),
            output_schema="media_script_output",
            input_adapter="pipeline_context",
            artifact_type="media_script_output",
            service_handler="media_script_finalize",
        ),
    ),
)

register_pipeline(MEDIA_SCRIPT_PIPELINE)
