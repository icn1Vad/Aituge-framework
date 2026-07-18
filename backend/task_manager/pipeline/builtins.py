from __future__ import annotations

from .stage_registry import StageExecutionContext, StageServiceResult, register_stage_handler
from .store import list_stage_runs


async def merge_pipeline_artifacts(context: StageExecutionContext) -> StageServiceResult:
    latest_by_stage = {}
    for item in await list_stage_runs(context.run.id):
        previous = latest_by_stage.get(item.stage_id)
        if previous is None or item.attempt >= previous.attempt:
            latest_by_stage[item.stage_id] = item
    output = {
        "artifacts": {
            stage_id: artifact.content_json
            for stage_id, artifact in context.artifacts.items()
            if artifact.content_json is not None
        },
        "stages": {
            stage_id: {
                "status": item.status,
                "error_message": item.error_message,
            }
            for stage_id, item in latest_by_stage.items()
            if stage_id != context.stage.stage_id
        },
    }
    return StageServiceResult(output=output, summary="Merged pipeline stage artifacts.")


register_stage_handler("merge_pipeline_artifacts", merge_pipeline_artifacts)
