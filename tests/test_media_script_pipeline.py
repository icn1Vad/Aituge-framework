import asyncio
import json

import httpx

from backend.local_code_chat_app import create_app
from common.encrypt_utils import encrypt_key
from common.llm.models import TextChunk
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db, reset_engine_for_test
from db.models.llm import LlmModelEntity
from scheduling.scheduler import SchedulingRuntimeOptions
import service.agent.single_agent_runner as runner_mod
from service.cache.session_history_manager import session_history_manager
from task_manager.models import TaskMemoryEntity
from task_manager.memory import TaskMemoryService
from task_manager.pipeline import media_script as media_pipeline
from task_manager.runtime.broker import reset_event_broker_for_test
from task_manager.runtime.worker import TaskWorker


class MediaPipelineAgent:
    seen_messages: dict[str, str] = {}
    seen_system_prompts: dict[str, str] = {}

    def __init__(self, llm, system_prompt, tools):
        if "Media Script Writer" in system_prompt:
            self.stage = "writer"
        elif "Media Storyboard" in system_prompt:
            self.stage = "storyboard"
        else:
            raise AssertionError(system_prompt[:500])
        self.seen_system_prompts[self.stage] = system_prompt

    async def run_async(self, state):
        self.seen_messages[self.stage] = str(state.messages[-1].get("content") or "")
        payloads = {
            "writer": {
                "final_script": {
                    "topic_name": "Policy boundary",
                    "persona_name": "Veteran adviser",
                    "video_goal": "Explain eligibility",
                    "platform": "douyin",
                    "duration_seconds": 60,
                    "duration_reason": "About 180 Chinese characters.",
                    "target_char_range": [160, 220],
                    "hook_3s": "This policy is not automatic for everyone.",
                    "structure": ["hook", "facts", "action"],
                    "voiceover": "Verify your eligibility against the official notice before applying.",
                    "subtitle_points": ["Check eligibility", "Use official notice"],
                    "visual_direction": ["Presenter on camera"],
                    "material_bridge": "Uses the verified policy notice.",
                    "master_library_usage": {},
                },
                "readable_script": "Verify your eligibility against the official notice before applying.",
                "hermes_agent_result": {"status": "done", "editor_summary": "Boundary-first explanation", "why_this_angle": "Prevents overclaiming", "risks": [], "parse_notes": [], "master_library_usage": {}},
            },
            "storyboard": {
                "storyboard": [{"time": "0-6s", "scene": "Presenter", "shot": "medium", "action": "faces camera", "voiceover": "This policy is not automatic for everyone.", "subtitle_focus": "Check eligibility", "visual_prompt": "document and presenter"}],
                "storyboard_plan": "One presenter shot supported by a document close-up.",
                "visual_direction": ["Document close-up"],
                "warnings": "Keep policy dates visible on screen.",
            },
        }

        async def generate():
            text = json.dumps(payloads[self.stage], ensure_ascii=False)
            yield TextChunk(delta=text[: len(text) // 2])
            yield TextChunk(delta=text[len(text) // 2 :])

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


def test_media_script_lite_pipeline_runs_writer_and_storyboard_agents(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'media-pipeline.db'}")
        monkeypatch.setenv("TASK_EVENT_BROKER", "memory")
        monkeypatch.setenv("TASK_EXECUTOR_LOCK_BACKEND", "memory")
        reset_engine_for_test()
        reset_event_broker_for_test()
        await init_db()
        await _seed_llm_config()
        MediaPipelineAgent.seen_messages = {}
        MediaPipelineAgent.seen_system_prompts = {}
        get_latest_calls = 0
        original_get_latest = TaskMemoryService.get_latest

        async def counting_get_latest(self, **kwargs):
            nonlocal get_latest_calls
            get_latest_calls += 1
            return await original_get_latest(self, **kwargs)

        monkeypatch.setattr(TaskMemoryService, "get_latest", counting_get_latest)
        async with create_db_session() as session:
            session.add(
                TaskMemoryEntity(
                    tenant_id=DEFAULT_TENANT_ID,
                    user_id="media-user",
                    task_key="media-user_jiaoben_123",
                    version=1,
                    content="开头直接给出结论，并保持自然口语。",
                    source_text="用户确认的测试记忆。",
                )
            )
        monkeypatch.setattr(runner_mod, "ReactAgent", MediaPipelineAgent)
        async def no_history(*args, **kwargs):
            return []

        async def ignore_history(*args, **kwargs):
            return None

        monkeypatch.setattr(session_history_manager, "get_history_messages", no_history)
        monkeypatch.setattr(session_history_manager, "save_messages", ignore_history)

        async def fake_gateway(path, payload, context):
            if path.endswith("/context"):
                return {
                    "topic_card": {"id": "topic-1", "topic_name": "Policy boundary"},
                    "source_brief": {"usable_level": "usable"},
                    "current_persona": {"display_name": "Veteran adviser"},
                    "persona_context": {},
                    "material_full": {"ok": True},
                    "material_comments": {"ok": True},
                    "script_stack_recommendation": {},
                    "material_analysis": {},
                    "product_intent": {},
                    "rules": {},
                    "user_constraints": {"duration_seconds": 60},
                    "warnings": [],
                }
            return {
                "passed": False,
                "high_risk": True,
                "score": 42,
                "findings": [{"code": "duration_warning", "message": "Script exceeds the target duration."}],
                "metrics": {},
                "warnings": ["Deterministic warnings do not pause the Lite Pipeline."],
            }

        monkeypatch.setattr(media_pipeline, "_post_gateway", fake_gateway)
        app = create_app()
        headers = {"X-User-Id": "media-user", "X-Tenant-Id": DEFAULT_TENANT_ID}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "media.script.pipeline.generate",
                    "task_key": "media-user_jiaoben_123",
                    "input_payload": {
                        "topic": "Policy boundary",
                        "topic_card_id": "topic-1",
                        "duration_seconds": 60,
                        "require_human_review": True,
                    },
                },
            )
            assert created.status_code == 200, created.text
            task_id = created.json()["task"]["id"]
            started = await client.post(f"/task-manager/tasks/{task_id}/runs", headers=headers, json={})
            assert started.status_code == 200, started.text
            run_id = started.json()["run_id"]
            worker = TaskWorker(
                SchedulingRuntimeOptions(
                    local_python_artifact_dir=tmp_path / "artifacts"
                ),
                worker_id="media-pipeline-test-worker",
            )
            assert await worker.run_once() is True
            for _ in range(300):
                run_response = await client.get(f"/task-manager/runs/{run_id}", headers=headers)
                status = run_response.json()["run"]["status"]
                if status in {"succeeded", "failed", "waiting_human"}:
                    break
                await asyncio.sleep(0.02)
            assert status == "succeeded"
            stages = (await client.get(f"/task-manager/runs/{run_id}/stages", headers=headers)).json()["stages"]
            assert [item["stage_id"] for item in stages] == [
                "context", "writer", "storyboard", "deterministic_checks", "finalize"
            ]
            artifacts = (await client.get(f"/task-manager/tasks/{task_id}/artifacts", headers=headers)).json()["artifacts"]
            assert artifacts[-1]["artifact_type"] == "media_script_output"
            output = artifacts[-1]["content_json"]
            assert output["generation_meta"]["workflow"] == "media-script-lite-pipeline-v1"
            assert output["generation_meta"]["reviewed"] is False
            assert output["generation_meta"]["web_research_used"] is False
            assert output["generation_meta"]["deterministic_checks_passed"] is False
            assert output["workflow_state"]["workflow_status"] == "pass"
            assert output["workflow_state"]["review_result"]["reviewed"] is False
            assert output["final_script"]["storyboard"]
            assert "# Task Memory" in MediaPipelineAgent.seen_system_prompts["writer"]
            assert "开头直接给出结论" in MediaPipelineAgent.seen_system_prompts["writer"]
            assert "开头直接给出结论" in MediaPipelineAgent.seen_system_prompts["storyboard"]
            assert "# Task Memory" not in MediaPipelineAgent.seen_messages["writer"]
            assert get_latest_calls == 1

    try:
        asyncio.run(run())
    finally:
        reset_event_broker_for_test()
        reset_engine_for_test()
