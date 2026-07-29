import asyncio

import httpx

from backend.local_code_chat_app import create_app
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import init_db, reset_engine_for_test
from task_manager.output_parser import parse_json_output


def test_task_manager_exposes_only_framework_task_definitions(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'task-manager.db'}",
        )
        reset_engine_for_test()
        await init_db()

        app = create_app()
        headers = {
            "X-User-Id": "task-manager-test-user",
            "X-Tenant-Id": DEFAULT_TENANT_ID,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/task-manager/definitions", headers=headers)
            assert response.status_code == 200
            definitions = response.json()["definitions"]
            task_types = {item["task_type"] for item in definitions}
            assert {
                "ai.search.chat",
                "pipeline.demo",
                "table.audit",
            }.issubset(task_types)
            assert not any(
                token in task_type.lower()
                for task_type in task_types
                for token in ("douyin", "media.")
            )

            search = next(
                item for item in definitions if item["task_type"] == "ai.search.chat"
            )
            assert search["default_skill_package"] == "ai-search-package"
            assert search["input_schema_name"] == "ai_search_chat_input"
            assert search["output_schema_name"] == "ai_search_output"

            invalid = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "ai.search.chat",
                    "input_payload": {},
                },
            )
            assert invalid.status_code == 400
            assert "ai_search_chat_input" in invalid.text

            created = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "ai.search.chat",
                    "title": "Search framework documentation",
                    "input_payload": {
                        "message": "Find the current framework documentation.",
                        "max_results": 3,
                    },
                },
            )
            assert created.status_code == 200, created.text
            task = created.json()["task"]
            assert task["task_type"] == "ai.search.chat"
            assert task["agent_id"] == "default-single-agent"
            assert task["definition_snapshot_json"]["default_skill_package"] == (
                "ai-search-package"
            )

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_output_parser_extracts_json_from_wrapped_content():
    content = 'Here is the result:\n```json\n{"ok": true, "items": [1, 2,],}\n```\nDone.'
    parsed = parse_json_output(content)
    assert parsed.ok
    assert parsed.structured == {"ok": True, "items": [1, 2]}
