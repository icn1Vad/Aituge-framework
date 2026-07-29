from __future__ import annotations

import asyncio

import pytest
from sqlmodel import select

from db.db_context import create_db_session, init_db, reset_engine_for_test
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.idempotency import IdempotencyConflictError
from task_manager.models import TaskRunEntity
from task_manager.schemas import TaskCreateRequest, TaskRunRequest
from task_manager.service import TaskManagerService


async def _service(tmp_path, monkeypatch, name: str) -> TaskManagerService:
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv(
        "SQLITE_URL",
        f"sqlite+aiosqlite:///{tmp_path / name}",
    )
    monkeypatch.setenv("MODEL_PACK_ID", "api-rerank")
    reset_engine_for_test()
    await init_db()
    return TaskManagerService(
        SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    )


def _request(
    *,
    key: str,
    model_pack_id: str | None = None,
    user_id: str = "user-1",
) -> TaskCreateRequest:
    return TaskCreateRequest(
        task_type="pipeline.demo",
        idempotency_key=key,
        input_payload={"goal": "verify model pack routing"},
        user_id=user_id,
        tenant_id="tenant-1",
        model_pack_id=model_pack_id,
    )


def test_default_and_explicit_model_packs_are_frozen_on_task_and_run(
    tmp_path,
    monkeypatch,
) -> None:
    async def run() -> None:
        service = await _service(tmp_path, monkeypatch, "model-pack-freeze.db")
        monkeypatch.setattr(
            "task_manager.service.start_background_run",
            lambda run_id, coroutine: coroutine.close(),
        )

        default_task = await service.create_task(
            _request(key="default-pack"),
            service_name="ai-policy",
        )
        private_task = await service.create_task(
            _request(
                key="private-pack",
                model_pack_id=" api-rerank-similarity ",
            ),
            service_name="ai-policy",
        )
        assert default_task.model_pack_id == "api-rerank"
        assert private_task.model_pack_id == "api-rerank-similarity"

        default_run = await service.start_task_run(
            default_task.id,
            TaskRunRequest(idempotency_key="default-run"),
        )
        private_run = await service.start_task_run(
            private_task.id,
            TaskRunRequest(idempotency_key="private-run"),
        )
        assert default_run.model_pack_id == "api-rerank"
        assert private_run.model_pack_id == "api-rerank-similarity"

        async with create_db_session() as session:
            rows = list((await session.exec(select(TaskRunEntity))).all())
        assert {row.model_pack_id for row in rows} == {
            "api-rerank",
            "api-rerank-similarity",
        }

    asyncio.run(run())


def test_omitted_and_explicit_default_pack_are_idempotently_equivalent(
    tmp_path,
    monkeypatch,
) -> None:
    async def run() -> None:
        service = await _service(tmp_path, monkeypatch, "model-pack-default.db")
        omitted = await service.create_task(
            _request(key="same-default"),
            service_name="ai-policy",
        )
        explicit = await service.create_task(
            _request(key="same-default", model_pack_id="api-rerank"),
            service_name="ai-policy",
        )
        assert omitted.id == explicit.id
        assert explicit.model_pack_id == "api-rerank"

    asyncio.run(run())


def test_model_pack_participates_in_task_idempotency_fingerprint(
    tmp_path,
    monkeypatch,
) -> None:
    async def run() -> None:
        service = await _service(tmp_path, monkeypatch, "model-pack-idempotency.db")
        await service.create_task(
            _request(key="pack-conflict", model_pack_id="api-rerank"),
            service_name="ai-policy",
        )
        with pytest.raises(IdempotencyConflictError):
            await service.create_task(
                _request(
                    key="pack-conflict",
                    model_pack_id="api-rerank-similarity",
                ),
                service_name="ai-policy",
            )

    asyncio.run(run())


def test_unknown_model_pack_is_rejected_before_task_persistence(
    tmp_path,
    monkeypatch,
) -> None:
    async def run() -> None:
        service = await _service(tmp_path, monkeypatch, "model-pack-invalid.db")
        with pytest.raises(ValueError, match="Invalid model_pack_id"):
            await service.create_task(
                _request(key="invalid-pack", model_pack_id="missing-pack"),
                service_name="ai-policy",
            )

    asyncio.run(run())


def test_two_users_can_create_different_model_pack_tasks_concurrently(
    tmp_path,
    monkeypatch,
) -> None:
    async def run() -> None:
        service = await _service(tmp_path, monkeypatch, "model-pack-concurrent.db")
        public_task, private_task = await asyncio.gather(
            service.create_task(
                _request(
                    key="user-api",
                    model_pack_id="api-rerank",
                    user_id="user-public",
                ),
                service_name="ai-policy",
            ),
            service.create_task(
                _request(
                    key="user-private",
                    model_pack_id="api-rerank-similarity",
                    user_id="user-private",
                ),
                service_name="ai-policy",
            ),
        )
        assert (public_task.user_id, public_task.model_pack_id) == (
            "user-public",
            "api-rerank",
        )
        assert (private_task.user_id, private_task.model_pack_id) == (
            "user-private",
            "api-rerank-similarity",
        )

    asyncio.run(run())
