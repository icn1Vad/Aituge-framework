import asyncio
import json

import httpx

from backend.simple_chat_app import create_app
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
            text = state.messages[-1]["content"]
            yield TextChunk(delta="outer ")
            yield TextChunk(delta=f"reply: {text}")

        return gen()


async def _delete_thread(thread_id: str):
    async with create_db_session() as session:
        try:
            await ThreadService(session).delete_thread(thread_id)
        except ValueError:
            pass


def test_outer_backend_serves_frontend_and_chat(monkeypatch):
    async def run():
        monkeypatch.setattr(runner_mod, "ReactAgent", FakeAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        thread_ids = []

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            page = await client.get("/")
            assert page.status_code == 200
            assert "PAI-RAG" in page.text
            assert "新建对话" in page.text
            assert "报告 skill" in page.text

            non_stream = await client.post(
                "/single-agent/chat",
                json={
                    "message": "hello",
                    "user_id": "outer-test-user",
                    "stream": False,
                },
            )
            assert non_stream.status_code == 200
            body = non_stream.json()
            thread_ids.append(body["thread_id"])
            assert body["response"]["choices"][0]["message"]["content"] == "outer reply: hello"

            stream = await client.post(
                "/single-agent/chat",
                json={
                    "message": "stream hello",
                    "user_id": "outer-test-user",
                    "stream": True,
                },
            )
            assert stream.status_code == 200
            assert "event: metadata" in stream.text
            assert "event: chunk" in stream.text
            assert "event: final" in stream.text
            assert "outer " in stream.text
            assert "reply: stream hello" in stream.text
            for line in stream.text.splitlines():
                if line.startswith("data: ") and '"event":"metadata"' in line:
                    thread_ids.append(json.loads(line.removeprefix("data: "))["thread_id"])
                    break

        for thread_id in thread_ids:
            await session_history_manager.clear_history("outer-test-user", thread_id)
            await _delete_thread(thread_id)

    asyncio.run(run())
