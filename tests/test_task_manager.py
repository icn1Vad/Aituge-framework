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
from task_manager.models import TaskMemoryEntity
from task_manager.output_parser import parse_json_output
import task_manager.adapters.legacy_douyin as legacy_douyin_adapter
import service.agent.single_agent_runner as runner_mod


class JsonCapturingAgent:
    all_system_prompts: list[str] = []
    table_audit_system_prompts: list[str] = []

    def __init__(self, llm, system_prompt, tools):
        self.system_prompt = system_prompt
        self.tools = tools
        self.all_system_prompts.append(system_prompt)

    async def run_async(self, state):
        async def gen():
            if "# Douyin Account Report Skill" in self.system_prompt:
                content = {
                    "status": "partial_data",
                    "account_id": "demo-douyin-account",
                    "account_name": "Demo Douyin Account",
                    "platform": "douyin",
                    "analysis_scope": "all_data",
                    "covered_period": {"scope": "all_available_data"},
                    "warnings": ["sample data only"],
                    "missing_fields": ["comment_text", "full_script_text"],
                    "executive_summary": "The account has usable all-data evidence but lacks comment text and scripts.",
                    "key_metrics": {"play_count": 18600, "publish_count": 3},
                    "sections": {
                        "overall_performance": {
                            "title": "Overall performance",
                            "summary": "The sample content has uneven performance.",
                            "findings": ["video-001 is the strongest item."],
                            "evidence": ["video-001 play_count=9200"],
                            "limitations": ["No monthly split required for this all-data task."],
                            "next_actions": ["Expand the strongest topic type."],
                        }
                    },
                    "top_content_analysis": [{"id": "video-001", "reason": "Highest play count."}],
                    "low_content_analysis": [{"id": "video-003", "reason": "Lowest play count."}],
                    "data_limitations": ["No comment text."],
                    "next_month_actions": ["Publish follow-up policy explainer."],
                    "export_markdown": "# Account report\n\nSample report.",
                }
                yield TextChunk(delta=json.dumps(content, ensure_ascii=False))
                return

            if "# Table Audit" in self.system_prompt:
                self.table_audit_system_prompts.append(self.system_prompt)
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

            if "Prefer a direct, evidence-backed selection" in self.system_prompt:
                yield TextChunk(delta='{"selected":"candidate-a"}')
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


def test_task_manager_create_run_and_events(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'task-manager.db'}")
        reset_engine_for_test()
        monkeypatch.setattr(runner_mod, "ReactAgent", JsonCapturingAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        await init_db()
        await _seed_llm_config()
        JsonCapturingAgent.all_system_prompts = []
        JsonCapturingAgent.table_audit_system_prompts = []
        async with create_db_session() as session:
            session.add_all(
                [
                    TaskMemoryEntity(
                        tenant_id=DEFAULT_TENANT_ID,
                        user_id="task-manager-test-user",
                        task_key="table-audit-smoke",
                        content="Always verify the evidence column before passing a row.",
                    ),
                    TaskMemoryEntity(
                        tenant_id=DEFAULT_TENANT_ID,
                        user_id="task-manager-test-user",
                        task_key="media-select-smoke",
                        content="Prefer a direct, evidence-backed selection.",
                    ),
                ]
            )
            await session.commit()

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {
                "X-User-Id": "task-manager-test-user",
                "X-Tenant-Id": DEFAULT_TENANT_ID,
            }
            other_user_headers = {
                "X-User-Id": "other-user",
                "X-Tenant-Id": DEFAULT_TENANT_ID,
            }
            definitions = await client.get("/task-manager/definitions")
            assert definitions.status_code == 200
            assert any(
                item["task_type"] == "media.script.generate"
                for item in definitions.json()["definitions"]
            )
            script_definition = next(
                item for item in definitions.json()["definitions"]
                if item["task_type"] == "media.script.generate"
            )
            assert script_definition["handler"] == "external"
            assert script_definition["required_task_key"] == "media_script"
            assert script_definition["default_agent_id"] == "main-agent-runtime"
            table_definition = next(
                item for item in definitions.json()["definitions"]
                if item["task_type"] == "table.audit"
            )
            douyin_report_definition = next(
                item for item in definitions.json()["definitions"]
                if item["task_type"] == "analytics.douyin.account_report.generate"
            )
            assert table_definition["input_schema_name"] == "table_audit_input"
            assert table_definition["output_schema_name"] == "batch_task_output"
            assert table_definition["item_output_schema_name"] == "table_audit_item_output"
            assert table_definition["default_skill_package"] == "table-audit-package"
            assert douyin_report_definition["default_agent_id"] == "report-agent"
            assert douyin_report_definition["default_skill_package"] == "douyin-account-report-package"
            assert douyin_report_definition["input_schema_name"] == "douyin_account_report_input"
            assert douyin_report_definition["output_schema_name"] == "douyin_account_report_output"

            missing_field_response = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "media.script.generate",
                    "title": "Missing topic",
                    "task_key": "media_script",
                    "input_payload": {
                        "platform": "douyin",
                        "duration_seconds": 60,
                    },
                },
            )
            assert missing_field_response.status_code == 400
            assert "media_script_main_agent_input" in missing_field_response.text

            extra_field_response = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "media.script.generate",
                    "title": "Extra field",
                    "task_key": "media_script",
                    "input_payload": {
                        "workspace_id": "workspace-extra",
                        "instruction": "Test strict input validation.",
                        "operation": "interact",
                        "unexpected_field": "must fail",
                    },
                },
            )
            assert extra_field_response.status_code == 400
            assert "unexpected_field" in extra_field_response.text

            invalid_policy_response = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "table.audit",
                    "title": "Invalid policy",
                    "input_payload": {
                        "audit_goal": "Audit rows.",
                        "failure_policy": "skip_everything",
                        "rows": [{"id": "row-001"}],
                    },
                },
            )
            assert invalid_policy_response.status_code == 400
            assert "table_audit_input" in invalid_policy_response.text

            async def fake_fetch_legacy_douyin_data(*, base_url, account_id, content_limit):
                assert base_url == "http://legacy.test"
                assert account_id == "acct_douyin_demo"
                assert content_limit == 20
                return (
                    {
                        "account": {
                            "id": "acct_douyin_demo",
                            "platform": "douyin",
                            "account_name": "Legacy Douyin Demo",
                        },
                        "metrics": {
                            "account_id": "acct_douyin_demo",
                            "date": "2026-07-01",
                            "fans_count": 73,
                            "new_fans_count": 1,
                            "profile_visit_count": 49,
                            "publish_count": 2,
                            "play_count": 300,
                            "like_count": 30,
                            "comment_count": 4,
                            "share_count": 2,
                            "collect_count": 1,
                        },
                    },
                    {
                        "items": [
                            {
                                "content_id": "video-001",
                                "title": "Legacy top video",
                                "url": "https://www.douyin.com/video/video-001",
                                "publish_time": "2026-06-01T00:00:00+00:00",
                                "play_count": 200,
                                "like_count": 20,
                                "comment_count": 3,
                                "share_count": 1,
                                "collect_count": 1,
                                "raw": {
                                    "derived": {
                                        "completion_rate": 0.2,
                                        "avg_view_second": 6.5,
                                    }
                                },
                            },
                            {
                                "content_id": "video-002",
                                "title": "Legacy lower video",
                                "play_count": 100,
                                "like_count": 10,
                                "comment_count": 1,
                                "share_count": 1,
                                "collect_count": 0,
                            },
                        ]
                    },
                )

            monkeypatch.setattr(
                legacy_douyin_adapter,
                "_fetch_legacy_douyin_data",
                fake_fetch_legacy_douyin_data,
            )
            legacy_create_response = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "analytics.douyin.account_report.generate",
                    "title": "Legacy Douyin report",
                    "input_payload": {
                        "data_source": "legacy_douyin_api",
                        "legacy_api_base_url": "http://legacy.test",
                        "account_id": "acct_douyin_demo",
                        "content_limit": 20,
                    },
                },
            )
            assert legacy_create_response.status_code == 200
            legacy_payload = legacy_create_response.json()["task"]["input_payload_json"]
            assert legacy_payload["data_source"] == "legacy_douyin_api"
            assert legacy_payload["account_name"] == "Legacy Douyin Demo"
            assert legacy_payload["metrics_summary"]["play_count"] == 300
            assert legacy_payload["report_context"]["source"] == "legacy_douyin_api"
            assert [item["id"] for item in legacy_payload["content_items"]] == ["video-001", "video-002"]

            missing_report_data_response = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "analytics.douyin.account_report.generate",
                    "title": "Missing report data",
                    "input_payload": {
                        "analysis_scope": "all_data",
                    },
                },
            )
            assert missing_report_data_response.status_code == 400
            assert "douyin_account_report_input" in missing_report_data_response.text

            create_response = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "media.script.generate",
                    "title": "生成短视频脚本",
                    "task_key": "media_script",
                    "user_id": "task-manager-test-user",
                    "stream": True,
                    "input_payload": {
                        "workspace_id": "workspace-main-agent",
                        "instruction": "Agent 架构设计",
                        "operation": "generate",
                    },
                },
            )
            assert create_response.status_code == 200
            created_task = create_response.json()["task"]
            task_id = created_task["id"]
            assert created_task["root_task_id"] == task_id
            assert created_task["handler_name"] == "external"
            assert created_task["task_key"] == "media_script"
            assert created_task["agent_id"] == "main-agent-runtime"
            assert created_task["user_id"] == "task-manager-test-user"
            assert created_task["tenant_id"] == DEFAULT_TENANT_ID
            assert created_task["definition_snapshot_json"]["task_type"] == "media.script.generate"
            assert created_task["progress_total"] == 1
            assert created_task["cancel_requested"] is False

            forbidden_response = await client.get(f"/task-manager/tasks/{task_id}", headers=other_user_headers)
            assert forbidden_response.status_code == 403

            gateway_ref_response = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "media.script.generate",
                    "title": "Gateway ref smoke",
                    "task_key": "media_script",
                    "input_payload": {
                        "workspace_id": "workspace-gateway",
                        "instruction": "Gateway",
                        "operation": "interact",
                        "resource_refs": [
                            {"type": "kb", "id": "kb_1", "operation": "search"}
                        ],
                    },
                },
            )
            assert gateway_ref_response.status_code == 200
            gateway_payload = gateway_ref_response.json()["task"]["input_payload_json"]
            assert gateway_payload["validated_resource_refs"][0]["id"] == "kb_1"

            blocked_ref_response = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "media.script.generate",
                    "title": "Blocked ref",
                    "task_key": "media_script",
                    "input_payload": {
                        "workspace_id": "workspace-blocked",
                        "instruction": "Gateway",
                        "operation": "interact",
                        "resource_refs": [
                            {"type": "file", "id": "file_1", "metadata": {"path": "E:/secret.txt"}}
                        ],
                    },
                },
            )
            assert blocked_ref_response.status_code == 400
            assert "forbidden metadata keys" in blocked_ref_response.text

            item_create_response = await client.post(
                "/task-manager/tasks",
                headers=headers,
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
            items_response = await client.get(f"/task-manager/tasks/{item_task['id']}/items", headers=headers)
            assert items_response.status_code == 200
            items = items_response.json()["items"]
            assert [item["item_key"] for item in items] == ["candidate-a", "candidate-b"]
            assert all(item["item_type"] == "script_candidate" for item in items)
            assert all(item["status"] == "pending" for item in items)
            single_run_response = await client.post(
                f"/task-manager/tasks/{item_task['id']}/run",
                headers=headers,
                json={"stream": False},
            )
            assert single_run_response.status_code == 200, single_run_response.text
            assert any(
                "Prefer a direct, evidence-backed selection" in prompt
                for prompt in JsonCapturingAgent.all_system_prompts
            )

            events_response = await client.get(f"/task-manager/tasks/{task_id}/events", headers=headers)
            assert events_response.status_code == 200
            event_types = [event["event_type"] for event in events_response.json()["events"]]
            assert "task_created" in event_types
            events = events_response.json()["events"]
            assert all("token_usage_json" in event for event in events)

            douyin_report_response = await client.post(
                "/task-manager/run",
                headers=headers,
                json={
                    "task_type": "analytics.douyin.account_report.generate",
                    "title": "Douyin all-data report",
                    "user_id": "task-manager-test-user",
                    "stream": False,
                    "input_payload": {
                        "account_id": "demo-douyin-account",
                        "account_name": "Demo Douyin Account",
                        "analysis_scope": "all_data",
                        "report_depth": "deep",
                        "report_context": {
                            "metrics_summary": {"play_count": 18600, "publish_count": 3},
                            "missing_fields": ["comment_text", "full_script_text"],
                        },
                        "content_items": [
                            {"id": "video-001", "title": "A", "play_count": 9200},
                            {"id": "video-003", "title": "C", "play_count": 3300},
                        ],
                    },
                },
            )
            assert douyin_report_response.status_code == 200
            douyin_report_task = douyin_report_response.json()["task"]
            assert douyin_report_task["status"] == "succeeded"
            assert douyin_report_task["agent_id"] == "report-agent"
            assert (
                douyin_report_task["result_payload_json"]["structured"]["analysis_scope"]
                == "all_data"
            )
            assert "export_markdown" in douyin_report_task["result_payload_json"]["structured"]
            legacy_monthly = douyin_report_task["result_payload_json"]["structured"]["legacy_monthly_report"]
            assert legacy_monthly["sections"]["conclusion"]["overall_summary"]
            assert legacy_monthly["sections"]["content_and_script_clues"]["content_performance"]
            assert legacy_monthly["sections"]["interaction_and_comments"]["comment_count_analysis"]

            batch_response = await client.post(
                "/task-manager/run",
                headers=headers,
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

            batch_items_response = await client.get(f"/task-manager/tasks/{batch_task_id}/items", headers=headers)
            assert batch_items_response.status_code == 200
            batch_items = batch_items_response.json()["items"]
            assert [item["item_key"] for item in batch_items] == ["row-001", "row-002", "row-003"]
            assert all(item["item_type"] == "table_row" for item in batch_items)
            assert all(item["status"] == "succeeded" for item in batch_items)
            assert all(item["result_payload_json"]["result"]["risk_level"] == "low" for item in batch_items)

            batch_events_response = await client.get(f"/task-manager/tasks/{batch_task_id}/events", headers=headers)
            assert batch_events_response.status_code == 200
            batch_events = batch_events_response.json()["events"]
            batch_event_types = [event["event_type"] for event in batch_events]
            assert "batch_started" in batch_event_types
            assert "item_started" in batch_event_types
            assert "item_succeeded" in batch_event_types
            assert "batch_succeeded" in batch_event_types
            assert any(event["item_id"] for event in batch_events if event["event_type"].startswith("item_"))
            assert len(JsonCapturingAgent.table_audit_system_prompts) == 3
            assert all(
                "Always verify the evidence column" in prompt
                for prompt in JsonCapturingAgent.table_audit_system_prompts
            )

            for item in batch_items:
                await session_history_manager.clear_history("task-manager-test-user", f"{batch_task_id}:{item['id']}")

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_output_parser_extracts_json_from_wrapped_content():
    content = 'Here is the result:\n```json\n{"ok": true, "items": [1, 2,],}\n```\nDone.'
    parsed = parse_json_output(content)
    assert parsed.ok
    assert parsed.structured == {"ok": True, "items": [1, 2]}
