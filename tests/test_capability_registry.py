import asyncio
import json

import httpx
from sqlmodel import select

from backend.local_code_chat_app import create_app
from capability_registry import list_capabilities
from common.encrypt_utils import encrypt_key
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db, reset_engine_for_test
from tool.registry import ToolConfigEntity


def test_capability_catalog_lists_tools_and_skill_packages(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            session.add_all(
                [
                    ToolConfigEntity(
                    tenant_id=DEFAULT_TENANT_ID,
                    tool_name="web_search",
                    provider="aliyun",
                    enabled=True,
                    config_json='{"search_count": 3}',
                    encrypted_secrets_json=encrypt_key('{"api_key": "secret-iqs-key"}'),
                    ),
                    ToolConfigEntity(
                        tenant_id=DEFAULT_TENANT_ID,
                        tool_name="media_master_library",
                        provider="media_military_http",
                        enabled=True,
                    ),
                ]
            )
            await session.flush()
            capabilities = await list_capabilities(session)
            retired = (
                await session.exec(
                    select(ToolConfigEntity).where(
                        ToolConfigEntity.tool_name == "media_master_library"
                    )
                )
            ).first()

        by_key = {
            (
                item["capability_type"],
                item["name"],
                item.get("provider"),
            ): item
            for item in capabilities
        }

        assert ("tool", "code_interpreter", "local_python") in by_key
        assert ("tool", "code_interpreter", "limited_sandbox") not in by_key
        web_search = by_key[("tool", "web_search", "aliyun")]
        assert web_search["configured"] is True
        assert web_search["enabled"] is True
        assert web_search["display_name"] == "Aliyun IQS Web Search"
        assert web_search["llm_names"] == ["aliyun-websearch"]

        local_python = by_key[("tool", "code_interpreter", "local_python")]
        assert local_python["configured"] is False
        assert local_python["enabled"] is True

        memory_package = by_key[
            ("skill_package", "task-memory-compression-package", None)
        ]
        assert memory_package["display_name"] == "Task Memory Compression Package"
        assert memory_package["primary"] == "task-memory-compression"
        assert memory_package["auxiliary"] == []

        payload = json.dumps(capabilities, ensure_ascii=False)
        assert "secret-iqs-key" not in payload
        assert "encrypted_secrets_json" not in payload
        assert retired is None

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_capability_catalog_api_returns_unified_shape(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'catalog-api.db'}")
        reset_engine_for_test()
        await init_db()
        app = create_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/registry/capabilities")

        assert response.status_code == 200
        capabilities = response.json()["capabilities"]
        assert capabilities
        for item in capabilities:
            assert {
                "capability_type",
                "name",
                "display_name",
                "description",
                "enabled",
                "configured",
                "inner",
            }.issubset(item)
        assert any(item["capability_type"] == "tool" for item in capabilities)
        assert any(item["capability_type"] == "skill_package" for item in capabilities)

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
