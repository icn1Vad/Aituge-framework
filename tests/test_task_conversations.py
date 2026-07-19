import asyncio
from datetime import datetime

import httpx

from backend.local_code_chat_app import create_app
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db, reset_engine_for_test
from db.models.message import MessageEntity
from db.models.thread import ThreadEntity
from task_manager.models import TaskEntity


def test_task_conversation_views_group_filter_and_paginate(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv(
            "SQLITE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'task-conversations.db'}",
        )
        reset_engine_for_test()
        await init_db()

        async with create_db_session() as session:
            session.add_all(
                [
                    ThreadEntity(
                        id="thread-a",
                        user_id="proof-user",
                        tenant_id=DEFAULT_TENANT_ID,
                        title="制度审批怎么做？",
                        created_at=datetime(2026, 7, 18, 9, 0),
                        updated_at=datetime(2026, 7, 18, 9, 0),
                    ),
                    ThreadEntity(
                        id="thread-b",
                        user_id="proof-user",
                        tenant_id=DEFAULT_TENANT_ID,
                        title="财务制度有哪些？",
                        created_at=datetime(2026, 7, 18, 10, 0),
                        updated_at=datetime(2026, 7, 18, 10, 0),
                    ),
                    ThreadEntity(
                        id="thread-other-user",
                        user_id="other-user",
                        tenant_id=DEFAULT_TENANT_ID,
                        title="其他用户的会话",
                    ),
                    ThreadEntity(
                        id="thread-other-type",
                        user_id="proof-user",
                        tenant_id=DEFAULT_TENANT_ID,
                        title="其他任务类型",
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    MessageEntity(
                        id="message-a-user",
                        thread_id="thread-a",
                        tenant_id=DEFAULT_TENANT_ID,
                        role="user",
                        content=[{"type": "text", "text": "制度审批怎么做？"}],
                        created_at=datetime(2026, 7, 18, 9, 1),
                    ),
                    MessageEntity(
                        id="message-a-assistant",
                        thread_id="thread-a",
                        tenant_id=DEFAULT_TENANT_ID,
                        role="assistant",
                        content=[{"type": "text", "text": "需要履行审批程序。[制度｜第一条｜Chunk #1]"}],
                        attachments=[
                            {
                                "id": "artifact-1",
                                "name": "image-001.png",
                                "mime": "image/png",
                                "url": "/task-manager/artifacts/artifact-1/content",
                            }
                        ],
                        created_at=datetime(2026, 7, 18, 9, 2),
                    ),
                    MessageEntity(
                        id="message-b-user",
                        thread_id="thread-b",
                        tenant_id=DEFAULT_TENANT_ID,
                        role="user",
                        content=[{"type": "text", "text": "财务制度有哪些？"}],
                        created_at=datetime(2026, 7, 18, 10, 1),
                    ),
                ]
            )
            session.add_all(
                [
                    TaskEntity(
                        id="task-a-old",
                        task_type="media.chat",
                        status="succeeded",
                        handler_name="scheduler",
                        thread_id="thread-a",
                        user_id="proof-user",
                        tenant_id=DEFAULT_TENANT_ID,
                        created_at=datetime(2026, 7, 18, 9, 0),
                        updated_at=datetime(2026, 7, 18, 9, 3),
                    ),
                    TaskEntity(
                        id="task-a-latest",
                        task_type="media.chat",
                        status="running",
                        handler_name="scheduler",
                        thread_id="thread-a",
                        current_run_id="run-a",
                        user_id="proof-user",
                        tenant_id=DEFAULT_TENANT_ID,
                        created_at=datetime(2026, 7, 18, 11, 0),
                        updated_at=datetime(2026, 7, 18, 14, 0),
                    ),
                    TaskEntity(
                        id="task-b",
                        task_type="media.chat",
                        status="succeeded",
                        handler_name="scheduler",
                        thread_id="thread-b",
                        user_id="proof-user",
                        tenant_id=DEFAULT_TENANT_ID,
                        created_at=datetime(2026, 7, 18, 10, 0),
                        updated_at=datetime(2026, 7, 18, 13, 0),
                    ),
                    TaskEntity(
                        id="task-other-user",
                        task_type="media.chat",
                        status="succeeded",
                        handler_name="scheduler",
                        thread_id="thread-other-user",
                        user_id="other-user",
                        tenant_id=DEFAULT_TENANT_ID,
                    ),
                    TaskEntity(
                        id="task-other-type",
                        task_type="media.script.select",
                        status="succeeded",
                        handler_name="scheduler",
                        thread_id="thread-other-type",
                        user_id="proof-user",
                        tenant_id=DEFAULT_TENANT_ID,
                    ),
                ]
            )
            await session.commit()

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        headers = {
            "X-User-Id": "proof-user",
            "X-Tenant-Id": DEFAULT_TENANT_ID,
        }
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first_page = await client.get(
                "/task-manager/conversations",
                params={"task_type": "media.chat", "limit": 1, "offset": 0},
                headers=headers,
            )
            assert first_page.status_code == 200, first_page.text
            assert first_page.json()["pagination"]["has_more"] is True
            assert [
                item["thread_id"] for item in first_page.json()["conversations"]
            ] == ["thread-a"]

            all_rows = await client.get(
                "/task-manager/conversations",
                params={"task_type": "media.chat"},
                headers=headers,
            )
            assert all_rows.status_code == 200, all_rows.text
            conversations = all_rows.json()["conversations"]
            assert [item["thread_id"] for item in conversations] == [
                "thread-a",
                "thread-b",
            ]
            assert conversations[0]["message_count"] == 2
            assert conversations[0]["preview"].startswith("需要履行审批程序")
            assert conversations[0]["latest_task"] == {
                "task_id": "task-a-latest",
                "status": "running",
                "run_id": "run-a",
                "stream_url": "/task-manager/runs/run-a/events/stream",
            }

            detail = await client.get(
                "/task-manager/conversations/thread-a",
                params={"task_type": "media.chat"},
                headers=headers,
            )
            assert detail.status_code == 200, detail.text
            assert [message["role"] for message in detail.json()["messages"]] == [
                "user",
                "assistant",
            ]
            assert detail.json()["messages"][1]["text"].endswith("Chunk #1]")
            assert detail.json()["messages"][1]["attachments"][0]["id"] == "artifact-1"
            assert detail.json()["latest_task"]["task_id"] == "task-a-latest"

            hidden = await client.get(
                "/task-manager/conversations/thread-a",
                params={"task_type": "media.chat"},
                headers={
                    "X-User-Id": "other-user",
                    "X-Tenant-Id": DEFAULT_TENANT_ID,
                },
            )
            assert hidden.status_code == 404
            assert hidden.json()["detail"]["code"] == "conversation_not_found"

            wrong_type = await client.get(
                "/task-manager/conversations/thread-a",
                params={"task_type": "media.script.select"},
                headers=headers,
            )
            assert wrong_type.status_code == 404

            unknown_type = await client.get(
                "/task-manager/conversations",
                params={"task_type": "unknown.task"},
                headers=headers,
            )
            assert unknown_type.status_code == 400

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
