import asyncio

import httpx

from backend.local_code_chat_app import create_app
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db, reset_engine_for_test
from db.models.message import MessageCreate
from db.models.thread import ThreadCreate
from scheduling.scheduler import SchedulingRuntimeOptions
from service.thread.message_service import MessageService
from service.thread.thread_service import ThreadService
from skill import SkillManager
from task_manager.memory import TaskMemoryRefreshContext, TaskMemoryService, render_task_memory
from task_manager.models import TaskEntity
from task_manager.registry import ConversationTaskType, get_task_definition
from task_manager.schemas import TaskCreateRequest
from task_manager.service import TaskManagerService
import task_manager.memory as memory_module


def test_task_memory_versions_are_scoped_by_user_and_injected_as_prompt(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'task-memory.db'}")
        reset_engine_for_test()
        await init_db()

        captured_messages: list[str] = []
        captured_system_prompts: list[str] = []

        class FakeLlmRuntime:
            def __init__(self, tenant_id, model_pack_id=None):
                assert tenant_id == DEFAULT_TENANT_ID
                assert model_pack_id is None

            async def complete(
                self,
                messages,
                model_id=None,
                system_prompt="",
                max_tokens=None,
                temperature=None,
            ):
                message = messages[-1]["content"]
                captured_messages.append(message)
                captured_system_prompts.append(system_prompt)
                assert model_id is None
                assert max_tokens is None
                assert temperature is None
                if "Previous memory:\n(empty)" in message:
                    memory = "脚本开头优先直接给出核心结论。"
                elif "不要使用公文表达" in message:
                    memory = "脚本开头优先直接给出核心结论；使用自然口语，避免公文化表达。"
                else:
                    memory = "另一个用户的独立记忆。"
                return '{"memory":"' + memory + '"}'

        monkeypatch.setattr(memory_module, "LlmRuntime", FakeLlmRuntime)

        app = create_app()
        user_headers = {"X-User-Id": "lzt", "X-Tenant-Id": DEFAULT_TENANT_ID}
        other_headers = {"X-User-Id": "nrx", "X-Tenant-Id": DEFAULT_TENANT_ID}
        task_key = "media_script"
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
            assert "# Media Script Task Memory Compression" in captured_system_prompts[0]

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
        assert "Preserve the force and scope" in skill_context.task_prompt

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


def test_task_type_refresh_is_explicit_and_conversation_aware(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'refresh.db'}")
        reset_engine_for_test()
        await init_db()
        options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")

        default_task_type = get_task_definition("media.script.select")
        skipped = await default_task_type.refresh_memory(
            TaskEntity(
                id="default-task",
                task_type="media.script.select",
                task_key="default-memory",
                user_id="memory-user",
                tenant_id=DEFAULT_TENANT_ID,
            ),
            TaskMemoryRefreshContext(options=options),
        )
        assert skipped.status == "skipped"
        assert skipped.reason == "no_memory_material"
        assert await TaskMemoryService(options).get_latest(
            tenant_id=DEFAULT_TENANT_ID,
            user_id="memory-user",
            task_key="default-memory",
        ) is None

        task_type = get_task_definition("media.chat")
        assert isinstance(task_type, ConversationTaskType)
        async with create_db_session() as session:
            thread = await ThreadService(session).create_thread(
                ThreadCreate(user_id="memory-user", title="Memory source"),
                DEFAULT_TENANT_ID,
            )
            message_service = MessageService(session)
            for role, text in [
                ("system", "internal"),
                ("user", "以后开头直接说结论。"),
                ("tool", "temporary tool output"),
                ("assistant", "明白，我会保持直接。"),
            ]:
                await message_service.create_message(
                    MessageCreate(
                        thread_id=thread.id,
                        role=role,
                        content=[{"type": "text", "text": text}],
                    ),
                    DEFAULT_TENANT_ID,
                )
            await session.commit()

        service = TaskManagerService(options)
        task = await service.create_task(
            TaskCreateRequest(
                task_type="media.chat",
                task_key="media-chat-memory",
                input_payload={"message": "继续优化脚本"},
                user_id="memory-user",
                thread_id=thread.id,
                session_id="media-chat-session",
                stream=False,
            )
        )

        captured = {}

        class FakeLlmRuntime:
            def __init__(self, tenant_id, model_pack_id=None):
                assert tenant_id == DEFAULT_TENANT_ID
                assert model_pack_id == "api-rerank"

            async def complete(self, messages, model_id=None, **kwargs):
                assert model_id is None
                captured["message"] = messages[-1]["content"]
                return '{"memory":"脚本开头直接给出结论。"}'

        monkeypatch.setattr(memory_module, "LlmRuntime", FakeLlmRuntime)

        refreshed = await service.refresh_task_memory(task.id)
        assert refreshed.status == "updated"
        assert refreshed.memory is not None
        assert refreshed.memory.version == 1
        assert "[user]" in refreshed.memory.source_text
        assert "[assistant]" in refreshed.memory.source_text
        assert "temporary tool output" not in refreshed.memory.source_text
        assert "Assistant suggestions are not facts" in captured["message"]

        foreign_user = await task_type.refresh_memory(
            task.model_copy(
                update={
                    "id": "foreign-user-task",
                    "user_id": "another-user",
                    "task_key": "foreign-user-memory",
                }
            ),
            TaskMemoryRefreshContext(options=options),
        )
        assert foreign_user.status == "skipped"
        assert foreign_user.reason == "no_memory_material"

        async with create_db_session() as session:
            empty_thread = await ThreadService(session).create_thread(
                ThreadCreate(user_id="memory-user", title="Empty memory source"),
                DEFAULT_TENANT_ID,
            )
            task_row = await session.get(TaskEntity, task.id)
            assert task_row is not None
            task_row.thread_id = empty_thread.id
            session.add(task_row)
            await session.commit()

        empty = await service.refresh_task_memory(task.id)
        assert empty.status == "skipped"
        assert empty.reason == "no_memory_material"
        latest = await TaskMemoryService(options).get_latest(
            tenant_id=DEFAULT_TENANT_ID,
            user_id="memory-user",
            task_key="media-chat-memory",
        )
        assert latest is not None
        assert latest.version == 1

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
