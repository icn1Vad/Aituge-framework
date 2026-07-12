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


def _finding_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("message") or item.get("code") or item)
    return str(item)


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


async def finalize_media_script(context: StageExecutionContext) -> StageServiceResult:
    artifacts = dict(context.stage_input.get("artifacts") or {})
    writer = dict(artifacts.get("media_script_writer_draft") or {})
    storyboard = dict(artifacts.get("media_storyboard_draft") or {})
    checks = dict(artifacts.get("media_script_check_result") or {})
    context_bundle = dict(artifacts.get("media_script_context_bundle") or {})

    final_script = dict(writer.get("final_script") or {})
    final_script["storyboard"] = storyboard.get("storyboard") or []
    final_script["storyboard_plan"] = storyboard.get("storyboard_plan") or {}
    if not final_script.get("visual_direction"):
        final_script["visual_direction"] = storyboard.get("visual_direction") or []

    workflow_status = "pass"
    check_findings = [_finding_text(item) for item in checks.get("findings") or []]
    check_warnings = [str(item) for item in checks.get("warnings") or []]
    hermes_result = {
        **dict(writer.get("hermes_agent_result") or {}),
        "status": workflow_status,
        "review_summary": "Automated Agent review was skipped by the Lite Pipeline.",
        "deterministic_score": checks.get("score"),
        "risks": list(dict.fromkeys([
            *[str(item) for item in (writer.get("hermes_agent_result") or {}).get("risks") or []],
            *check_findings,
            *check_warnings,
        ])),
        "human_override": False,
        "reviewed": False,
    }
    workflow_state = {
        "workflow_status": workflow_status,
        "current_persona": context_bundle.get("current_persona") or {},
        "persona_context": context_bundle.get("persona_context") or {},
        "source_brief": context_bundle.get("source_brief") or {},
        "script_stack_recommendation": context_bundle.get("script_stack_recommendation") or {},
        "hermes_agent_result": hermes_result,
        "storyboard_plan": storyboard.get("storyboard_plan") or {},
        "deterministic_check_result": checks,
        "review_result": {
            "reviewed": False,
            "recommendation": "not_reviewed",
            "summary": "Research and Review Agent stages were skipped.",
        },
        "final_script": final_script,
        "readable_script": writer.get("readable_script") or final_script.get("voiceover") or "",
        "workflow_trace": [
            {"node": stage_id, "status": "done"}
            for stage_id in ("context", "writer", "storyboard", "deterministic_checks", "finalize")
        ],
    }
    output = {
        "final_script": final_script,
        "readable_script": workflow_state["readable_script"],
        "hermes_agent_result": hermes_result,
        "workflow_state": workflow_state,
        "generation_meta": {
            "provider": "aituge_task_manager",
            "workflow": "media-script-lite-pipeline-v1",
            "workflow_status": workflow_status,
            "pipeline_version": "1.0",
            "human_override": False,
            "reviewed": False,
            "web_research_used": False,
            "deterministic_checks_passed": bool(checks.get("passed", False)),
        },
    }
    return StageServiceResult(output=output, summary="Finalized the unreviewed media script package.")


register_stage_handler("media_script_context_gateway", load_media_script_context)
register_stage_handler("media_script_checks_gateway", run_media_script_checks)
register_stage_handler("media_script_finalize", finalize_media_script)


MEDIA_SCRIPT_PIPELINE = PipelineDefinition(
    pipeline_id="media-script-lite-pipeline-v1",
    version="1.0",
    task_type="media.script.pipeline.generate",
    description="Two-Agent script and storyboard generation using only provided media context.",
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
            stage_id="writer",
            name="Write structured script draft",
            stage_type="agent",
            depends_on=("context",),
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
            stage_id="finalize",
            name="Finalize compatible script package",
            stage_type="finalizer",
            depends_on=("context", "writer", "storyboard", "deterministic_checks"),
            output_schema="media_script_output",
            input_adapter="pipeline_context",
            artifact_type="media_script_output",
            service_handler="media_script_finalize",
        ),
    ),
)

register_pipeline(MEDIA_SCRIPT_PIPELINE)
