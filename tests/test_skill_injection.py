import asyncio

import httpx

from api.single_agent_api import create_app
from db.db_context import create_db_session, init_db, reset_engine_for_test
from skill import (
    SkillManager,
    ensure_default_skill_packages,
    get_skill_packages_by_names,
    upsert_skill_package,
)


def test_skill_manager_builds_ai_search_context(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'skill-manager.db'}",
        )
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            await ensure_default_skill_packages(session)

        context = await SkillManager().create_context("ai-search-package")

        assert "# AI Search" in context.task_prompt
        assert [tool.metadata.name for tool in context.tools] == []
        active = context.skills["active_package"]
        assert active["package_name"] == "ai-search-package"
        assert active["primary"]["name"] == "ai-search"

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_skill_manager_empty_disabled_and_unknown_packages(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'skill-disabled.db'}",
        )
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            await ensure_default_skill_packages(session)
            package = (
                await get_skill_packages_by_names(session, ["ai-search-package"])
            )[0]
            package.enabled = False
            session.add(package)
            await session.commit()

        assert (await SkillManager().create_context(None)).task_prompt == ""
        assert (
            await SkillManager().create_context("ai-search-package")
        ).task_prompt == ""

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


def test_skill_manager_rejects_package_with_missing_skill(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'skill-broken.db'}",
        )
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


def test_api_lists_available_skill_packages(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'skill-list.db'}",
        )
        reset_engine_for_test()
        await init_db()
        app = create_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get("/single-agent/skill-packages")

        assert response.status_code == 200
        packages = {
            item["package_name"]: item
            for item in response.json()["skill_packages"]
        }
        assert packages["pipeline-demo-package"]["primary_skill"] == "pipeline-demo"
        assert packages["ai-search-package"]["primary_skill"] == "ai-search"
        assert (
            packages["task-memory-compression-package"]["primary_skill"]
            == "task-memory-compression"
        )

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
