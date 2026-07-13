import asyncio

import httpx

from backend.local_code_chat_app import create_app
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import init_db, reset_engine_for_test
from scheduling.scheduler import SchedulingRuntimeOptions
from skill import SkillManager
from task_manager.memory import TaskMemoryService, render_task_memory
import task_manager.memory as memory_module


def test_task_memory_versions_are_scoped_by_user_and_injected_as_prompt(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'task-memory.db'}")
        reset_engine_for_test()
        await init_db()

        captured_messages: list[str] = []

        async def fake_chat(self, profile, request):
            captured_messages.append(request.message or "")
            if "Previous memory:\n(empty)" in (request.message or ""):
                memory = "脚本开头优先直接给出核心结论。"
            elif "不要使用公文表达" in (request.message or ""):
                memory = "脚本开头优先直接给出核心结论；使用自然口语，避免公文化表达。"
            else:
                memory = "另一个用户的独立记忆。"
            return {
                "response": {
                    "choices": [
                        {"message": {"content": '{"memory":"' + memory + '"}'}}
                    ]
                }
            }

        monkeypatch.setattr(memory_module.SchedulingService, "chat", fake_chat)

        app = create_app()
        user_headers = {"X-User-Id": "lzt", "X-Tenant-Id": DEFAULT_TENANT_ID}
        other_headers = {"X-User-Id": "nrx", "X-Tenant-Id": DEFAULT_TENANT_ID}
        task_key = "lzt_jiaoben_123"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            page = await client.get("/task-memory-test/")
            assert page.status_code == 200
            assert "Task Memory Test" in page.text

            first = await client.post(
                f"/task-manager/memories/{task_key}/compress",
                headers=user_headers,
                json={"new_information": "以后脚本开头直接给结论。"},
            )
            assert first.status_code == 200, first.text
            assert first.json()["memory"]["version"] == 1

            second = await client.post(
                f"/task-manager/memories/{task_key}/compress",
                headers=user_headers,
                json={"new_information": "以后不要使用公文表达。"},
            )
            assert second.status_code == 200, second.text
            assert second.json()["memory"]["version"] == 2
            assert "自然口语" in second.json()["memory"]["content"]
            assert "脚本开头优先直接给出核心结论" in captured_messages[1]

            latest = await client.get(
                f"/task-manager/memories/{task_key}",
                headers=user_headers,
            )
            assert latest.status_code == 200
            assert latest.json()["memory"]["version"] == 2

            isolated = await client.get(
                f"/task-manager/memories/{task_key}",
                headers=other_headers,
            )
            assert isolated.status_code == 200
            assert isolated.json()["memory"] is None

        options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
        latest_row = await TaskMemoryService(options).get_latest(
            tenant_id=DEFAULT_TENANT_ID,
            user_id="lzt",
            task_key=task_key,
        )
        prompt = render_task_memory(latest_row)
        assert "# Task Memory" in prompt
        assert "current request always takes priority" in prompt
        assert "自然口语" in prompt

        skill_context = await SkillManager(tenant_id=DEFAULT_TENANT_ID).create_context(
            "media-script-memory-compression-package"
        )
        assert "# Media Script Task Memory Compression" in skill_context.task_prompt

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_task_memory_requires_a_stable_task_key(tmp_path):
    async def run():
        options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
        service = TaskMemoryService(options)
        try:
            await service.get_latest(
                tenant_id=DEFAULT_TENANT_ID,
                user_id="lzt",
                task_key="",
            )
        except ValueError as exc:
            assert "task_key must not be empty" in str(exc)
        else:
            raise AssertionError("An empty task_key must be rejected.")

    asyncio.run(run())
