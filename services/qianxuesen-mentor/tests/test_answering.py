from __future__ import annotations

import asyncio
from types import SimpleNamespace

from qianxuesen_mentor.answering import MentorAnswerService
from qianxuesen_mentor.answering import _retrieval_query
from qianxuesen_mentor.config import Settings
from qianxuesen_mentor.model_clients import _is_official_deepseek_v4
from qianxuesen_mentor.schemas import ChatHistoryItem


class FakeRetrieval:
    def search(self, query, *, top_k, retrieval_mode):
        assert query == "怎样组织科研项目？"
        assert top_k == 12
        assert retrieval_mode == "hybrid"
        return {"results": [{
            "evidence_type": "book",
            "document_id": "book-1",
            "document_name": "论系统工程",
            "chapter": "科研系统工程",
            "page_start": 59,
            "page_end": 59,
            "chunk_id": "chunk-1",
            "quote": "科学技术研究的组织管理要运用系统工程。",
        }]}


class FakeModels:
    prompts = None

    async def plan_tool(self, question, recent_history, **_availability):
        return {"tool": "qxs_retrieve", "query": question, "code": ""}

    async def should_retrieve(self, question, recent_history):
        return True

    async def stream_chat(self, messages):
        self.prompts = messages
        yield "先明确目标，"
        yield "再组织实施 [E1]。"


class DirectModels(FakeModels):
    async def plan_tool(self, question, recent_history, **_availability):
        assert question == "你好"
        assert recent_history == []
        return {"tool": "direct", "query": question, "code": ""}

    async def should_retrieve(self, question, recent_history):
        assert question == "你好"
        assert recent_history == []
        return False


class UnexpectedRetrieval:
    def search(self, *_args, **_kwargs):
        raise AssertionError("direct conversation must not retrieve")


def test_answer_stream_contains_grounded_text_and_citation():
    models = FakeModels()
    service = MentorAnswerService(FakeRetrieval(), models)

    async def collect():
        return [event async for event in service.stream(
            "怎样组织科研项目？", history=[], top_k=12,
        )]

    events = asyncio.run(collect())
    assert [event for event, _ in events].count("delta") == 2
    assert events[-1][0] == "done"
    citation = next(data["citation"] for event, data in events if event == "citation")
    assert citation["sourceName"] == "论系统工程"
    assert citation["location"] == "第 59 页"
    assert "不必逐条复述资料" in models.prompts[-1]["content"]
    assert "先判断资料是否真正支持问题" in models.prompts[-1]["content"]
    assert "不必套固定结构" in models.prompts[0]["content"]
    assert "资料中未找到" in models.prompts[0]["content"]


def test_model_router_skips_retrieval_for_greeting():
    models = DirectModels()
    service = MentorAnswerService(UnexpectedRetrieval(), models)

    async def collect():
        return [event async for event in service.stream(
            "你好", history=[], top_k=12,
        )]

    events = asyncio.run(collect())
    event_types = [event for event, _ in events]
    assert event_types == ["meta", "delta", "delta", "done"]
    assert models.prompts[-1] == {"role": "user", "content": "你好"}


def test_deepseek_v4_uses_official_thinking_switch():
    assert _is_official_deepseek_v4(SimpleNamespace(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
    ))
    assert not _is_official_deepseek_v4(SimpleNamespace(
        provider="openai_compatible",
        base_url="http://localhost:11434/v1",
        model="qwen3",
    ))


def test_answer_temperature_is_service_scoped_and_validated():
    settings = Settings(_env_file=None, llm_answer_temperature=0.35)
    assert settings.llm_answer_temperature == 0.35


def test_short_followup_retrieval_keeps_previous_user_question():
    history = [
        ChatHistoryItem(role="user", content="钱学森是哪一年回国的？"),
        ChatHistoryItem(role="assistant", content="1955 年。"),
    ]
    assert _retrieval_query("那具体是哪一天？", history) == (
        "钱学森是哪一年回国的？\n追问：那具体是哪一天？"
    )
