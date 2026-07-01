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
            if "# Table Audit" in self.system_prompt:
                content = {
                    "risk_level": "low",
                    "passed": True,
                    "issues": [],
                    "reason": "The row is acceptable in the test fixture.",
                    "recommended_action": "No action required.",
                    "evidence": ["test fixture"],
                }
                yield TextChunk(delta=json.dumps(content, ensure_ascii=False))
                return

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
            created_task = create_response.json()["task"]
            task_id = created_task["id"]
            assert created_task["root_task_id"] == task_id
            assert created_task["handler_name"] == "scheduler"
            assert created_task["definition_snapshot_json"]["task_type"] == "media.script.generate"
            assert created_task["progress_total"] == 1
            assert created_task["cancel_requested"] is False

            item_create_response = await client.post(
                "/task-manager/tasks",
                json={
                    "task_type": "media.script.select",
                    "title": "Script candidate structure test",
                    "task_key": "media-select-smoke",
                    "user_id": "task-manager-test-user",
                    "input_payload": {
                        "topic": "TaskManager",
                        "script_candidates": [
                            {"id": "candidate-a", "title": "A", "content": "A script"},
                            {"id": "candidate-b", "title": "B", "content": "B script"},
                        ],
                    },
                },
            )
            assert item_create_response.status_code == 200
            item_task = item_create_response.json()["task"]
            assert item_task["task_key"] == "media-select-smoke"
            assert item_task["progress_total"] == 2
            items_response = await client.get(f"/task-manager/tasks/{item_task['id']}/items")
            assert items_response.status_code == 200
            items = items_response.json()["items"]
            assert [item["item_key"] for item in items] == ["candidate-a", "candidate-b"]
            assert all(item["item_type"] == "script_candidate" for item in items)
            assert all(item["status"] == "pending" for item in items)

            run_response = await client.post(f"/task-manager/tasks/{task_id}/run", json={"stream": False})
            assert run_response.status_code == 200
            body = run_response.json()
            task = body["task"]
            assert task["status"] == "succeeded"
            assert task["progress_current"] == task["progress_total"] == 1
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
            events = events_response.json()["events"]
            assert any(event["step_id"] == "scheduler_request_build" for event in events)
            assert any(event["step_id"] == "agent_final" for event in events)
            assert all("token_usage_json" in event for event in events)

            batch_response = await client.post(
                "/task-manager/run",
                json={
                    "task_type": "table.audit",
                    "title": "Table audit batch test",
                    "task_key": "table-audit-smoke",
                    "user_id": "task-manager-test-user",
                    "stream": False,
                    "input_payload": {
                        "audit_goal": "Audit each row for risk.",
                        "max_concurrency": 2,
                        "failure_policy": "continue",
                        "retry_per_item": 0,
                        "rows": [
                            {"id": "row-001", "name": "A company", "amount": 12000},
                            {"id": "row-002", "name": "B company", "amount": 800},
                            {"id": "row-003", "name": "C company", "amount": 4500},
                        ],
                    },
                },
            )
            assert batch_response.status_code == 200
            batch_body = batch_response.json()
            batch_task = batch_body["task"]
            batch_task_id = batch_task["id"]
            assert batch_task["status"] == "succeeded"
            assert batch_task["handler_name"] == "batch_item_scheduler"
            assert batch_task["progress_current"] == batch_task["progress_total"] == 3
            assert batch_task["result_payload_json"]["structured"]["summary"]["total"] == 3
            assert batch_task["result_payload_json"]["structured"]["summary"]["succeeded"] == 3

            batch_items_response = await client.get(f"/task-manager/tasks/{batch_task_id}/items")
            assert batch_items_response.status_code == 200
            batch_items = batch_items_response.json()["items"]
            assert [item["item_key"] for item in batch_items] == ["row-001", "row-002", "row-003"]
            assert all(item["item_type"] == "table_row" for item in batch_items)
            assert all(item["status"] == "succeeded" for item in batch_items)
            assert all(item["result_payload_json"]["result"]["risk_level"] == "low" for item in batch_items)

            batch_events_response = await client.get(f"/task-manager/tasks/{batch_task_id}/events")
            assert batch_events_response.status_code == 200
            batch_events = batch_events_response.json()["events"]
            batch_event_types = [event["event_type"] for event in batch_events]
            assert "batch_started" in batch_event_types
            assert "item_started" in batch_event_types
            assert "item_succeeded" in batch_event_types
            assert "batch_succeeded" in batch_event_types
            assert any(event["item_id"] for event in batch_events if event["event_type"].startswith("item_"))

            for item in batch_items:
                await session_history_manager.clear_history("task-manager-test-user", f"{batch_task_id}:{item['id']}")

        await session_history_manager.clear_history("task-manager-test-user", task["session_id"])
        await _delete_thread(task["thread_id"])

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
