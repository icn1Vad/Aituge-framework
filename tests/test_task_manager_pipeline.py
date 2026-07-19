import asyncio
import json
import time
from types import MethodType, SimpleNamespace

import httpx

from backend.local_code_chat_app import create_app
from common.encrypt_utils import encrypt_key
from common.llm.models import TextChunk
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db, reset_engine_for_test
from db.models.llm import LlmModelEntity
import service.agent.single_agent_runner as runner_mod
import task_manager.pipeline.executor as pipeline_executor_mod
from task_manager.handlers.base import TaskHandlerEvent
from task_manager.pipeline.executor import PipelineExecutor
from task_manager.pipeline.models import (
    AgentStageConfig,
    PipelineDefinition,
    RetryPolicy,
    StageDefinition,
    validate_pipeline_definition,
)
from task_manager.pipeline.stage_registry import StageServiceResult
from task_manager.result_sink import RequiredResultSinkError, ResultSinkRejectedError
from task_manager.runtime.broker import reset_event_broker_for_test
from task_manager.runtime.execution import executor_lock


class RetryThenPipelineAgent:
    calls = 0

    def __init__(self, llm, system_prompt, tools):
        assert "Pipeline Demo" in system_prompt

    async def run_async(self, state):
        type(self).calls += 1

        async def generate():
            if type(self).calls == 1:
                yield TextChunk(delta="not-json")
                return
            content = json.dumps(
                {
                    "summary": "A reusable pipeline separates orchestration from stage execution.",
                    "steps": ["Validate input", "Execute stages", "Persist artifacts"],
                    "risks": ["Invalid stage output"],
                },
                ensure_ascii=False,
            )
            midpoint = len(content) // 2
            yield TextChunk(delta=content[:midpoint])
            yield TextChunk(delta=content[midpoint:])

        return generate()


class SlowPipelineAgent:
    def __init__(self, llm, system_prompt, tools):
        pass

    async def run_async(self, state):
        async def generate():
            for _ in range(20):
                await asyncio.sleep(0.05)
                yield TextChunk(delta="x" * 30)

        return generate()


async def _seed_llm_config():
    async with create_db_session() as session:
        session.add(
            LlmModelEntity(
                tenant_id=DEFAULT_TENANT_ID,
                base_url="http://example.test/v1",
                model="deepseek-v4-pro",
                model_name="deepseek-v4-pro",
                model_id="deepseek-v4-pro",
                encrypted_api_key=encrypt_key("test-key"),
                provider_name="openai_like",
                source="openai_like",
            )
        )


async def _wait_for_status(client, run_id, headers, expected, timeout=120):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/task-manager/runs/{run_id}", headers=headers)
        assert response.status_code == 200
        status = response.json()["run"]["status"]
        if status in expected:
            return status
        await asyncio.sleep(0.03)
    raise AssertionError(f"Run {run_id} did not reach {expected}.")


def test_executor_lock_prevents_duplicate_run(monkeypatch):
    async def run():
        monkeypatch.setenv("TASK_EXECUTOR_LOCK_BACKEND", "memory")
        async with executor_lock("run-lock-test") as first:
            async with executor_lock("run-lock-test") as duplicate:
                assert first is True
                assert duplicate is False
        async with executor_lock("run-lock-test") as released:
            assert released is True

    asyncio.run(run())


def test_pipeline_definition_rejects_dependency_cycles():
    definition = PipelineDefinition(
        pipeline_id="cycle",
        version="1",
        task_type="cycle.task",
        stages=(
            StageDefinition(
                stage_id="a",
                name="A",
                stage_type="deterministic",
                depends_on=("b",),
                service_handler="unused",
            ),
            StageDefinition(
                stage_id="b",
                name="B",
                stage_type="deterministic",
                depends_on=("a",),
                service_handler="unused",
            ),
        ),
    )
    try:
        validate_pipeline_definition(definition)
    except ValueError as exc:
        assert "dependency cycle" in str(exc)
    else:
        raise AssertionError("Cycle validation should fail.")


def test_pipeline_ready_stages_run_in_parallel_and_finish_in_delay_order(monkeypatch):
    async def run():
        definition = PipelineDefinition(
            pipeline_id="parallel-test",
            version="1",
            task_type="parallel.test",
            max_parallelism=2,
            final_artifact_type="final",
            stages=(
                StageDefinition("slow", "Slow", "deterministic", service_handler="unused"),
                StageDefinition("fast", "Fast", "deterministic", service_handler="unused"),
                StageDefinition(
                    "finalize",
                    "Finalize",
                    "finalizer",
                    depends_on=("slow", "fast"),
                    artifact_type="final",
                    service_handler="unused",
                ),
            ),
        )
        executor = PipelineExecutor(SimpleNamespace())
        completion_order = []

        async def fake_stream_stage(self, *, stage, artifacts, **kwargs):
            await asyncio.sleep({"slow": 0.16, "fast": 0.06, "finalize": 0.01}[stage.stage_id])
            completion_order.append(stage.stage_id)
            artifacts[stage.stage_id] = SimpleNamespace(
                id=f"artifact-{stage.stage_id}",
                artifact_type="final" if stage.stage_id == "finalize" else stage.stage_id,
                content_json={"stage": stage.stage_id},
            )
            yield TaskHandlerEvent(
                event_type="stage_completed",
                stage=stage.stage_id,
                message="done",
            )

        async def no_stage_runs(_run_id):
            return []

        async def no_artifacts(_run_id):
            return {}

        async def no_cancel(_run_id):
            return None

        async def no_sink(*args, **kwargs):
            return None

        executor._stream_stage = MethodType(fake_stream_stage, executor)
        monkeypatch.setattr(pipeline_executor_mod, "list_stage_runs", no_stage_runs)
        monkeypatch.setattr(pipeline_executor_mod, "_artifacts_by_stage", no_artifacts)
        monkeypatch.setattr(pipeline_executor_mod, "_raise_if_cancelled", no_cancel)
        monkeypatch.setattr(pipeline_executor_mod, "deliver_task_result", no_sink)
        context = SimpleNamespace(
            task=SimpleNamespace(id="task", input_payload_json={}),
            task_type=SimpleNamespace(result_sink_url=None),
        )
        pipeline_run = SimpleNamespace(id="run", metadata_json={})

        started = time.perf_counter()
        events = [event async for event in executor.stream(
            context=context,
            run=pipeline_run,
            definition=definition,
        )]
        duration = time.perf_counter() - started

        assert duration < 0.23
        assert completion_order == ["fast", "slow", "finalize"]
        assert events[-1].event_type == "result_snapshot"

    asyncio.run(run())


def test_required_stage_sink_rejection_retries_before_artifact_commit(monkeypatch):
    async def run():
        executor = PipelineExecutor(SimpleNamespace())
        task = SimpleNamespace(
            id="task-required-sink",
            task_type="contract.review.run",
            input_payload_json={"review_id": "review-1"},
        )
        context = SimpleNamespace(task=task, task_type=SimpleNamespace(result_sink_url=None))
        pipeline_run = SimpleNamespace(
            id="run-required-sink",
            metadata_json={},
            cancel_requested=False,
        )
        stage = StageDefinition(
            stage_id="validated-stage",
            name="Validated stage",
            stage_type="gateway",
            service_handler="validated-stage-handler",
            artifact_type="validated-result",
            retry_policy=RetryPolicy(
                max_attempts=2,
                retry_on=("required_result_sink_failed",),
            ),
        )
        handler_calls = 0
        sink_calls = 0
        artifact_calls = 0

        async def handler(_context):
            nonlocal handler_calls
            handler_calls += 1
            return StageServiceResult(output={"attempt": handler_calls})

        async def deliver(*_args, **_kwargs):
            nonlocal sink_calls
            sink_calls += 1
            if sink_calls == 1:
                try:
                    raise ResultSinkRejectedError("source anchor rejected")
                except ResultSinkRejectedError as exc:
                    raise RequiredResultSinkError("source anchor rejected") from exc

        async def create_stage_run(**kwargs):
            return SimpleNamespace(id=f"stage-run-{kwargs['attempt']}", agent_id=None)

        async def create_artifact(**kwargs):
            nonlocal artifact_calls
            artifact_calls += 1
            return SimpleNamespace(
                id="artifact-accepted",
                artifact_type=kwargs["artifact_type"],
                artifact_version=1,
                checksum="checksum",
                content_json=kwargs["content"],
            )

        async def no_update(*_args, **_kwargs):
            return None

        async def current_run(_run_id):
            return pipeline_run

        monkeypatch.setattr(pipeline_executor_mod, "is_required_result_sink", lambda _task_type: True)
        monkeypatch.setattr(pipeline_executor_mod, "deliver_task_result", deliver)
        monkeypatch.setattr(pipeline_executor_mod, "get_stage_handler", lambda _name: handler)
        monkeypatch.setattr(pipeline_executor_mod, "create_stage_run", create_stage_run)
        monkeypatch.setattr(pipeline_executor_mod, "create_artifact", create_artifact)
        monkeypatch.setattr(pipeline_executor_mod, "update_run", no_update)
        monkeypatch.setattr(pipeline_executor_mod, "update_stage_run", no_update)
        monkeypatch.setattr(pipeline_executor_mod, "get_run", current_run)

        artifacts = {}
        events = [event async for event in executor._stream_stage(
            context=context,
            run=pipeline_run,
            definition=SimpleNamespace(),
            stage=stage,
            stage_index=1,
            artifacts=artifacts,
            attempt_offsets={},
        )]

        assert handler_calls == 2
        assert sink_calls == 2
        assert artifact_calls == 1
        assert artifacts[stage.stage_id].content_json == {"attempt": 2}
        assert "stage_retrying" in {event.event_type for event in events}

    asyncio.run(run())


def test_stage_retry_feedback_is_bounded_and_visible_to_next_model_attempt():
    message = pipeline_executor_mod._stage_message(
        SimpleNamespace(task_type="contract.review.run"),
        StageDefinition(
            stage_id="extract_contract_ir",
            name="Extract Contract IR",
            stage_type="agent",
            agent_config=AgentStageConfig(agent_id="contract-agent"),
        ),
        {"task_input": {"review_id": "review-1"}},
        retry_feedback="source anchor rejected" + ("x" * 3000),
    )

    assert "Previous attempt rejection:" in message
    assert "source anchor rejected" in message
    assert "must start with '{'" in message
    assert len(message.rsplit("Correct that rejection", 1)[0]) < 5000


def test_direct_model_stage_uses_skill_and_llm_runtime_without_react(monkeypatch):
    async def run():
        class SessionContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return None

        profile = SimpleNamespace(
            enabled=True,
            agent_id="summary-agent",
            model_id="summary-model",
            system_prompt="PROFILE SYSTEM",
            default_tools=[],
            default_datasets=[],
        )
        captured = {}

        async def ensure_profiles(_session):
            return None

        async def get_profile(_session, agent_id):
            assert agent_id == "summary-agent"
            return profile

        class FakeSkillManager:
            def __init__(self, tenant_id):
                assert tenant_id == "tenant"

            async def create_context(self, package):
                assert package == "summary-package"
                return SimpleNamespace(task_prompt="SKILL RULES")

        class FakeLlmRuntime:
            def __init__(self, tenant_id):
                assert tenant_id == "tenant"

            async def complete(self, *, messages, model_id, system_prompt):
                captured.update(
                    messages=messages,
                    model_id=model_id,
                    system_prompt=system_prompt,
                )
                return '{"plain_summary":"ok"}'

        class ForbiddenReact:
            def __init__(self, *args, **kwargs):
                raise AssertionError("ReactAgent must not be constructed by direct_model")

        monkeypatch.setattr(pipeline_executor_mod, "create_db_session", lambda: SessionContext())
        monkeypatch.setattr(pipeline_executor_mod, "ensure_default_agent_profiles", ensure_profiles)
        monkeypatch.setattr(pipeline_executor_mod, "get_agent_profile", get_profile)
        monkeypatch.setattr(pipeline_executor_mod, "SkillManager", FakeSkillManager)
        monkeypatch.setattr(pipeline_executor_mod, "LlmRuntime", FakeLlmRuntime)
        monkeypatch.setattr(runner_mod, "ReactAgent", ForbiddenReact)

        executor = PipelineExecutor(SimpleNamespace())
        stage = StageDefinition(
            stage_id="summary",
            name="Summary",
            stage_type="direct_model",
            agent_config=AgentStageConfig(
                agent_id="summary-agent",
                skill_package="summary-package",
            ),
        )
        events = [event async for event in executor._stream_direct_model_stage(
            context=SimpleNamespace(),
            task=SimpleNamespace(tenant_id="tenant", task_type="proof.audit.run"),
            stage=stage,
            stage_run_id="stage-run",
            stage_input={"summary_chunks": [{"id": "chunk-1", "text": "正文"}]},
        )]

        assert events[-1].structured_output == {"plain_summary": "ok"}
        assert captured["model_id"] == "summary-model"
        assert captured["system_prompt"] == "PROFILE SYSTEM\n\nSKILL RULES"
        assert "chunk-1" in captured["messages"][0]["content"]

    asyncio.run(run())


def test_pipeline_run_retry_replay_and_human_review(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pipeline.db'}")
        monkeypatch.setenv("TASK_EVENT_BROKER", "memory")
        reset_engine_for_test()
        reset_event_broker_for_test()
        await init_db()
        await _seed_llm_config()
        RetryThenPipelineAgent.calls = 0
        monkeypatch.setattr(runner_mod, "ReactAgent", RetryThenPipelineAgent)

        app = create_app()
        headers = {"X-User-Id": "pipeline-user", "X-Tenant-Id": DEFAULT_TENANT_ID}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            definitions = await client.get("/task-manager/definitions")
            demo = next(item for item in definitions.json()["definitions"] if item["task_type"] == "pipeline.demo")
            assert demo["handler"] == "pipeline"
            assert demo["pipeline_id"] == "pipeline-demo-v1"

            pipelines = await client.get("/task-manager/pipelines")
            pipeline = next(item for item in pipelines.json()["pipelines"] if item["pipeline_id"] == "pipeline-demo-v1")
            assert [stage["stage_id"] for stage in pipeline["stages"]] == ["analyze", "normalize", "finalize"]

            create = await client.post(
                "/task-manager/tasks",
                headers={**headers, "Idempotency-Key": "pipeline-task-001"},
                json={
                    "task_type": "pipeline.demo",
                    "title": "Pipeline test",
                    "input": {"goal": "Explain reusable pipelines"},
                    "client_context": {"source": "pipeline-test"},
                },
            )
            assert create.status_code == 200
            task_id = create.json()["task"]["id"]
            duplicate_create = await client.post(
                "/task-manager/tasks",
                headers={**headers, "Idempotency-Key": "pipeline-task-001"},
                json={"task_type": "pipeline.demo", "input": {"goal": "Explain reusable pipelines"}},
            )
            assert duplicate_create.json()["task"]["id"] == task_id
            start = await client.post(
                f"/task-manager/tasks/{task_id}/runs",
                headers={**headers, "Idempotency-Key": "pipeline-run-001"},
                json={},
            )
            assert start.status_code == 200
            run_id = start.json()["run_id"]
            duplicate_start = await client.post(
                f"/task-manager/tasks/{task_id}/runs",
                headers={**headers, "Idempotency-Key": "pipeline-run-001"},
                json={},
            )
            assert duplicate_start.json()["run_id"] == run_id
            live_stream = await client.get(start.json()["stream_url"], headers=headers)
            assert live_stream.status_code == 200
            assert "event: stage_started" in live_stream.text
            assert "event: agent_delta" in live_stream.text
            assert "event: task_succeeded" in live_stream.text
            assert await _wait_for_status(client, run_id, headers, {"succeeded", "failed"}) == "succeeded"

            stages = (await client.get(f"/task-manager/runs/{run_id}/stages", headers=headers)).json()["stages"]
            assert [(item["stage_id"], item["attempt"], item["status"]) for item in stages] == [
                ("analyze", 1, "failed"),
                ("analyze", 2, "succeeded"),
                ("normalize", 1, "succeeded"),
                ("finalize", 1, "succeeded"),
            ]
            artifacts = (await client.get(f"/task-manager/tasks/{task_id}/artifacts", headers=headers)).json()["artifacts"]
            assert [item["artifact_type"] for item in artifacts] == [
                "pipeline_demo_analysis",
                "pipeline_demo_normalized",
                "pipeline_demo_result",
            ]
            assert artifacts[-1]["content_json"]["status"] == "success"

            events_response = await client.get(f"/task-manager/runs/{run_id}/events", headers=headers)
            events = events_response.json()["events"]
            sequences = [event["sequence"] for event in events]
            assert sequences == list(range(1, len(sequences) + 1))
            event_types = [event["event_type"] for event in events]
            assert "stage_retrying" in event_types
            assert "agent_delta" in event_types
            assert "artifact_created" in event_types
            assert event_types[-1] == "task_succeeded"

            after = events[-3]["sequence"]
            replay = await client.get(
                f"/task-manager/runs/{run_id}/events/stream?after_sequence={after}",
                headers=headers,
            )
            assert replay.status_code == 200
            assert f"id: {run_id}:{after + 1}" in replay.text
            assert "event: task_succeeded" in replay.text

            RetryThenPipelineAgent.calls = 1
            review_create = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "pipeline.demo",
                    "title": "Human review test",
                    "input_payload": {"goal": "Test approval", "require_human_review": True},
                },
            )
            review_task_id = review_create.json()["task"]["id"]
            review_start = await client.post(f"/task-manager/tasks/{review_task_id}/runs", headers=headers, json={})
            review_run_id = review_start.json()["run_id"]
            assert await _wait_for_status(client, review_run_id, headers, {"waiting_human", "failed"}) == "waiting_human"

            review_events = (
                await client.get(f"/task-manager/runs/{review_run_id}/events", headers=headers)
            ).json()["events"]
            assert review_events[-1]["event_type"] == "human_review_required"
            approve = await client.post(
                f"/task-manager/runs/{review_run_id}/review",
                headers=headers,
                json={"action": "approve", "comment": "Approved", "resume_from_stage": "finalize"},
            )
            assert approve.status_code == 200
            assert await _wait_for_status(client, review_run_id, headers, {"succeeded", "failed"}) == "succeeded"
            resumed_events = (
                await client.get(f"/task-manager/runs/{review_run_id}/events", headers=headers)
            ).json()["events"]
            resumed_types = [event["event_type"] for event in resumed_events]
            assert "human_review_submitted" in resumed_types
            assert "pipeline_resumed" in resumed_types
            assert resumed_types[-1] == "task_succeeded"

    try:
        asyncio.run(run())
    finally:
        reset_event_broker_for_test()
        reset_engine_for_test()


def test_pipeline_cancel_is_cooperative(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cancel.db'}")
        monkeypatch.setenv("TASK_EVENT_BROKER", "memory")
        reset_engine_for_test()
        reset_event_broker_for_test()
        await init_db()
        await _seed_llm_config()
        monkeypatch.setattr(runner_mod, "ReactAgent", SlowPipelineAgent)
        app = create_app()
        headers = {"X-User-Id": "pipeline-user", "X-Tenant-Id": DEFAULT_TENANT_ID}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={"task_type": "pipeline.demo", "input_payload": {"goal": "slow cancellation test"}},
            )
            task_id = created.json()["task"]["id"]
            started = await client.post(f"/task-manager/tasks/{task_id}/runs", headers=headers, json={})
            run_id = started.json()["run_id"]
            await asyncio.sleep(0.12)
            cancelled = await client.post(f"/task-manager/runs/{run_id}/cancel", headers=headers)
            assert cancelled.status_code == 200
            assert cancelled.json()["run"]["cancel_requested"] is True
            assert await _wait_for_status(client, run_id, headers, {"cancelled", "failed"}) == "cancelled"
            events = (await client.get(f"/task-manager/runs/{run_id}/events", headers=headers)).json()["events"]
            assert events[-1]["event_type"] == "task_cancelled"

    try:
        asyncio.run(run())
    finally:
        reset_event_broker_for_test()
        reset_engine_for_test()
