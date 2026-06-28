import asyncio
import json

import httpx

from api.single_agent_api import create_app
from common.llm.models import TextChunk
from db.db_context import create_db_session
from service.cache.session_history_manager import session_history_manager
from service.thread.thread_service import ThreadService
import service.agent.single_agent_runner as runner_mod


class FakeAgent:
    def __init__(self, llm, system_prompt, tools):
        self.tools = tools
        self.system_prompt = system_prompt

    async def run_async(self, state):
        async def gen():
            user_text = state.messages[-1]["content"]
            yield TextChunk(delta=f"mock answer: {user_text}")

        return gen()


async def _delete_thread(thread_id: str):
    async with create_db_session() as session:
        try:
            await ThreadService(session).delete_thread(thread_id)
        except ValueError:
            pass


def test_single_agent_api_non_stream_and_stream(monkeypatch):
    async def run():
        monkeypatch.setattr(runner_mod, "ReactAgent", FakeAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        thread_ids = []
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            non_stream_resp = await client.post(
                "/single-agent/chat",
                json={
                    "message": "hello",
                    "user_id": "api-test-user",
                    "stream": False,
                },
            )
            assert non_stream_resp.status_code == 200
            non_stream = non_stream_resp.json()
            thread_ids.append(non_stream["thread_id"])
            assert non_stream["response"]["choices"][0]["message"]["content"] == "mock answer: hello"
            assert non_stream["user_message_id"]
            assert non_stream["assistant_message_id"]

            stream_resp = await client.post(
                "/single-agent/chat",
                json={
                    "message": "stream hello",
                    "user_id": "api-test-user",
                    "stream": True,
                },
            )
            assert stream_resp.status_code == 200
            body = stream_resp.text
            assert "event: metadata" in body
            assert "event: chunk" in body
            assert "event: final" in body
            assert "mock answer: stream hello" in body
            for line in body.splitlines():
                if line.startswith("data: ") and '"event":"metadata"' in line:
                    thread_ids.append(json.loads(line.removeprefix("data: "))["thread_id"])
                    break

        for thread_id in thread_ids:
            await session_history_manager.clear_history("api-test-user", thread_id)
            await _delete_thread(thread_id)

    asyncio.run(run())


def test_single_agent_api_lists_threads_and_messages(monkeypatch):
    async def run():
        monkeypatch.setattr(runner_mod, "ReactAgent", FakeAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        thread_id = None

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            chat_resp = await client.post(
                "/single-agent/chat",
                json={
                    "message": "history hello",
                    "user_id": "api-history-list-user",
                    "stream": False,
                },
            )
            assert chat_resp.status_code == 200
            thread_id = chat_resp.json()["thread_id"]

            threads_resp = await client.get(
                "/single-agent/threads",
                params={"user_id": "api-history-list-user"},
            )
            assert threads_resp.status_code == 200
            threads = threads_resp.json()["threads"]
            assert any(thread["id"] == thread_id for thread in threads)

            messages_resp = await client.get(f"/single-agent/threads/{thread_id}/messages")
            assert messages_resp.status_code == 200
            messages = messages_resp.json()["messages"]
            assert [message["role"] for message in messages] == ["user", "assistant"]
            assert messages[0]["text"] == "history hello"
            assert messages[1]["text"] == "mock answer: history hello"

        await session_history_manager.clear_history("api-history-list-user", thread_id)
        await _delete_thread(thread_id)

    asyncio.run(run())


def test_single_agent_restores_sqlite_history_when_redis_missing(monkeypatch):
    async def run():
        class CapturingAgent:
            def __init__(self, llm, system_prompt, tools):
                pass

            async def run_async(self, state):
                async def gen():
                    roles = ",".join(message["role"] for message in state.messages)
                    contents = "|".join(str(message["content"]) for message in state.messages)
                    yield TextChunk(delta=f"roles={roles}; contents={contents}")

                return gen()

        class FakeLlm:
            context_window = 4096
            max_tokens = 512

        monkeypatch.setattr(runner_mod, "ReactAgent", CapturingAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: FakeLlm())

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        thread_id = None

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first_resp = await client.post(
                "/single-agent/chat",
                json={
                    "message": "first turn",
                    "user_id": "api-sqlite-restore-user",
                    "stream": False,
                },
            )
            assert first_resp.status_code == 200
            thread_id = first_resp.json()["thread_id"]

            await session_history_manager.clear_history("api-sqlite-restore-user", thread_id)

            second_resp = await client.post(
                "/single-agent/chat",
                json={
                    "message": "second turn",
                    "user_id": "api-sqlite-restore-user",
                    "thread_id": thread_id,
                    "session_id": thread_id,
                    "stream": False,
                },
            )
            assert second_resp.status_code == 200
            content = second_resp.json()["response"]["choices"][0]["message"]["content"]
            assert "roles=user,assistant,user" in content
            assert "first turn" in content
            assert "second turn" in content

        await session_history_manager.clear_history("api-sqlite-restore-user", thread_id)
        await _delete_thread(thread_id)

    asyncio.run(run())
