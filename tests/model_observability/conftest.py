from __future__ import annotations

from contextlib import asynccontextmanager

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_observability.entities import MODEL_OBSERVABILITY_METADATA


@pytest_asyncio.fixture
async def isolated_model_store(tmp_path):
    database = tmp_path / "model-observability.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.run_sync(MODEL_OBSERVABILITY_METADATA.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def session_context():
        session = factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    yield session_context
    await engine.dispose()
