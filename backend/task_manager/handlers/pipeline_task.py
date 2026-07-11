from __future__ import annotations

from collections.abc import AsyncIterator

from scheduling.scheduler import SchedulingRuntimeOptions

from task_manager.handlers.base import TaskHandlerEvent
from task_manager.models import TaskEntity
from task_manager.pipeline.errors import PipelineCancelled
from task_manager.pipeline.executor import PipelineExecutor
from task_manager.pipeline.registry import get_pipeline_definition
from task_manager.pipeline.store import get_run
from task_manager.registry import TaskDefinition


class PipelineTaskHandler:
    def __init__(self, options: SchedulingRuntimeOptions) -> None:
        self.options = options

    async def stream(
        self,
        *,
        task: TaskEntity,
        definition: TaskDefinition,
    ) -> AsyncIterator[TaskHandlerEvent]:
        if not definition.pipeline_id:
            raise ValueError(f"Pipeline task '{task.task_type}' has no pipeline_id.")
        if not task.current_run_id:
            raise ValueError(f"Pipeline task '{task.id}' has no active run.")
        run = await get_run(task.current_run_id)
        if run is None:
            raise ValueError(f"Run '{task.current_run_id}' not found.")
        pipeline = get_pipeline_definition(definition.pipeline_id)
        if pipeline.task_type != task.task_type:
            raise ValueError(
                f"Pipeline '{pipeline.pipeline_id}' belongs to '{pipeline.task_type}', not '{task.task_type}'."
            )
        try:
            async for event in PipelineExecutor(self.options).stream(task=task, run=run, definition=pipeline):
                yield event
        except PipelineCancelled as exc:
            yield TaskHandlerEvent(
                event_type="task_cancelled",
                stage=run.current_stage_id or "pipeline",
                message=str(exc),
                payload={"run_id": run.id},
                terminal_status="cancelled",
                outcome="cancelled",
                stream_semantics="status",
                source={"type": "pipeline", "id": pipeline.pipeline_id},
            )
