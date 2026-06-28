"""Simple chat app with the local Python runtime tool enabled."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from backend.simple_chat_app import create_app as create_simple_chat_app
from db.db_context import create_db_session, init_db
from tool import ToolBundle
from tool.registry import (
    ToolProviderConfig,
    create_enabled_tool_bundle,
    get_default_tool_list,
)


LOCAL_PYTHON_ARTIFACT_DIR = (
    Path(__file__).resolve().parent / "tool" / "local_runtime" / "artifacts"
)


def create_app() -> FastAPI:
    async def tool_provider(_request):
        local_python_bundle = get_default_tool_list().create_bundle(
            ToolProviderConfig(
                tool_name="code_interpreter",
                provider="local_python",
                config={
                    "timeout_seconds": 20,
                    "max_output_chars": 50_000,
                    "work_dir": LOCAL_PYTHON_ARTIFACT_DIR,
                    "artifact_base_url": "/tool-artifacts/local-python",
                    "keep_work_dir": True,
                },
            )
        )

        async with create_db_session() as session:
            db_tool_bundle = await create_enabled_tool_bundle(session)

        return ToolBundle.combine([local_python_bundle, db_tool_bundle])

    @asynccontextmanager
    async def lifespan(_app):
        await init_db()
        yield

    return create_simple_chat_app(tool_provider=tool_provider, lifespan=lifespan)
