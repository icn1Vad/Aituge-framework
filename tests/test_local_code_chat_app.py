import asyncio

import httpx

from backend.local_code_chat_app import create_app
from common.llm.models import TextChunk
from db.db_context import create_db_session
import service.agent.single_agent_runner as runner_mod
from service.cache.session_history_manager import session_history_manager
from service.thread.thread_service import ThreadService


class CapturingAgent:
    def __init__(self, llm, system_prompt, tools):
        self.tools = tools

    async def run_async(self, state):
        async def gen():
            tool_names = ",".join(tool.metadata.name for tool in self.tools)
            yield TextChunk(delta=f"tools={tool_names}")

        return gen()


async def _delete_thread(thread_id: str):
    async with create_db_session() as session:
        try:
            await ThreadService(session).delete_thread(thread_id)
        except ValueError:
            pass


def test_local_code_chat_app_injects_local_python_tool(monkeypatch):
    async def run():
        monkeypatch.setattr(runner_mod, "ReactAgent", CapturingAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/single-agent/chat",
                json={
                    "message": "hello",
                    "user_id": "local-code-app-test-user",
                    "stream": False,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["response"]["choices"][0]["message"]["content"] == (
            "tools=LimitedLocalPythonInterpreter"
        )

        await session_history_manager.clear_history(
            "local-code-app-test-user",
            body["thread_id"],
        )
        await _delete_thread(body["thread_id"])

    asyncio.run(run())

