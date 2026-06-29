import asyncio

import httpx

from api.single_agent_api import create_app
from common.llm.models import TextChunk
from db.db_context import create_db_session
import service.agent.single_agent_runner as runner_mod
from service.cache.session_history_manager import session_history_manager
from service.thread.thread_service import ThreadService
from skill import SkillBundle, load_skill


async def _delete_thread(thread_id: str):
    async with create_db_session() as session:
        try:
            await ThreadService(session).delete_thread(thread_id)
        except ValueError:
            pass


def test_api_accepts_task_owned_skill_bundle(monkeypatch):
    async def run():
        class CapturingAgent:
            def __init__(self, llm, system_prompt, tools):
                self.system_prompt = system_prompt

            async def run_async(self, state):
                async def gen():
                    assert "# Task Style Skill" in self.system_prompt
                    yield TextChunk(delta="task-style loaded")

                return gen()

        def skill_provider(_request):
            return SkillBundle.from_skills(primary=load_skill("task-style"))

        monkeypatch.setattr(runner_mod, "ReactAgent", CapturingAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        app = create_app(tool_provider=skill_provider)
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/single-agent/chat",
                json={
                    "message": "hello skill",
                    "user_id": "skill-provider-test-user",
                    "stream": False,
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["response"]["choices"][0]["message"]["content"] == (
                "task-style loaded"
            )

        await session_history_manager.clear_history(
            "skill-provider-test-user",
            body["thread_id"],
        )
        await _delete_thread(body["thread_id"])

    asyncio.run(run())


def test_api_loads_skills_from_request_fields(monkeypatch):
    async def run():
        class CapturingAgent:
            def __init__(self, llm, system_prompt, tools):
                self.system_prompt = system_prompt
                self.tools = tools

            async def run_async(self, state):
                async def gen():
                    assert "# Task Style Skill" in self.system_prompt
                    assert "review-style: Add a brief risk check" in self.system_prompt
                    assert [tool.metadata.name for tool in self.tools] == ["ReadSkill"]
                    yield TextChunk(delta="request skill fields loaded")

                return gen()

        monkeypatch.setattr(runner_mod, "ReactAgent", CapturingAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        app = create_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/single-agent/chat",
                json={
                    "message": "hello request skill",
                    "user_id": "skill-request-test-user",
                    "primary_skill": "task-style",
                    "candidate_skills": ["review-style"],
                    "stream": False,
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["response"]["choices"][0]["message"]["content"] == (
                "request skill fields loaded"
            )
            assert body["skills"]["primary"]["name"] == "task-style"
            assert body["skills"]["candidates"][0]["name"] == "review-style"

        await session_history_manager.clear_history(
            "skill-request-test-user",
            body["thread_id"],
        )
        await _delete_thread(body["thread_id"])

    asyncio.run(run())


def test_api_stream_metadata_includes_request_skills(monkeypatch):
    async def run():
        class CapturingAgent:
            def __init__(self, llm, system_prompt, tools):
                self.system_prompt = system_prompt

            async def run_async(self, state):
                async def gen():
                    yield TextChunk(delta="stream skill fields loaded")

                return gen()

        monkeypatch.setattr(runner_mod, "ReactAgent", CapturingAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        thread_id = None

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/single-agent/chat",
                json={
                    "message": "hello stream skill",
                    "user_id": "skill-stream-test-user",
                    "primary_skill": "task-style",
                    "candidate_skills": ["review-style"],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert '"skills"' in response.text
        assert '"primary":{"name":"task-style"' in response.text
        assert '"candidates":[{"name":"review-style"' in response.text
        for line in response.text.splitlines():
            if line.startswith("data: ") and '"event":"metadata"' in line:
                thread_id = line.split('"thread_id":"', 1)[1].split('"', 1)[0]
                break

        if thread_id:
            await session_history_manager.clear_history("skill-stream-test-user", thread_id)
            await _delete_thread(thread_id)

    asyncio.run(run())


def test_api_lists_available_skills():
    async def run():
        app = create_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/single-agent/skills")

        assert response.status_code == 200
        names = {skill["name"] for skill in response.json()["skills"]}
        assert {
            "implementation-plan",
            "debugging-checklist",
            "concise-summary",
            "report-generator",
            "report-executive-summary",
        }.issubset(names)

    asyncio.run(run())


def test_api_injects_different_skill_types_from_request(monkeypatch):
    async def run():
        class CapturingAgent:
            def __init__(self, llm, system_prompt, tools):
                self.system_prompt = system_prompt
                self.tools = tools

            async def run_async(self, state):
                async def gen():
                    assert "# Debugging Checklist Skill" in self.system_prompt
                    assert "implementation-plan: Structure coding tasks" in self.system_prompt
                    assert "concise-summary: Summarize results" in self.system_prompt
                    assert [tool.metadata.name for tool in self.tools] == ["ReadSkill"]
                    yield TextChunk(delta="debug skill package loaded")

                return gen()

        monkeypatch.setattr(runner_mod, "ReactAgent", CapturingAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        app = create_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/single-agent/chat",
                json={
                    "message": "debug this",
                    "user_id": "skill-types-test-user",
                    "primary_skill": "debugging-checklist",
                    "candidate_skills": ["implementation-plan", "concise-summary"],
                    "stream": False,
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["response"]["choices"][0]["message"]["content"] == (
                "debug skill package loaded"
            )

        await session_history_manager.clear_history(
            "skill-types-test-user",
            body["thread_id"],
        )
        await _delete_thread(body["thread_id"])

    asyncio.run(run())


def test_api_injects_report_generator_skill_package(monkeypatch):
    async def run():
        class CapturingAgent:
            def __init__(self, llm, system_prompt, tools):
                self.system_prompt = system_prompt
                self.tools = tools

            async def run_async(self, state):
                async def gen():
                    assert "# Report Generator Skill" in self.system_prompt
                    assert "report-executive-summary: Write the report opening" in self.system_prompt
                    assert "report-analysis-findings: Turn evidence" in self.system_prompt
                    assert "report-risk-actions: Close a report" in self.system_prompt
                    assert [tool.metadata.name for tool in self.tools] == ["ReadSkill"]
                    yield TextChunk(delta="report skill package loaded")

                return gen()

        monkeypatch.setattr(runner_mod, "ReactAgent", CapturingAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        app = create_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/single-agent/chat",
                json={
                    "message": "write a complete report",
                    "user_id": "report-skill-test-user",
                    "primary_skill": "report-generator",
                    "candidate_skills": [
                        "report-executive-summary",
                        "report-context-scope",
                        "report-analysis-findings",
                        "report-quantitative-calculation",
                        "report-chart-figure",
                        "report-code-verification",
                        "report-risk-actions",
                    ],
                    "stream": False,
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["response"]["choices"][0]["message"]["content"] == (
                "report skill package loaded"
            )

        await session_history_manager.clear_history(
            "report-skill-test-user",
            body["thread_id"],
        )
        await _delete_thread(body["thread_id"])

    asyncio.run(run())
