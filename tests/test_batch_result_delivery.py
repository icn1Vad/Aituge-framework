from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import task_manager.handlers.batch_item_scheduler as batch_scheduler_mod
import task_manager.pipeline.executor as pipeline_executor_mod
from task_manager.handlers.base import TaskExecutionContext, TaskHandlerEvent
from task_manager.handlers.batch_item_scheduler import BatchItemSchedulerHandler
from task_manager.models import TaskEntity
from task_manager.pipeline.executor import PipelineExecutor
from task_manager.pipeline.models import BatchStageConfig, StageDefinition


def test_batch_scheduler_result_delivery_can_be_delegated(monkeypatch) -> None:
    delivered: list[dict] = []

    @asynccontextmanager
    async def fake_session():
        yield SimpleNamespace()

    async def no_op(*_args, **_kwargs):
        return None

    async def profile(*_args, **_kwargs):
        return SimpleNamespace(enabled=True, agent_type="single", agent_id="audit-agent")

    async def no_pending_items(*_args, **_kwargs):
        return []

    async def no_results(*_args, **_kwargs):
        return []

    async def capture_delivery(_task, _definition, output):
        delivered.append(output)

    monkeypatch.setattr(batch_scheduler_mod, "create_db_session", fake_session)
    monkeypatch.setattr(batch_scheduler_mod, "ensure_default_skill_packages", no_op)
    monkeypatch.setattr(batch_scheduler_mod, "ensure_default_agent_profiles", no_op)
    monkeypatch.setattr(batch_scheduler_mod, "get_agent_profile", profile)
    monkeypatch.setattr(batch_scheduler_mod.item_store, "list_pending_items", no_pending_items)
    monkeypatch.setattr(batch_scheduler_mod.item_store, "load_item_results", no_results)
    monkeypatch.setattr(batch_scheduler_mod, "_deliver_result", capture_delivery)

    context = SimpleNamespace(
        task=SimpleNamespace(
            id="task-1",
            agent_id="audit-agent",
            input_payload_json={},
            metadata_json={},
        ),
        task_type=SimpleNamespace(default_agent_id="audit-agent"),
        runtime_context=SimpleNamespace(),
    )
    handler = BatchItemSchedulerHandler(SimpleNamespace())

    async def collect(deliver_result: bool):
        return [
            event
            async for event in handler.stream(
                context=context,
                item_type="pipeline:semantic_audit",
                deliver_result=deliver_result,
            )
        ]

    delegated_events = asyncio.run(collect(False))
    assert delivered == []
    assert json.loads(delegated_events[-1].final_content)["summary"] == {
        "total": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
    }

    direct_events = asyncio.run(collect(True))
    assert len(delivered) == 1
    assert json.loads(direct_events[-1].final_content) == delivered[0]


def test_pipeline_batch_stage_delegates_delivery_to_parent(monkeypatch) -> None:
    captured: dict = {}

    class StubBatchHandler:
        def __init__(self, _options) -> None:
            pass

        async def stream(self, *, context, item_type=None, deliver_result=True):
            captured.update(
                context=context,
                item_type=item_type,
                deliver_result=deliver_result,
            )
            final = {
                "summary": {"total": 0, "succeeded": 0, "failed": 0, "skipped": 0},
                "items": [],
            }
            yield TaskHandlerEvent(
                event_type="batch_succeeded",
                stage="batch_item_scheduler",
                message="done",
                final_content=json.dumps(final),
            )

    async def ensure_stage_items(*_args, **_kwargs):
        return []

    monkeypatch.setattr(pipeline_executor_mod, "BatchItemSchedulerHandler", StubBatchHandler)
    monkeypatch.setattr(pipeline_executor_mod.item_store, "ensure_stage_items", ensure_stage_items)

    task = TaskEntity(
        id="task-1",
        task_type="proof.audit.run",
        input_payload_json={"semantic_items": []},
        agent_id="parent-agent",
        current_run_id="run-1",
    )
    context = TaskExecutionContext(
        task=task,
        task_type=SimpleNamespace(),
        memory_view=SimpleNamespace(),
        runtime_context=SimpleNamespace(),
    )
    stage = StageDefinition(
        stage_id="semantic_audit",
        name="Semantic audit",
        stage_type="batch",
        batch_config=BatchStageConfig(
            agent_id="audit-agent",
            item_source="semantic_items",
        ),
    )

    async def collect():
        return [
            event
            async for event in PipelineExecutor(SimpleNamespace())._stream_batch_stage(
                context=context,
                stage=stage,
                stage_run_id="stage-run-1",
                stage_input={},
            )
        ]

    events = asyncio.run(collect())
    assert captured["deliver_result"] is False
    assert captured["item_type"] == "pipeline:semantic_audit"
    assert captured["context"].task.id == task.id
    assert events[-1].structured_output == {
        "summary": {"total": 0, "succeeded": 0, "failed": 0, "skipped": 0},
        "items": [],
    }
