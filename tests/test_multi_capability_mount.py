import asyncio
import json
from pathlib import Path

import pytest
from sqlmodel import select

from capability_mount import mount_capabilities_from_env
from db.db_context import create_db_session, init_db, reset_engine_for_test
from scheduling.agent_registry import get_agent_profile
from task_manager import get_task_definition
from tool.registry import ToolConfigEntity, get_default_tool_list


def _write_entry(
    root: Path,
    *,
    source_id: str,
    tool_name: str,
    agent_id: str,
    task_type: str,
) -> Path:
    root.mkdir(parents=True)
    entry = root / "register.py"
    entry.write_text(
        f'''from pydantic import BaseModel, ConfigDict, Field

CAPABILITY_ID = {source_id!r}

class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1)

async def register(registry, settings):
    registry.register_http_tool(
        tool_name={tool_name!r},
        provider={source_id + "_http"!r},
        display_name={tool_name!r},
        description="Multi-entry test tool.",
        base_url={"http://" + source_id + ".test"!r},
        path="/v1/echo",
        input_model=Input,
    )
    registry.register_agent(
        agent_id={agent_id!r},
        name={agent_id!r},
        default_tools=[{tool_name!r}],
    )
    registry.register_task(
        task_type={task_type!r},
        name={task_type!r},
        default_agent_id={agent_id!r},
        default_tools=[{tool_name!r}],
        input_model=Input,
        conversation_message_field="message",
    )
''',
        encoding="utf-8",
    )
    return entry


def test_plural_entries_mount_in_order_dedupe_and_remain_idempotent(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'multi.db'}")
        reset_engine_for_test()
        await init_db()
        first = _write_entry(
            tmp_path / "first",
            source_id="multi-first",
            tool_name="multi_first_echo",
            agent_id="multi-first-agent",
            task_type="multi.first.chat",
        )
        second = _write_entry(
            tmp_path / "second",
            source_id="multi-second",
            tool_name="multi_second_echo",
            agent_id="multi-second-agent",
            task_type="multi.second.chat",
        )
        monkeypatch.setenv(
            "AITUGE_CAPABILITY_ENTRIES",
            json.dumps([str(first), str(first), str(second)]),
        )
        monkeypatch.setenv("AITUGE_CAPABILITY_ENTRY", str(tmp_path / "ignored.py"))

        mounted = await mount_capabilities_from_env()
        mounted_again = await mount_capabilities_from_env()

        assert [item["source_id"] for item in mounted] == ["multi-first", "multi-second"]
        assert mounted_again == mounted
        assert get_task_definition("multi.first.chat").default_agent_id == "multi-first-agent"
        assert get_task_definition("multi.second.chat").default_agent_id == "multi-second-agent"
        assert get_default_tool_list().get("multi_first_echo").provider == "multi-first_http"
        assert get_default_tool_list().get("multi_second_echo").provider == "multi-second_http"

        async with create_db_session() as session:
            configs = list(
                (
                    await session.exec(
                        select(ToolConfigEntity).where(
                            ToolConfigEntity.tool_name.in_(
                                ["multi_first_echo", "multi_second_echo"]
                            )
                        )
                    )
                ).all()
            )
            first_agent = await get_agent_profile(session, "multi-first-agent")
            second_agent = await get_agent_profile(session, "multi-second-agent")
        assert {(item.tool_name, item.provider) for item in configs} == {
            ("multi_first_echo", "multi-first_http"),
            ("multi_second_echo", "multi-second_http"),
        }
        assert first_agent is not None
        assert second_agent is not None

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_plural_entries_fall_back_to_legacy_single_entry(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
        reset_engine_for_test()
        await init_db()
        entry = _write_entry(
            tmp_path / "legacy",
            source_id="multi-legacy",
            tool_name="multi_legacy_echo",
            agent_id="multi-legacy-agent",
            task_type="multi.legacy.chat",
        )
        monkeypatch.delenv("AITUGE_CAPABILITY_ENTRIES", raising=False)
        monkeypatch.setenv("AITUGE_CAPABILITY_ENTRY", str(entry))

        mounted = await mount_capabilities_from_env()

        assert [item["source_id"] for item in mounted] == ["multi-legacy"]

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not-json", "must be a JSON array"),
        ('{"entry": "register.py"}', "must be a JSON array"),
        ('[""]', "must be a non-empty string path"),
        ("[3]", "must be a non-empty string path"),
    ],
)
def test_plural_entries_reject_invalid_configuration(monkeypatch, raw, message):
    monkeypatch.setenv("AITUGE_CAPABILITY_ENTRIES", raw)
    with pytest.raises(ValueError, match=message):
        asyncio.run(mount_capabilities_from_env())


def test_plural_entries_reject_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AITUGE_CAPABILITY_ENTRIES",
        json.dumps([str(tmp_path / "missing.py")]),
    )
    with pytest.raises(ValueError, match="does not exist"):
        asyncio.run(mount_capabilities_from_env())


def test_plural_entries_reject_cross_capability_tool_name_collision(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'collision.db'}")
        reset_engine_for_test()
        await init_db()
        first = _write_entry(
            tmp_path / "collision-first",
            source_id="multi-collision-first",
            tool_name="multi_collision_echo",
            agent_id="multi-collision-first-agent",
            task_type="multi.collision.first",
        )
        second = _write_entry(
            tmp_path / "collision-second",
            source_id="multi-collision-second",
            tool_name="multi_collision_echo",
            agent_id="multi-collision-second-agent",
            task_type="multi.collision.second",
        )
        monkeypatch.setenv(
            "AITUGE_CAPABILITY_ENTRIES",
            json.dumps([str(first), str(second)]),
        )

        with pytest.raises(ValueError, match="already registered"):
            await mount_capabilities_from_env()

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
