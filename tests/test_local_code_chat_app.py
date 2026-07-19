import asyncio

import httpx

from backend.local_code_chat_app import DEFAULT_RAG_PDF_PATH, create_app
from backend.simple_chat_app import LOCAL_PYTHON_ARTIFACT_DIR
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


def test_default_rag_pdf_uses_tracked_repository_file():
    assert DEFAULT_RAG_PDF_PATH.is_file()
    assert DEFAULT_RAG_PDF_PATH.name == "1.pdf"


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
        assert "LimitedLocalPythonInterpreter" in body["response"]["choices"][0]["message"]["content"]

        await session_history_manager.clear_history(
            "local-code-app-test-user",
            body["thread_id"],
        )
        await _delete_thread(body["thread_id"])

    asyncio.run(run())


def test_local_code_chat_app_does_not_expose_artifact_directory():
    async def run():
        app = create_app()
        run_dir = LOCAL_PYTHON_ARTIFACT_DIR / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        artifact_file = run_dir / "chart.svg"
        artifact_file.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>',
            encoding="utf-8",
        )

        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/tool-artifacts/local-python/test-run/chart.svg")

            assert response.status_code == 404
        finally:
            artifact_file.unlink(missing_ok=True)
            run_dir.rmdir()

    asyncio.run(run())
