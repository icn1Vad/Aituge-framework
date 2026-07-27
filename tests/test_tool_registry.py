from pathlib import Path
import asyncio

import pytest
from llama_index.core.tools.function_tool import FunctionTool

from common.encrypt_utils import encrypt_key
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db, reset_engine_for_test
from tool import ToolBundle
from tool.registry import (
    ToolDefinition,
    ToolConfigEntity,
    ToolManager,
    ToolProviderConfig,
    ToolList,
    get_default_tool_list,
)


def test_default_tool_list_contains_current_tool_providers():
    tool_list = get_default_tool_list()
    entries = {(entry.tool_name, entry.provider) for entry in tool_list.list()}

    assert ("code_interpreter", "local_python") in entries
    assert ("web_search", "aliyun") in entries
    assert ("media_master_library", "media_military_http") in entries
    assert ("code_interpreter", "limited_sandbox") not in entries


def test_default_tool_list_exposes_llm_tool_names():
    tool_list = get_default_tool_list()

    local_python = tool_list.get("code_interpreter", "local_python")

    assert local_python.llm_tool_names == ("LimitedLocalPythonInterpreter",)
    assert tool_list.get("web_search", "aliyun").llm_tool_names == (
        "aliyun-websearch",
    )
    assert tool_list.get(
        "media_master_library", "media_military_http"
    ).llm_tool_names == (
        "media_get_master_library_manifest",
        "media_search_master_library",
        "media_fetch_master_library_item",
        "media_recommend_templates_for_strategy",
        "media_get_random_script_type_candidates",
        "media_get_script_examples_by_strategy",
    )


def test_tool_list_builds_media_master_library_bundle():
    bundle = get_default_tool_list().create_bundle(
        ToolProviderConfig(
            tool_name="media_master_library",
            provider="media_military_http",
            config={"base_url": "http://127.0.0.1:8010"},
        )
    )

    assert [tool.metadata.name for tool in bundle.tools] == [
        "media_get_master_library_manifest",
        "media_search_master_library",
        "media_fetch_master_library_item",
        "media_recommend_templates_for_strategy",
        "media_get_random_script_type_candidates",
        "media_get_script_examples_by_strategy",
    ]


def test_tool_list_builds_local_python_bundle(tmp_path: Path):
    bundle = get_default_tool_list().create_bundle(
        ToolProviderConfig(
            tool_name="code_interpreter",
            provider="local_python",
            config={"work_dir": tmp_path, "keep_work_dir": True},
        )
    )

    assert [tool.metadata.name for tool in bundle.tools] == [
        "LimitedLocalPythonInterpreter"
    ]
    assert len(bundle.cleanup_hooks) == 1


def test_tool_list_returns_empty_bundle_when_disabled():
    bundle = get_default_tool_list().create_bundle(
        ToolProviderConfig(
            tool_name="code_interpreter",
            provider="local_python",
            enabled=False,
        )
    )

    assert bundle.tools == []
    assert bundle.cleanup_hooks == []


def test_tool_list_raises_for_unknown_provider():
    tool_list = ToolList()

    with pytest.raises(KeyError):
        tool_list.get("web_search", "aliyun")


def test_tool_config_entity_to_provider_config_decrypts_secrets():
    entity = ToolConfigEntity(
        tool_name="web_search",
        provider="aliyun",
        config_json='{"endpoint": "https://cloud-iqs.aliyuncs.com/search/unified", "search_count": 5}',
        encrypted_secrets_json=encrypt_key('{"api_key": "iqs-api-key"}'),
    )

    config = entity.to_provider_config()

    assert config.tool_name == "web_search"
    assert config.provider == "aliyun"
    assert config.config["search_count"] == 5
    assert config.secrets == {"api_key": "iqs-api-key"}


def _fake_tool_bundle(config: ToolProviderConfig) -> ToolBundle:
    async def afake_tool(query: str = "") -> str:
        return f"fake:{query}:{config.config.get('label', '')}"

    return ToolBundle.from_tools(
        [
            FunctionTool.from_defaults(
                async_fn=afake_tool,
                name="FakeDbTool",
                description="Fake DB-backed tool for registry tests.",
            )
        ]
    )


def _fake_tool_list() -> ToolList:
    tool_list = ToolList(get_default_tool_list().list())
    tool_list.register(
        ToolDefinition(
            tool_name="fake_db_tool",
            provider="fake",
            display_name="Fake DB Tool",
            description="Fake DB-backed provider.",
            llm_tool_names=("FakeDbTool",),
            factory=_fake_tool_bundle,
        ),
        make_default=True,
    )
    return tool_list


def test_tool_manager_builds_local_python_bundle(tmp_path: Path):
    async def run():
        manager = ToolManager(
            local_python_work_dir=tmp_path,
        )
        bundle = await manager.create_bundle(["code_interpreter"])

        assert [tool.metadata.name for tool in bundle.tools] == [
            "LimitedLocalPythonInterpreter"
        ]
        assert len(bundle.cleanup_hooks) == 1
        await bundle.cleanup()

    asyncio.run(run())


def test_tool_manager_loads_all_enabled_db_tools(tmp_path: Path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'tools-all.db'}")
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            session.add(
                ToolConfigEntity(
                    tenant_id=DEFAULT_TENANT_ID,
                    tool_name="fake_db_tool",
                    provider="fake",
                    enabled=True,
                    config_json='{"label": "all"}',
                )
            )
            await session.flush()
            manager = ToolManager(
                local_python_work_dir=tmp_path,
                tool_list=_fake_tool_list(),
            )
            bundle = await manager.create_bundle(["enabled_db_tools"], session=session)

        assert [tool.metadata.name for tool in bundle.tools] == ["FakeDbTool"]

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_tool_manager_loads_named_db_tool(tmp_path: Path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'tools-named.db'}")
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            session.add(
                ToolConfigEntity(
                    tenant_id=DEFAULT_TENANT_ID,
                    tool_name="fake_db_tool",
                    provider="fake",
                    enabled=True,
                    config_json='{"label": "named"}',
                )
            )
            await session.flush()
            manager = ToolManager(
                local_python_work_dir=tmp_path,
                tool_list=_fake_tool_list(),
            )
            bundle = await manager.create_bundle(["fake_db_tool"], session=session)

        assert [tool.metadata.name for tool in bundle.tools] == ["FakeDbTool"]

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_tool_manager_skips_unknown_unconfigured_tool(tmp_path: Path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'tools-unknown.db'}")
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            manager = ToolManager(
                local_python_work_dir=tmp_path,
                tool_list=_fake_tool_list(),
            )
            bundle = await manager.create_bundle(["not_configured_yet"], session=session)

        assert bundle.tools == []
        assert bundle.cleanup_hooks == []

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_tenant_disabled_tool_does_not_fall_back_to_default(tmp_path: Path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'tools-disabled.db'}")
        reset_engine_for_test()
        await init_db()
        async with create_db_session() as session:
            session.add(
                ToolConfigEntity(
                    tenant_id=DEFAULT_TENANT_ID,
                    tool_name="fake_db_tool",
                    provider="fake",
                    enabled=True,
                    config_json='{"label": "global"}',
                )
            )
            session.add(
                ToolConfigEntity(
                    tenant_id="tenant-disabled",
                    tool_name="fake_db_tool",
                    provider="fake",
                    enabled=False,
                    config_json='{"label": "disabled"}',
                )
            )
            await session.flush()
            bundle = await ToolManager(
                local_python_work_dir=tmp_path,
                tenant_id="tenant-disabled",
                tool_list=_fake_tool_list(),
            ).create_bundle(["fake_db_tool"], session=session)

        assert bundle.tools == []

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
