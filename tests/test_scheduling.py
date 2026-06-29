import asyncio

import httpx

from backend.local_code_chat_app import create_app
from common.encrypt_utils import encrypt_key
from common.llm.models import TextChunk
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db, reset_engine_for_test
from db.models.llm import LlmModelEntity
from scheduling.agent_registry import (
    ensure_default_agent_profiles,
    get_agent_profile,
    list_agent_profiles,
)
from service.cache.session_history_manager import session_history_manager
from service.thread.thread_service import ThreadService
import service.agent.single_agent_runner as runner_mod


class CapturingAgent:
    def __init__(self, llm, system_prompt, tools):
        self.system_prompt = system_prompt
        self.tools = tools

    async def run_async(self, state):
        async def gen():
            tool_names = ",".join(tool.metadata.name for tool in self.tools)
            has_report_skill = "# Report Generator Skill" in self.system_prompt
            yield TextChunk(delta=f"tools={tool_names}; report_skill_prompt={has_report_skill}")

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


async def _delete_thread(thread_id: str):
    async with create_db_session() as session:
        try:
            await ThreadService(session).delete_thread(thread_id)
        except ValueError:
            pass


def test_agent_registry_creates_default_profiles(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'registry.db'}")
        reset_engine_for_test()
        await init_db()

        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            profiles = await list_agent_profiles(session)
            report_agent = await get_agent_profile(session, "report-agent")

        assert {profile.agent_id for profile in profiles} >= {
            "default-single-agent",
            "report-agent",
            "all-capable-agent",
            "rag-agent",
            "code-agent",
        }
        assert report_agent is not None
        assert report_agent.default_skills[0] == "report-generator"
        assert "code_interpreter" in report_agent.default_tools

        all_capable_agent = await get_agent_profile(session, "all-capable-agent")
        assert all_capable_agent is not None
        assert all_capable_agent.default_tools == [
            "code_interpreter",
            "enabled_db_tools",
            "rag_retrieval",
        ]
        assert "report-generator" in all_capable_agent.default_skills
        assert all_capable_agent.runtime_config["tool_policy"] == "allowlist"

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_scheduling_chat_assembles_profile_tools_and_skills(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}")
        reset_engine_for_test()
        monkeypatch.setattr(runner_mod, "ReactAgent", CapturingAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        await init_db()
        await _seed_llm_config()

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            agents = await client.get("/scheduling/agents")
            assert agents.status_code == 200
            assert any(
                agent["agent_id"] == "report-agent"
                for agent in agents.json()["agents"]
            )

            response = await client.post(
                "/scheduling/agents/report-agent/chat",
                json={
                    "message": "write a report",
                    "user_id": "scheduling-test-user",
                    "stream": False,
                },
            )

        assert response.status_code == 200
        body = response.json()
        content = body["response"]["choices"][0]["message"]["content"]
        assert body["agent"]["agent_id"] == "report-agent"
        assert body["skills"]["primary"]["name"] == "report-generator"
        assert "LimitedLocalPythonInterpreter" in content
        assert "ReadSkill" in content
        assert "report_skill_prompt=True" in content

        await session_history_manager.clear_history(
            "scheduling-test-user",
            body["thread_id"],
        )
        await _delete_thread(body["thread_id"])

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
