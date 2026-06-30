import asyncio
import json

import httpx

from backend.local_code_chat_app import create_app
from common.encrypt_utils import encrypt_key
from common.llm.models import TextChunk
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db, reset_engine_for_test
from db.models.llm import LlmModelEntity
from service.cache.session_history_manager import session_history_manager
from service.thread.thread_service import ThreadService
import service.agent.single_agent_runner as runner_mod


class JsonCapturingAgent:
    def __init__(self, llm, system_prompt, tools):
        self.system_prompt = system_prompt
        self.tools = tools

    async def run_async(self, state):
        async def gen():
            content = {
                "final_script": {
                    "topic_name": "Agent 架构设计",
                    "persona_name": "燕姐",
                    "video_goal": "Explain the architecture.",
                    "platform": "douyin",
                    "duration_seconds": 60,
                    "duration_reason": "Short explainer.",
                    "target_char_range": "180-220",
                    "hook_3s": "别再把 Agent 当成一个聊天框。",
                    "structure": ["single agent", "scheduler", "task manager"],
                    "voiceover": "TaskManager 负责业务生命周期，Scheduler 负责调度。",
                    "subtitle_points": ["Task lifecycle", "Scheduler", "Single Agent"],
                    "visual_direction": "Architecture cards.",
                    "material_bridge": "会议摘要直接对应架构拆解。",
                    "master_library_usage": {
                        "role_id": None,
                        "strategy_id": None,
                        "template_id": None,
                        "script_type_id": None,
                        "script_example_ids": [],
                        "risk_rule_ids": [],
                        "replace_reason": "",
                    },
                },
                "readable_script": "别再把 Agent 当成一个聊天框。",
                "hermes_agent_result": {
                    "status": "ok",
                    "editor_summary": "Generated for testing.",
                    "why_this_angle": "Architecture is the user topic.",
                    "risks": [],
                    "parse_notes": [],
                    "master_library_usage": {},
                },
            }
            assert "# Media Script Generator" in self.system_prompt
            yield TextChunk(delta=json.dumps(content, ensure_ascii=False))

        return gen()


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


async def _delete_thread(thread_id: str | None):
    if not thread_id:
        return
    async with create_db_session() as session:
        try:
            await ThreadService(session).delete_thread(thread_id)
        except ValueError:
            pass


def test_task_manager_create_run_and_events(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'task-manager.db'}")
        reset_engine_for_test()
        monkeypatch.setattr(runner_mod, "ReactAgent", JsonCapturingAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        await init_db()
        await _seed_llm_config()

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            definitions = await client.get("/task-manager/definitions")
            assert definitions.status_code == 200
            assert any(
                item["task_type"] == "media.script.generate"
                for item in definitions.json()["definitions"]
            )

            create_response = await client.post(
                "/task-manager/tasks",
                json={
                    "task_type": "media.script.generate",
                    "title": "生成短视频脚本",
                    "user_id": "task-manager-test-user",
                    "stream": True,
                    "input_payload": {
                        "topic": "Agent 架构设计",
                        "platform": "douyin",
                        "duration_seconds": 60,
                    },
                },
            )
            assert create_response.status_code == 200
            task_id = create_response.json()["task"]["id"]

            run_response = await client.post(f"/task-manager/tasks/{task_id}/run", json={"stream": False})
            assert run_response.status_code == 200
            body = run_response.json()
            task = body["task"]
            assert task["status"] == "succeeded"
            assert task["thread_id"]
            assert task["session_id"]
            assert task["result_payload_json"]["structured"]["final_script"]["topic_name"] == "Agent 架构设计"
            assert any(event["event_type"] == "task_started" for event in body["events"])
            assert any(event["event_type"] == "task_succeeded" for event in body["events"])

            events_response = await client.get(f"/task-manager/tasks/{task_id}/events")
            assert events_response.status_code == 200
            event_types = [event["event_type"] for event in events_response.json()["events"]]
            assert "task_created" in event_types
            assert "scheduler_request_built" in event_types
            assert "agent_final" in event_types

        await session_history_manager.clear_history("task-manager-test-user", task["session_id"])
        await _delete_thread(task["thread_id"])

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
