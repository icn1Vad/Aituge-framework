import asyncio
import json
from pathlib import Path

import httpx
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall

from api.single_agent_api import create_app
from common.llm.models import TextChunk
from data.RAG.tool_retrieval import (
    KnowledgeBase,
    KnowledgeBaseChunk,
    KnowledgeBaseFile,
    ToolRetrievalRAG,
)
from db.db_context import create_db_session, init_db, reset_engine_for_test
import service.agent.single_agent_runner as runner_mod
from service.cache.session_history_manager import session_history_manager
from service.thread.thread_service import ThreadService
from tool import ToolBundle
from tool.local_runtime import LimitedLocalPythonConfig, create_limited_local_python_bundle
from tool.search import AliyunSearchConfig, create_aliyun_web_search_bundle
from tool.search.aliyun_search import AliyunSearchTool


def _tool_call(index: int, name: str, arguments: dict) -> ChoiceDeltaToolCall:
    return ChoiceDeltaToolCall(
        index=index,
        id=f"call_{index}_{name.replace('-', '_')}",
        type="function",
        function={
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    )


async def _delete_thread(thread_id: str):
    async with create_db_session() as session:
        try:
            await ThreadService(session).delete_thread(thread_id)
        except ValueError:
            pass


class _ToolCallingFakeLlm:
    context_window = 16_000
    max_tokens = 2_000

    def __init__(self):
        self.calls = 0

    async def astream(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            async def first_pass():
                tool_names = {
                    tool["function"]["name"]
                    for tool in tools
                    if tool.get("type") == "function"
                }
                assert {
                    "ReadSkill",
                    "LimitedLocalPythonInterpreter",
                    "aliyun-websearch",
                    "search-knowledgebase-kb_project",
                }.issubset(tool_names)

                yield TextChunk(
                    tool_calls=[
                        _tool_call(
                            0,
                            "ReadSkill",
                            {"skill_name": "media-script-generator"},
                        ),
                        _tool_call(
                            1,
                            "LimitedLocalPythonInterpreter",
                            {
                                "code": (
                                    "from pathlib import Path\n"
                                    "Path('chart.svg').write_text("
                                    "\"<svg xmlns='http://www.w3.org/2000/svg' "
                                    "width='120' height='40'><text x='6' y='24'>"
                                    "TUGE</text></svg>\", encoding='utf-8')\n"
                                    "print('drawing artifact ready')"
                                )
                            },
                        ),
                        _tool_call(
                            2,
                            "aliyun-websearch",
                            {"query": "TUGE single agent framework test"},
                        ),
                        _tool_call(
                            3,
                            "search-knowledgebase-kb_project",
                            {"query": "internal audit board report"},
                        ),
                    ]
                )

            return first_pass()

        async def final_pass():
            tool_context = "\n".join(str(message.get("content") or "") for message in messages)
            assert "You are a short-video script creator" in tool_context
            assert "drawing artifact ready" in tool_context
            assert '"artifacts":[]' in tool_context
            assert "Aliyun mocked result" in tool_context
            assert "Internal audit evidence for board reporting" in tool_context
            yield TextChunk(
                delta=(
                    "combined framework ok: skill + drawing artifact + web search "
                    "+ rag retrieval"
                )
            )

        return final_pass()


def _rag_bundle() -> ToolBundle:
    rag = ToolRetrievalRAG(
        knowledgebases=[
            KnowledgeBase(
                id="kb_project",
                name="Project KB",
                description="Framework acceptance knowledgebase.",
            )
        ],
        files=[
            KnowledgeBaseFile(
                id="file_1",
                kb_id="kb_project",
                file_name="internal-audit.md",
                title="Internal Audit",
            )
        ],
        chunks=[
            KnowledgeBaseChunk(
                id="chunk_1",
                kb_id="kb_project",
                file_id="file_1",
                text=(
                    "Internal audit evidence for board reporting and audit "
                    "committee quarterly review."
                ),
                index=0,
            )
        ],
    )
    return ToolBundle.from_tools(rag.create_tools())


def test_combined_skill_artifact_search_and_rag_flow(monkeypatch, tmp_path: Path):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'combined.db'}")
        reset_engine_for_test()
        await init_db()
        fake_llm = _ToolCallingFakeLlm()

        class FakeLlmRuntime:
            def __init__(self, *args, **kwargs):
                pass

            async def get_llm(self, model_id):
                return fake_llm

        monkeypatch.setattr(runner_mod, "LlmRuntime", FakeLlmRuntime)

        async def fake_aliyun_search(self, query: str):
            assert self.config.api_key == "test-key"
            assert self.config.engine_type == "LiteAdvanced"
            assert self.config.search_count == 3
            assert query == "TUGE single agent framework test"
            return [
                {
                    "title": "Aliyun mocked result",
                    "url": "https://example.test/search",
                    "content": "Aliyun web search returned current framework evidence.",
                    "score": 0.91,
                }
            ]

        monkeypatch.setattr(AliyunSearchTool, "_asearch", fake_aliyun_search)

        python_bundle = create_limited_local_python_bundle(
            LimitedLocalPythonConfig(
                work_dir=tmp_path,
                keep_work_dir=True,
            )
        )
        search_bundle = create_aliyun_web_search_bundle(
            AliyunSearchConfig(
                api_key="test-key",
                search_count=3,
            )
        )
        combined_bundle = ToolBundle.combine(
            [python_bundle, search_bundle, _rag_bundle()]
        )

        def tool_provider(_request):
            return combined_bundle

        app = create_app(tool_provider=tool_provider)
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/single-agent/chat",
                json={
                    "message": "Run the full framework acceptance flow.",
                    "user_id": "combined-framework-test-user",
                    "skill_package": "media-script-select-package",
                    "stream": False,
                },
            )

        assert response.status_code == 200
        body = response.json()
        content = body["response"]["choices"][0]["message"]["content"]
        assert "combined framework ok" in content
        assert body["skills"]["active_package"]["primary"]["name"] == (
            "media-script-selector"
        )

        steps = body["response"]["steps"]
        step_names = {step["tool"]["function"]["name"] for step in steps}
        assert {
            "ReadSkill",
            "LimitedLocalPythonInterpreter",
            "aliyun-websearch",
            "search-knowledgebase-kb_project",
        } == step_names

        python_step = next(
            step
            for step in steps
            if step["tool"]["function"]["name"] == "LimitedLocalPythonInterpreter"
        )
        assert "drawing artifact ready" in python_step["result"]
        assert json.loads(python_step["result"])["artifacts"] == []
        assert list(tmp_path.glob("*/chart.svg"))

        await session_history_manager.clear_history(
            "combined-framework-test-user",
            body["thread_id"],
        )
        await _delete_thread(body["thread_id"])

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
