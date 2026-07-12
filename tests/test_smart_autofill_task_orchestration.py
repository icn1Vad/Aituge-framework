from __future__ import annotations

import asyncio
import json
import re

import httpx

from backend.local_code_chat_app import create_app
from common.encrypt_utils import encrypt_key
from common.llm.models import TextChunk
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db, reset_engine_for_test
from db.models.llm import LlmModelEntity
import service.agent.single_agent_runner as runner_mod


GROUP_PACKAGES = {
    "project": "smart-fill-project-package",
    "company": "smart-fill-company-package",
    "financial": "smart-fill-financial-package",
    "risk": "smart-fill-risk-package",
    "analysis": "smart-fill-analysis-package",
}

PACKAGE_GROUPS = {package: group for group, package in GROUP_PACKAGES.items()}


class ParallelSmartFillAgent:
    active = 0
    peak_active = 0
    seen_packages: list[str] = []
    seen_tool_names: set[str] = set()

    def __init__(self, llm, system_prompt, tools):
        self.system_prompt = system_prompt
        self.tools = tools
        type(self).seen_tool_names.update(tool.metadata.name for tool in tools)

    async def run_async(self, state):
        async def gen():
            package_match = re.search(r"^Package: (.+)$", self.system_prompt, re.MULTILINE)
            assert package_match is not None
            package = package_match.group(1).strip()
            group_id = PACKAGE_GROUPS[package]
            type(self).seen_packages.append(package)
            type(self).active += 1
            type(self).peak_active = max(type(self).peak_active, type(self).active)
            try:
                await asyncio.sleep(0.08)
                content = {
                    "group_id": group_id,
                    "fields": [
                        {
                            "field_id": f"{group_id}_test_field",
                            "status": "missing",
                            "value": None,
                            "evidence": [],
                            "warnings": [],
                        }
                    ],
                    "missing_field_ids": [f"{group_id}_test_field"],
                    "warnings": [],
                }
                yield TextChunk(delta=json.dumps(content, ensure_ascii=False))
            finally:
                type(self).active -= 1

        return gen()


async def seed_llm_config() -> None:
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


def smart_fill_input() -> dict:
    return {
        "document_ids": ["fixture-doc-001"],
        "max_concurrency": 5,
        "failure_policy": "continue",
        "retry_per_item": 0,
        "items": [
            {
                "id": group_id,
                "group_id": group_id,
                "skill_package": package,
                "field_ids": [f"{group_id}_test_field"],
            }
            for group_id, package in GROUP_PACKAGES.items()
        ],
    }


def test_smart_fill_task_runs_five_packages_in_parallel(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'smart-fill-task.db'}")
        reset_engine_for_test()
        ParallelSmartFillAgent.active = 0
        ParallelSmartFillAgent.peak_active = 0
        ParallelSmartFillAgent.seen_packages = []
        ParallelSmartFillAgent.seen_tool_names = set()
        monkeypatch.setattr(runner_mod, "ReactAgent", ParallelSmartFillAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        await init_db()
        await seed_llm_config()
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        headers = {
            "X-User-Id": "default_user",
            "X-Tenant-Id": DEFAULT_TENANT_ID,
        }
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            definitions = await client.get("/task-manager/definitions", headers=headers)
            assert definitions.status_code == 200
            definition = next(
                item
                for item in definitions.json()["definitions"]
                if item["task_type"] == "form.smart_fill.extract"
            )
            assert definition["handler"] == "batch_item_scheduler"
            assert definition["default_agent_id"] == "default-single-agent"
            assert definition["item_output_schema_name"] == "smart_fill_group_output"

            response = await client.post(
                "/task-manager/run",
                headers=headers,
                json={
                    "task_type": "form.smart_fill.extract",
                    "title": "Smart fill five-group orchestration test",
                    "user_id": "default_user",
                    "stream": False,
                    "input_payload": smart_fill_input(),
                },
            )
            assert response.status_code == 200, response.text
            task = response.json()["task"]
            assert task["status"] == "succeeded"
            assert task["handler_name"] == "batch_item_scheduler"
            assert task["progress_current"] == task["progress_total"] == 5
            assert task["result_payload_json"]["structured"]["summary"] == {
                "total": 5,
                "succeeded": 5,
                "failed": 0,
                "skipped": 0,
            }

            items_response = await client.get(
                f"/task-manager/tasks/{task['id']}/items",
                headers=headers,
            )
            assert items_response.status_code == 200
            items = items_response.json()["items"]
            assert [item["item_key"] for item in items] == list(GROUP_PACKAGES)
            assert all(item["status"] == "succeeded" for item in items)
            assert {
                item["input_payload_json"]["skill_package"] for item in items
            } == set(GROUP_PACKAGES.values())

            events_response = await client.get(
                f"/task-manager/tasks/{task['id']}/events",
                headers=headers,
            )
            events = events_response.json()["events"]
            started_packages = {
                event["payload_json"]["skill_package"]
                for event in events
                if event["event_type"] == "item_started"
            }
            assert started_packages == set(GROUP_PACKAGES.values())

        assert set(ParallelSmartFillAgent.seen_packages) == set(GROUP_PACKAGES.values())
        assert "search-knowledgebase-smartfilld" in ParallelSmartFillAgent.seen_tool_names
        assert "fetch-smartfil" in ParallelSmartFillAgent.seen_tool_names
        assert ParallelSmartFillAgent.peak_active >= 2

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_smart_fill_rejects_wrong_or_incomplete_package_mapping(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'smart-fill-invalid.db'}")
        reset_engine_for_test()
        await init_db()
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        headers = {
            "X-User-Id": "default_user",
            "X-Tenant-Id": DEFAULT_TENANT_ID,
        }
        payload = smart_fill_input()
        payload["items"][0]["skill_package"] = "smart-fill-financial-package"

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "form.smart_fill.extract",
                    "title": "Invalid smart fill mapping",
                    "input_payload": payload,
                },
            )

        assert response.status_code == 400
        assert "must use skill package" in response.json()["detail"]

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
