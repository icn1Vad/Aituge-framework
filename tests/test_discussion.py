import asyncio
import json

import httpx

from backend.local_code_chat_app import create_app
from common.encrypt_utils import encrypt_key
from common.llm.models import TextChunk
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db, reset_engine_for_test
from db.models.llm import LlmModelEntity
from scheduling.discussion import DiscussionRunCreateRequest, DiscussionService
from scheduling.scheduler import (
    RuntimeContextBlock,
    SchedulingRuntimeContext,
    SchedulingRuntimeOptions,
)
from service.cache.session_history_manager import session_history_manager
import service.agent.single_agent_runner as runner_mod


class DiscussionAgent:
    def __init__(self, llm, system_prompt, tools):
        self.system_prompt = system_prompt

    async def run_async(self, state):
        user_text = state.messages[-1].get("content", "")

        if "You are RAG Agent" in self.system_prompt:
            decision = {
                "action": "speak",
                "content": "rag contribution",
                "reason": "rag has useful context",
            }
        elif "You are Code Agent" in self.system_prompt:
            decision = {
                "action": "pass",
                "content": "",
                "reason": "no code needed",
            }
        else:
            saw_rag = "rag contribution" in user_text
            decision = {
                "action": "speak" if saw_rag else "stop",
                "content": "report saw rag" if saw_rag else "",
                "reason": "summarize useful contribution" if saw_rag else "nothing to add",
            }

        async def gen():
            yield TextChunk(delta=json.dumps(decision, ensure_ascii=False))

        return gen()


async def _seed_llm_config():
    async with create_db_session() as session:
        session.add(
            LlmModelEntity(
                tenant_id=DEFAULT_TENANT_ID,
                base_url="http://example.test/v1",
                model="deepseek-v4-pro",
                model_name="deepseek-v4-pro",
                model_id="deepseek-v4-pro",
                encrypted_api_key=encrypt_key("test-key"),
                provider_name="openai_like",
                source="openai_like",
            )
        )


def test_discussion_run_uses_public_thread_messages(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'discussion.db'}")
        reset_engine_for_test()
        monkeypatch.setattr(runner_mod, "ReactAgent", DiscussionAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        await init_db()
        await _seed_llm_config()

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/scheduling/discussions/runs",
                json={
                    "topic": "讨论内部审计制度如何检索和写报告",
                    "participant_agent_ids": ["rag-agent", "code-agent", "report-agent"],
                    "moderator_agent_id": "report-agent",
                    "user_id": "discussion-test-user",
                    "max_rounds": 1,
                    "stream": False,
                },
            )
            assert response.status_code == 200
            body = response.json()
            run_id = body["run"]["id"]

            interjection = await client.post(
                f"/scheduling/discussions/runs/{run_id}/messages",
                json={"content": "补充：请优先考虑可验证依据", "user_id": "discussion-test-user"},
            )
            assert interjection.status_code == 200

            fetched = await client.get(f"/scheduling/discussions/runs/{run_id}")
            assert fetched.status_code == 200
            payload = fetched.json()

        assert payload["run"]["public_thread_id"]
        assert [item["agent_id"] for item in payload["participants"]] == [
            "rag-agent",
            "code-agent",
            "report-agent",
        ]

        turns = payload["turns"]
        assert [turn["action"] for turn in turns] == ["speak", "pass", "speak"]
        assert turns[0]["public_message_id"]
        assert turns[1]["public_message_id"]
        assert turns[2]["public_message_id"]
        assert turns[0]["response"]["choices"][0]["message"]["content"]
        assert turns[0]["steps"] == []
        assert turns[2]["skills"] is None

        messages = payload["messages"]
        public_texts = [message["text"] for message in messages]
        assert public_texts == [
            "讨论内部审计制度如何检索和写报告",
            "rag contribution",
            "no code needed",
            "report saw rag",
            "补充：请优先考虑可验证依据",
        ]
        assert messages[1]["discussion"]["speaker_id"] == "rag-agent"
        assert messages[1]["discussion"]["turn_id"] == turns[0]["id"]
        assert messages[2]["discussion"]["speaker_id"] == "code-agent"
        assert messages[2]["turn"]["action"] == "pass"
        assert messages[3]["turn"]["agent_id"] == "report-agent"

        for participant in payload["participants"]:
            await session_history_manager.clear_history(
                "discussion-test-user",
                participant["agent_session_id"],
            )

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


class InterjectionAwareAgent:
    def __init__(self, llm, system_prompt, tools):
        self.system_prompt = system_prompt

    async def run_async(self, state):
        user_text = state.messages[-1].get("content", "")
        saw_interjection = "优先考虑可验证依据" in user_text
        decision = {
            "action": "speak",
            "content": "interjection visible" if saw_interjection else "interjection missing",
            "reason": "checked public transcript",
        }

        async def gen():
            yield TextChunk(delta=json.dumps(decision, ensure_ascii=False))

        return gen()


def test_discussion_user_message_affects_later_agent_prompt(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'discussion-interjection.db'}")
        reset_engine_for_test()

        monkeypatch.setattr(runner_mod, "ReactAgent", InterjectionAwareAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        await init_db()
        await _seed_llm_config()

        service = DiscussionService(
            SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
        )
        run_entity = await service.create_run(
            DiscussionRunCreateRequest(
                topic="讨论如何写报告",
                participant_agent_ids=["report-agent"],
                moderator_agent_id="report-agent",
                user_id="discussion-interjection-user",
                max_rounds=1,
            )
        )
        await service.add_user_message(
            run_id=run_entity.id,
            content="补充：请优先考虑可验证依据",
            user_id="discussion-interjection-user",
        )
        payload = await service.execute_run(run_entity.id)

        assert [message["text"] for message in payload["messages"]] == [
            "讨论如何写报告",
            "补充：请优先考虑可验证依据",
            "interjection visible",
        ]

        participant = payload["participants"][0]
        await session_history_manager.clear_history(
            "discussion-interjection-user",
            participant["agent_session_id"],
        )

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


class RuntimeContextDiscussionAgent:
    system_prompts: list[str] = []

    def __init__(self, llm, system_prompt, tools):
        self.system_prompts.append(system_prompt)

    async def run_async(self, state):
        async def gen():
            yield TextChunk(
                delta=json.dumps(
                    {"action": "speak", "content": "context checked", "reason": "done"}
                )
            )

        return gen()


def test_discussion_runtime_context_is_internal_and_optional(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'discussion-context.db'}")
        reset_engine_for_test()
        monkeypatch.setattr(runner_mod, "ReactAgent", RuntimeContextDiscussionAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())
        RuntimeContextDiscussionAgent.system_prompts = []

        await init_db()
        await _seed_llm_config()
        options = SchedulingRuntimeOptions(local_python_artifact_dir=tmp_path / "artifacts")
        memory_context = SchedulingRuntimeContext(
            blocks=(
                RuntimeContextBlock(
                    kind="task_memory",
                    content="# Task Memory\nUse verified evidence first.",
                ),
            )
        )
        bound_service = DiscussionService(options, runtime_context=memory_context)
        bound_run = await bound_service.create_run(
            DiscussionRunCreateRequest(
                topic="bound discussion",
                participant_agent_ids=["report-agent"],
                moderator_agent_id="report-agent",
                user_id="bound-user",
            )
        )
        await bound_service.execute_run(bound_run.id)

        standalone_service = DiscussionService(options)
        standalone_run = await standalone_service.create_run(
            DiscussionRunCreateRequest(
                topic="standalone discussion",
                participant_agent_ids=["report-agent"],
                moderator_agent_id="report-agent",
                user_id="standalone-user",
            )
        )
        await standalone_service.execute_run(standalone_run.id)

        assert "Use verified evidence first" in RuntimeContextDiscussionAgent.system_prompts[0]
        assert "Use verified evidence first" not in RuntimeContextDiscussionAgent.system_prompts[1]

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()


def test_discussion_stream_wraps_single_agent_events(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'discussion-stream.db'}")
        reset_engine_for_test()
        monkeypatch.setattr(runner_mod, "ReactAgent", DiscussionAgent)
        monkeypatch.setattr(runner_mod, "create_llm", lambda config: object())

        await init_db()
        await _seed_llm_config()

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream(
                "POST",
                "/scheduling/discussions/runs",
                json={
                    "topic": "stream discussion",
                    "participant_agent_ids": ["rag-agent", "code-agent"],
                    "moderator_agent_id": "code-agent",
                    "user_id": "discussion-stream-user",
                    "max_rounds": 1,
                    "stream": True,
                },
            ) as response:
                assert response.status_code == 200
                body = await response.aread()

        text = body.decode()
        assert "event: discussion_started" in text
        assert "event: metadata" in text
        assert "event: chunk" in text
        assert "event: final" in text
        assert "event: turn_finished" in text
        assert "event: discussion_finished" in text
        assert '"agent_id": "rag-agent"' in text

    try:
        asyncio.run(run())
    finally:
        reset_engine_for_test()
