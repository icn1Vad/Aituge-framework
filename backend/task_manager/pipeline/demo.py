from __future__ import annotations

from .models import AgentStageConfig, PipelineDefinition, RetryPolicy, StageDefinition
from .registry import register_pipeline
from .stage_registry import StageExecutionContext, StageServiceResult, register_stage_handler


async def normalize_demo_analysis(context: StageExecutionContext) -> StageServiceResult:
    source = dict(context.stage_input)
    output = {
        "summary": str(source.get("summary") or "").strip(),
        "steps": [str(item).strip() for item in source.get("steps") or [] if str(item).strip()],
        "risks": list(dict.fromkeys(str(item).strip() for item in source.get("risks") or [] if str(item).strip())),
        "normalized": True,
    }
    return StageServiceResult(output=output, summary="Normalized the analysis artifact.")


async def finalize_demo(context: StageExecutionContext) -> StageServiceResult:
    normalized = dict(context.stage_input)
    review = dict(context.run.metadata_json or {}).get("human_review") or {}
    needs_review = bool((context.task.input_payload_json or {}).get("require_human_review"))
    approved = review.get("action") == "approve"
    output = {
        "status": "success" if approved or not needs_review else "needs_human_review",
        "summary": normalized.get("summary") or "Pipeline completed.",
        "steps": normalized.get("steps") or [],
        "risks": normalized.get("risks") or [],
        "artifact_ids": [artifact.id for artifact in context.artifacts.values()],
    }
    if needs_review and not approved:
        return StageServiceResult(
            output=output,
            summary="Pipeline paused for human review.",
            pause=True,
            pause_reason="The demo task requested a human approval gate.",
            pause_payload={
                "reason_codes": ["demo_human_review_requested"],
                "allowed_actions": ["approve", "reject", "revise_input", "rerun_stage"],
            },
        )
    return StageServiceResult(output=output, summary="Finalized the pipeline result.")


register_stage_handler("pipeline_demo_normalize", normalize_demo_analysis)
register_stage_handler("pipeline_demo_finalize", finalize_demo)

PIPELINE_DEMO = PipelineDefinition(
    pipeline_id="pipeline-demo-v1",
    version="1.0",
    task_type="pipeline.demo",
    description="Business-neutral pipeline used to validate reusable TaskManager runtime behavior.",
    final_artifact_type="pipeline_demo_result",
    timeout_seconds=300,
    resumable=True,
    max_parallelism=1,
    stages=(
        StageDefinition(
            stage_id="analyze",
            name="Analyze request",
            stage_type="agent",
            input_schema="pipeline_demo_input",
            output_schema="pipeline_demo_analysis",
            input_adapter="task_input",
            artifact_type="pipeline_demo_analysis",
            timeout_seconds=120,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=0.2, retry_on=("invalid_output", "timeout")),
            agent_config=AgentStageConfig(
                agent_id="default-single-agent",
                skill_package="pipeline-demo-package",
                tools=("code_interpreter", "enabled_db_tools", "rag_retrieval"),
                datasets=("local_rag",),
                session_policy="isolated_stage",
                output_policy="repair_once",
            ),
        ),
        StageDefinition(
            stage_id="normalize",
            name="Normalize result",
            stage_type="deterministic",
            depends_on=("analyze",),
            input_schema="pipeline_demo_analysis",
            output_schema="pipeline_demo_normalized",
            input_adapter="single_dependency",
            artifact_type="pipeline_demo_normalized",
            service_handler="pipeline_demo_normalize",
        ),
        StageDefinition(
            stage_id="finalize",
            name="Finalize result",
            stage_type="finalizer",
            depends_on=("normalize",),
            input_schema="pipeline_demo_normalized",
            output_schema="pipeline_demo_result",
            input_adapter="single_dependency",
            artifact_type="pipeline_demo_result",
            service_handler="pipeline_demo_finalize",
        ),
    ),
)

register_pipeline(PIPELINE_DEMO)
