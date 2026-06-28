import asyncio
import json

import httpx
from llama_index.core.tools.function_tool import FunctionTool

from api.single_agent_api import create_app
from common.llm.models import TextChunk
from db.db_context import create_db_session
import service.agent.single_agent_runner as runner_mod
from service.cache.session_history_manager import session_history_manager
from service.thread.thread_service import ThreadService
from tool import ToolBundle


async def _delete_thread(thread_id: str):
    async with create_db_session() as session:
        try:
            await ThreadService(session).delete_thread(thread_id)
        except ValueError:
            pass


def test_api_accepts_task_owned_tool_bundle(monkeypatch):
    async def run():
        seen_tool_names = []
        cleanup_calls = []

        class CapturingAgent:
            def __init__(self, llm, system_prompt, tools):
                self.tools = tools
                self.system_prompt = system_prompt
                seen_tool_names.append([tool.metadata.name for tool in tools])

            async def run_async(self, state):
                async def gen():
                    user_text = state.messages[-1]["content"]
                    tool_names = ",".join(seen_tool_names[-1])
                    yield TextChunk(delta=f"tools={tool_names}; message={user_text}")

                return gen()

        async def external_echo(value: str) -> str:
            return value

        async def cleanup():
            cleanup_calls.append("closed")

        def tool_provider(request):
            tool = FunctionTool.from_defaults(
                async_fn=external_echo,
                name="ExternalEcho",
                description="Echoes a value from an externally selected tool.",
            )
            if request.stream:
                return [tool], cleanup
            return ToolBundle.from_tools([tool], cleanup=cleanup)

        monkeypatch.setattr(runner_mod, "ReactAgent", CapturingAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        app = create_app(tool_provider=tool_provider)
        transport = httpx.ASGITransport(app=app)
        thread_ids = []

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            non_stream = await client.post(
                "/single-agent/chat",
                json={
                    "message": "hello tools",
                    "user_id": "tool-provider-test-user",
                    "stream": False,
                },
            )
            assert non_stream.status_code == 200
            body = non_stream.json()
            thread_ids.append(body["thread_id"])
            assert body["response"]["choices"][0]["message"]["content"] == (
                "tools=ExternalEcho; message=hello tools"
            )

            stream = await client.post(
                "/single-agent/chat",
                json={
                    "message": "stream tools",
                    "user_id": "tool-provider-test-user",
                    "stream": True,
                },
            )
            assert stream.status_code == 200
            assert "tools=ExternalEcho; message=stream tools" in stream.text
            for line in stream.text.splitlines():
                if line.startswith("data: ") and '"event":"metadata"' in line:
                    thread_ids.append(json.loads(line.removeprefix("data: "))["thread_id"])
                    break

        assert seen_tool_names == [["ExternalEcho"], ["ExternalEcho"]]
        assert cleanup_calls == ["closed", "closed"]

        for thread_id in thread_ids:
            await session_history_manager.clear_history("tool-provider-test-user", thread_id)
            await _delete_thread(thread_id)

    asyncio.run(run())
