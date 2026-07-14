import asyncio

import httpx

from api.single_agent_api import create_app
from common.llm.models import TextChunk
from db.db_context import create_db_session, init_db, reset_engine_for_test
import service.agent.single_agent_runner as runner_mod
from service.cache.session_history_manager import session_history_manager
from service.thread.thread_service import ThreadService
from skill import (
    SkillManager,
    ensure_default_skill_packages,
    get_skill_packages_by_names,
    upsert_skill_package,
)


async def _delete_thread(thread_id: str):
    async with create_db_session() as session:
        try:
            await ThreadService(session).delete_thread(thread_id)
        except ValueError:
            pass


def test_skill_manager_builds_report_package_context(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'skill-manager.db'}")
        reset_engine_for_test()
        await init_db()

        context = await SkillManager().create_context("report-package")

        assert "# Report Generator Skill" in context.task_prompt
        assert "report-analysis-findings: Turn evidence" in context.task_prompt
        assert [tool.metadata.name for tool in context.tools] == ["ReadSkill"]
        active_package = context.skills["active_package"]
        assert active_package["package_name"] == "report-package"
        assert active_package["primary"]["name"] == "report-generator"
        assert active_package["auxiliary_index"][0]["name"] == "report-context-scope"

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_skill_manager_builds_main_agent_and_managed_agent_packages(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'main-skills.db'}")
        reset_engine_for_test()
        await init_db()

        main = await SkillManager().create_context("main-agent-orchestration-package")
        consult = await SkillManager().create_context("media-writer-consult-package")
        delegate = await SkillManager().create_context("media-storyboard-delegate-package")

        assert main.skills["active_package"]["primary"]["name"] == (
            "main-agent-orchestration"
        )
        assert [
            item["name"]
            for item in main.skills["active_package"]["auxiliary_index"]
        ] == ["media-script-writer", "workspace-storyboard-editor"]
        assert [tool.metadata.name for tool in main.tools] == ["ReadSkill"]
        assert consult.skills["active_package"]["primary"]["name"] == (
            "managed-agent-consult"
        )
        assert consult.skills["active_package"]["auxiliary_index"][0]["name"] == (
            "media-script-writer"
        )
        assert delegate.skills["active_package"]["primary"]["name"] == (
            "managed-agent-delegate"
        )
        assert delegate.skills["active_package"]["auxiliary_index"][0]["name"] == (
            "workspace-storyboard-editor"
        )

        script_task = await SkillManager().create_context("media-script-main-agent-package")
        assert script_task.skills["active_package"]["primary"]["name"] == (
            "media-script-task-orchestration"
        )
        assert [
            item["name"]
            for item in script_task.skills["active_package"]["auxiliary_index"]
        ] == [
            "main-agent-orchestration",
            "media-script-writer",
            "workspace-storyboard-editor",
        ]

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_skill_manager_empty_and_disabled_packages(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'skill-disabled.db'}")
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            await ensure_default_skill_packages(session)
            packages = await get_skill_packages_by_names(session, ["report-package"])
            packages[0].enabled = False
            session.add(packages[0])

        empty = await SkillManager().create_context(None)
        blank = await SkillManager().create_context("")
        disabled = await SkillManager().create_context("report-package")

        assert empty.tools == []
        assert empty.task_prompt == ""
        assert empty.skills == {}
        assert blank.tools == []
        assert blank.task_prompt == ""
        assert blank.skills == {}
        assert disabled.tools == []
        assert disabled.task_prompt == ""
        assert disabled.skills == {}

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_default_skill_packages_backfill_legacy_writer_skill_names(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'skill-backfill.db'}")
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            await ensure_default_skill_packages(session)
            packages = await get_skill_packages_by_names(
                session,
                ["main-agent-orchestration-package", "media-writer-delegate-package"],
            )
            packages[0].auxiliary_skills_json = (
                '["workspace-script-editor", "workspace-storyboard-editor"]'
            )
            packages[1].auxiliary_skills_json = '["workspace-script-editor"]'
            session.add(packages[0])
            session.add(packages[1])
            await session.commit()

            await ensure_default_skill_packages(session)
            await session.refresh(packages[0])
            await session.refresh(packages[1])

        assert packages[0].auxiliary_skills == [
            "media-script-writer",
            "workspace-storyboard-editor",
        ]
        assert packages[1].auxiliary_skills == ["media-script-writer"]

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_skill_manager_unknown_package_errors(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'skill-missing.db'}")
        reset_engine_for_test()
        await init_db()
        try:
            await SkillManager().create_context("missing-package")
        except ValueError as exc:
            assert "missing-package" in str(exc)
        else:
            raise AssertionError("missing skill package should raise")

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_skill_manager_package_with_missing_skill_errors(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'skill-broken.db'}")
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            await upsert_skill_package(
                session,
                package_name="broken-package",
                primary_skill="not-a-real-skill",
            )
        try:
            await SkillManager().create_context("broken-package")
        except ValueError as exc:
            assert "not-a-real-skill" in str(exc)
        else:
            raise AssertionError("package with missing skill should raise")

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_api_loads_skill_package_from_request(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'skill-api.db'}")
        reset_engine_for_test()
        await init_db()

        class CapturingAgent:
            def __init__(self, llm, system_prompt, tools):
                self.system_prompt = system_prompt
                self.tools = tools

            async def run_async(self, state):
                async def gen():
                    assert "# Report Generator Skill" in self.system_prompt
                    assert "report-executive-summary: Write the report opening" in self.system_prompt
                    assert "report-analysis-findings: Turn evidence" in self.system_prompt
                    assert [tool.metadata.name for tool in self.tools] == ["ReadSkill"]
                    yield TextChunk(delta="request skill package loaded")

                return gen()

        class FakeLlmRuntime:
            def __init__(self, *args, **kwargs):
                pass

            async def get_llm(self, model_id):
                return object()

        monkeypatch.setattr(runner_mod, "ReactAgent", CapturingAgent)
        monkeypatch.setattr(runner_mod, "LlmRuntime", FakeLlmRuntime)

        app = create_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/single-agent/chat",
                json={
                    "message": "hello request skill package",
                    "user_id": "skill-package-request-test-user",
                    "skill_package": "report-package",
                    "stream": False,
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["response"]["choices"][0]["message"]["content"] == (
                "request skill package loaded"
            )
            assert body["skills"]["active_package"]["package_name"] == "report-package"

        await session_history_manager.clear_history(
            "skill-package-request-test-user",
            body["thread_id"],
        )
        await _delete_thread(body["thread_id"])

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_api_stream_metadata_includes_skill_package(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'skill-stream.db'}")
        reset_engine_for_test()
        await init_db()

        class CapturingAgent:
            def __init__(self, llm, system_prompt, tools):
                self.system_prompt = system_prompt

            async def run_async(self, state):
                async def gen():
                    yield TextChunk(delta="stream skill package loaded")

                return gen()

        class FakeLlmRuntime:
            def __init__(self, *args, **kwargs):
                pass

            async def get_llm(self, model_id):
                return object()

        monkeypatch.setattr(runner_mod, "ReactAgent", CapturingAgent)
        monkeypatch.setattr(runner_mod, "LlmRuntime", FakeLlmRuntime)

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        thread_id = None

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/single-agent/chat",
                json={
                    "message": "hello stream skill package",
                    "user_id": "skill-package-stream-test-user",
                    "skill_package": "general-package",
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert '"skills"' in response.text
        assert '"active_package"' in response.text
        assert '"package_name":"general-package"' in response.text
        assert '"primary":{"name":"task-style"' in response.text
        for line in response.text.splitlines():
            if line.startswith("data: ") and '"event":"metadata"' in line:
                thread_id = line.split('"thread_id":"', 1)[1].split('"', 1)[0]
                break

        if thread_id:
            await session_history_manager.clear_history(
                "skill-package-stream-test-user",
                thread_id,
            )
            await _delete_thread(thread_id)

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_api_lists_available_skill_packages(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'skill-list.db'}")
        reset_engine_for_test()
        await init_db()
        app = create_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/single-agent/skill-packages")

        assert response.status_code == 200
        packages = {item["package_name"]: item for item in response.json()["skill_packages"]}
        assert packages["general-package"]["primary_skill"] == "task-style"
        assert packages["report-package"]["primary_skill"] == "report-generator"

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
