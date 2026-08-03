import asyncio
import os
from pathlib import Path
import sys

import pytest
from sqlmodel import select


ROOT_DIR = Path(__file__).resolve().parent
SINGLE_AGENT_DIR = ROOT_DIR / "backend" / "single-agent"
TEST_DB_PATH = ROOT_DIR / ".pytest_cache" / "single_agent_test.sqlite"
os.environ["SQLITE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_PATH}"

if str(SINGLE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(SINGLE_AGENT_DIR))

import db.db_context as db_context  # noqa: E402
from common.system_constants import DEFAULT_TENANT_ID  # noqa: E402
from db.models.llm import LlmModelEntity  # noqa: E402


async def _dispose_cached_engine() -> None:
    engine = db_context._engine
    if engine is not None:
        await engine.dispose()
    db_context.reset_engine_for_test()


async def _bootstrap_single_agent_db() -> None:
    await db_context.init_db()
    async with db_context.create_db_session() as session:
        statement = select(LlmModelEntity).where(
            LlmModelEntity.model_id == "deepseek-v4-flash",
            LlmModelEntity.tenant_id == DEFAULT_TENANT_ID,
        )
        existing = (await session.exec(statement)).first()
        if existing is not None:
            return

        session.add(
            LlmModelEntity(
                tenant_id=DEFAULT_TENANT_ID,
                base_url="http://llm.example.test/v1",
                model="deepseek-v4-flash",
                model_name="deepseek-v4-flash",
                model_id="deepseek-v4-flash",
                provider_name="openai_like",
                source="openai_like",
                encrypted_api_key=None,
            )
        )


@pytest.fixture(scope="session", autouse=True)
def bootstrap_single_agent_db():
    TEST_DB_PATH.unlink(missing_ok=True)
    asyncio.run(_bootstrap_single_agent_db())
    asyncio.run(_dispose_cached_engine())


@pytest.fixture(autouse=True)
def reset_single_agent_engine_after_test():
    yield
    asyncio.run(_dispose_cached_engine())
