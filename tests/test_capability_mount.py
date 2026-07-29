import asyncio
import json
from pathlib import Path

import httpx
import pytest
from sqlmodel import func, select

from backend.local_code_chat_app import create_app
from capability_mount import CapabilitySettings, mount_capability_entry
from common.encrypt_utils import encrypt_key
from common.llm.models import TextChunk
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db, reset_engine_for_test
from db.models.llm import LlmModelEntity
from scheduling.agent_registry import get_agent_profile
import service.agent.single_agent_runner as runner_mod
from skill import SkillManager
from skill.package_models import SkillPackageEntity
from task_manager import get_task_definition
from tool.registry import ToolConfigEntity, ToolManager


class _CapturingAgent:
    last_user_message = ""
    last_system_prompt = ""

    def __init__(self, llm, system_prompt, tools):
        self.system_prompt = system_prompt
        self.tools = tools

    async def run_async(self, state):
        type(self).last_user_message = str(state.messages[-1]["content"])
        type(self).last_system_prompt = self.system_prompt

        async def generate():
            names = ",".join(tool.metadata.name for tool in self.tools)
            has_skill = "# Mounted Policy QA" in self.system_prompt
            search = next(
                tool for tool in self.tools if tool.metadata.name == "mounted_search"
            )
            search_result = await search.acall(query="approval", top_k=2)
            yield TextChunk(
                delta=f"tools={names};skill={has_skill};search={search_result}"
            )

        return generate()


def _write_capability_entry(root: Path) -> Path:
    skill_dir = root / "skills" / "mounted-policy-qa"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: mounted-policy-qa
description: Mounted policy QA test skill.
tags: [mounted, test]
---

# Mounted Policy QA

Always call mounted_search before answering.
""",
        encoding="utf-8",
    )
    entry = root / "register.py"
    entry.write_text(
        """from __future__ import annotations

from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field

CAPABILITY_ID = "mounted-test"

class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    top_k: int = Field(default=8, ge=1, le=20)

class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1)
    top_k: int = Field(default=8, ge=1, le=20)
    batch: BatchInput | None = None

class BatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = ""

async def register(registry, settings):
    registry.register_skill_root(Path(__file__).parent / "skills")
    registry.register_http_tool(
        tool_name="mounted_search",
        provider="mounted_http",
        display_name="Mounted Search",
        description="Search mounted policy evidence.",
        base_url=settings.require("MOUNTED_SERVICE_BASE_URL"),
        path="/v1/search",
        input_model=SearchInput,
        request_id_header="X-Request-Id",
    )
    registry.register_skill_package(
        package_name="mounted-policy-qa-package",
        display_name="Mounted Policy QA",
        primary_skill="mounted-policy-qa",
    )
    registry.register_agent(
        agent_id="mounted-policy-qa-agent",
        name="Mounted Policy QA Agent",
        system_prompt="Use the mounted policy skill.",
        default_tools=["mounted_search", "code_interpreter"],
    )
    registry.register_task(
        task_type="mounted.policy.qa",
        name="Mounted Policy QA",
        default_agent_id="mounted-policy-qa-agent",
        default_skill_package="mounted-policy-qa-package",
        default_primary_skill="mounted-policy-qa",
        default_tools=["mounted_search", "code_interpreter"],
        input_model=TaskInput,
        conversation_message_field="question",
    )
""",
        encoding="utf-8",
    )
    return entry


async def _seed_llm_config(*, tenant_id: str = DEFAULT_TENANT_ID) -> None:
    async with create_db_session() as session:
        session.add(
            LlmModelEntity(
                tenant_id=tenant_id,
                base_url="http://example.test/v1",
                model="deepseek-v4-pro",
                model_name="deepseek-v4-pro",
                model_id="deepseek-v4-pro",
                encrypted_api_key=encrypt_key("test-key"),
                provider_name="openai_like",
                source="openai_like",
            )
        )
        await session.commit()


def test_mounted_capability_registers_and_runs_through_task_scheduler(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'mounted.db'}")
        reset_engine_for_test()
        await init_db()
        await _seed_llm_config()
        entry = _write_capability_entry(tmp_path / "capability")
        settings = CapabilitySettings(
            {"MOUNTED_SERVICE_BASE_URL": "http://mounted-service.test"}
        )

        first = await mount_capability_entry(entry, settings=settings)
        second = await mount_capability_entry(entry, settings=settings)

        assert first == second
        assert first["tasks"] == ["mounted.policy.qa"]
        definition = get_task_definition("mounted.policy.qa")
        assert definition.handler == "scheduler"
        assert definition.default_agent_id == "mounted-policy-qa-agent"
        assert definition.default_tools == ["mounted_search", "code_interpreter"]
        assert definition.input_schema_name == "capability_mounted_test_mounted_policy_qa_input"
        assert definition.conversation_message_field == "question"

        async with create_db_session() as session:
            agent = await get_agent_profile(session, "mounted-policy-qa-agent")
            package_count = await session.exec(
                select(func.count()).select_from(SkillPackageEntity).where(
                    SkillPackageEntity.package_name == "mounted-policy-qa-package"
                )
            )
            tool_count = await session.exec(
                select(func.count()).select_from(ToolConfigEntity).where(
                    ToolConfigEntity.tool_name == "mounted_search"
                )
            )
        assert agent is not None
        assert agent.default_tools == ["mounted_search", "code_interpreter"]
        assert agent.runtime_config["_capability_id"] == "mounted-test"
        assert package_count.one() == 1
        assert tool_count.one() == 1

        skill_context = await SkillManager().create_context("mounted-policy-qa-package")
        assert "# Mounted Policy QA" in skill_context.task_prompt
        tenant_skill_context = await SkillManager(tenant_id="mounted-tenant").create_context(
            "mounted-policy-qa-package"
        )
        assert "# Mounted Policy QA" in tenant_skill_context.task_prompt

        request_ids = []
        tenant_ids = []
        model_pack_ids = []

        async def fake_request(client, method, url, **kwargs):
            if url != "/v1/search":
                return await original_request(client, method, url, **kwargs)
            assert method == "POST"
            assert kwargs["json"] == {"query": "approval", "top_k": 2}
            request_id = kwargs["headers"]["X-Request-Id"]
            assert request_id.startswith("tool-") and len(request_id) == 37
            request_ids.append(request_id)
            tenant_ids.append(kwargs["headers"]["X-Tenant-ID"])
            model_pack_ids.append(kwargs["headers"].get("X-Model-Pack-ID"))
            request = httpx.Request(method, "http://mounted-service.test/v1/search")
            return httpx.Response(
                200,
                request=request,
                json={"success": True, "data": {"results": [{"clause_ordinal": 2}]}},
            )

        original_request = httpx.AsyncClient.request
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        manager = ToolManager(
            local_python_work_dir=tmp_path / "code-runs",
        )
        bundle = await manager.create_bundle(["mounted_search"])
        assert [tool.metadata.name for tool in bundle.tools] == ["mounted_search"]
        output = await bundle.tools[0].acall(query="approval", top_k=2)
        assert json.loads(str(output))["data"]["results"][0]["clause_ordinal"] == 2
        await bundle.tools[0].acall(query="approval", top_k=2)
        assert len(request_ids) == 2
        assert request_ids[0] != request_ids[1]
        tenant_bundle = await ToolManager(
            local_python_work_dir=tmp_path / "tenant-code-runs",
            tenant_id="mounted-tenant",
            model_pack_id="local-rerank",
        ).create_bundle(["mounted_search"])
        assert [tool.metadata.name for tool in tenant_bundle.tools] == ["mounted_search"]
        await tenant_bundle.tools[0].acall(query="approval", top_k=2)
        assert tenant_ids == [DEFAULT_TENANT_ID, DEFAULT_TENANT_ID, "mounted-tenant"]
        assert model_pack_ids == [None, None, "local-rerank"]

        async def unavailable_request(client, method, url, **kwargs):
            request = httpx.Request(method, "http://mounted-service.test/v1/search")
            raise httpx.ConnectError("connection refused", request=request)

        monkeypatch.setattr(httpx.AsyncClient, "request", unavailable_request)
        with pytest.raises(RuntimeError, match="service is unavailable"):
            await bundle.tools[0].acall(query="approval", top_k=2)
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        monkeypatch.setattr(runner_mod, "ReactAgent", _CapturingAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())
        _CapturingAgent.last_user_message = ""
        _CapturingAgent.last_system_prompt = ""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        java_headers = {
            "X-User-Id": "mounted-user",
            "X-Tenant-Id": "mounted-tenant",
        }
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers=java_headers,
        ) as client:
            response = await client.post(
                "/task-manager/run",
                json={
                    "task_type": "mounted.policy.qa",
                    "model_pack_id": "local-rerank",
                    "stream": False,
                    "input_payload": {"question": "What is the approval rule?", "top_k": 2},
                },
            )

        assert response.status_code == 200, response.text
        task = response.json()["task"]
        assert task["status"] == "succeeded"
        assert task["tenant_id"] == "mounted-tenant"
        assert task["model_pack_id"] == "local-rerank"
        assert task["agent_id"] == "mounted-policy-qa-agent"
        content = task["result_payload_json"]["content"]
        assert "LimitedLocalPythonInterpreter" in content
        assert "mounted_search" in content
        assert "skill=True" in content
        assert _CapturingAgent.last_user_message == "What is the approval rule?"
        assert '"top_k": 2' in _CapturingAgent.last_system_prompt
        assert tenant_ids[-1] == "mounted-tenant"
        assert model_pack_ids[-1] == "local-rerank"
        assert "Execute task_type" not in _CapturingAgent.last_user_message
        assert "output_parse_failed" not in {
            event["event_type"] for event in response.json()["events"]
        }
        scheduler_event = next(
            event
            for event in response.json()["events"]
            if event["event_type"] == "scheduler_request_built"
        )
        assert scheduler_event["payload_json"]["model_id"] == "deepseek-v4-pro"

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers=java_headers,
        ) as client:
            conversations = await client.get(
                "/task-manager/conversations",
                params={"task_type": "mounted.policy.qa"},
            )
            assert conversations.status_code == 200, conversations.text
            summaries = conversations.json()["conversations"]
            assert len(summaries) == 1
            assert summaries[0]["thread_id"] == task["thread_id"]
            assert summaries[0]["title"] == "What is the approval rule?"

            detail = await client.get(
                f"/task-manager/conversations/{task['thread_id']}",
                params={"task_type": "mounted.policy.qa"},
            )
            assert detail.status_code == 200, detail.text
            assert [message["text"] for message in detail.json()["messages"]] == [
                "What is the approval rule?",
                content,
            ]

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_mount_rejects_missing_entry(tmp_path):
    async def run():
        try:
            await mount_capability_entry(tmp_path / "missing.py")
        except ValueError as exc:
            assert "does not exist" in str(exc)
        else:
            raise AssertionError("missing capability entry should fail")

    asyncio.run(run())
