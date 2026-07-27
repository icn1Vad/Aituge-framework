from __future__ import annotations

import asyncio
import httpx

import pytest

from backend.local_code_chat_app import create_app
from db.db_context import create_db_session, init_db, reset_engine_for_test
from scheduling.scheduler import SchedulingRuntimeOptions
from task_manager.idempotency import IdempotencyConflictError
from task_manager.models import TaskEntity, TaskRunEntity
from task_manager.schemas import TaskCreateRequest, TaskRunRequest
from task_manager.service import TaskManagerService


def _task_request(*, key: str, goal: str = "review contract", user_id: str = "user-1"):
    return TaskCreateRequest(
        task_type="pipeline.demo",
        idempotency_key=key,
        input_payload={"goal": goal},
        user_id=user_id,
        tenant_id="tenant-1",
    )


async def _service(tmp_path):
    reset_engine_for_test()
    await init_db()
    return TaskManagerService(
        SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
    )


def test_task_idempotency_is_concurrent_and_fingerprint_scoped(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'task-idempotency.db'}",
        )
        service = await _service(tmp_path)
        request = _task_request(key="same-key")

        rows = await asyncio.gather(
            *(
                service.create_task(request, service_name="ai-contract")
                for _ in range(4)
            )
        )

        assert {row.id for row in rows} == {rows[0].id}
        assert rows[0].service == "ai-contract"
        assert rows[0].request_fingerprint
        async with create_db_session() as session:
            tasks = list((await session.exec(select(TaskEntity))).all())
        assert len(tasks) == 1

        with pytest.raises(IdempotencyConflictError) as exc_info:
            await service.create_task(
                _task_request(key="same-key", goal="different request"),
                service_name="ai-contract",
            )
        assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"

    from sqlmodel import select

    asyncio.run(run())


def test_task_idempotency_scope_includes_service_and_tenant(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'task-scope.db'}",
        )
        service = await _service(tmp_path)
        request = _task_request(key="same-key")

        contract_task = await service.create_task(request, service_name="ai-contract")
        other_service_task = await service.create_task(request, service_name="ai-policy")

        assert contract_task.id != other_service_task.id
        assert contract_task.tenant_id == other_service_task.tenant_id

    asyncio.run(run())


def test_run_idempotency_returns_original_run_and_rejects_changed_request(
    tmp_path,
    monkeypatch,
):
    async def run():
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'run-idempotency.db'}",
        )
        service = await _service(tmp_path)
        task = await service.create_task(
            _task_request(key="task-key"),
            service_name="ai-contract",
        )
        monkeypatch.setattr(
            "task_manager.service.start_background_run",
            lambda run_id, coroutine: coroutine.close(),
        )

        first = await service.start_task_run(
            task.id,
            TaskRunRequest(idempotency_key="run-key"),
        )
        second = await service.start_task_run(
            task.id,
            TaskRunRequest(idempotency_key="run-key"),
        )

        assert first.id == second.id
        assert first.request_fingerprint
        async with create_db_session() as session:
            runs = list(
                (
                    await session.exec(
                        select(TaskRunEntity).where(TaskRunEntity.task_id == task.id)
                    )
                ).all()
            )
        assert len(runs) == 1

        with pytest.raises(IdempotencyConflictError):
            await service.start_task_run(
                task.id,
                TaskRunRequest(
                    idempotency_key="run-key",
                    metadata_patch={"changed": True},
                ),
            )

    from sqlmodel import select

def test_http_idempotency_conflict_is_409_and_service_scoped(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'http-idempotency.db'}",
        )
        reset_engine_for_test()
        await init_db()
        app = create_app()
        headers = {
            "X-User-Id": "api-user",
            "X-Tenant-Id": "tenant-1",
            "X-Internal-Service": "ai-contract",
            "Idempotency-Key": "api-task-key",
        }
        payload = {
            "task_type": "pipeline.demo",
            "title": "API idempotency",
            "input": {"goal": "Check API idempotency"},
            "client_context": {"source": "idempotency-test"},
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            first = await client.post("/task-manager/tasks", headers=headers, json=payload)
            assert first.status_code == 200
            first_id = first.json()["task"]["id"]

            duplicate = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json=payload,
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["task"]["id"] == first_id

            conflict = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={**payload, "title": "Changed request"},
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"

            other_service_headers = {
                **headers,
                "X-Internal-Service": "ai-policy",
            }
            other_service = await client.post(
                "/task-manager/tasks",
                headers=other_service_headers,
                json=payload,
            )
            assert other_service.status_code == 200
            assert other_service.json()["task"]["id"] != first_id

    asyncio.run(run())


    asyncio.run(run())
