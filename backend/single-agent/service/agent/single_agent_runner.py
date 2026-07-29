import json
from typing import AsyncIterator, List, Optional, Sequence

from agent.prompts import REACT_PROMPT
from agent.react_agent import ReactAgent
from agent.state import AgentState
from common.llm.utils import (
    convert_gen_to_chat_completions,
    convert_gen_to_stream_chat_completions,
)
from common.llm.constants import DEFAULT_LLM_MODEL_ID
from common.system_constants import DEFAULT_TENANT_ID
from llama_index.core.tools.function_tool import FunctionTool
from loguru import logger
from pydantic import BaseModel
from service.conversation import ConversationManager, LlmRuntime, create_llm
from tool.artifacts import extract_artifacts


class SingleAgentChatResult(BaseModel):
    thread_id: str
    session_id: str
    user_message_id: Optional[str] = None
    assistant_message_id: Optional[str] = None
    response: dict


class SingleAgentStreamEvent(BaseModel):
    event: str
    thread_id: str
    session_id: str
    user_message_id: Optional[str] = None
    assistant_message_id: Optional[str] = None
    data: Optional[dict] = None


def build_tools_summary(tools: Sequence[FunctionTool]) -> str:
    if not tools:
        return (
            "No tools are available in this session. "
            "Answer the user's questions directly using your own knowledge. "
            "Ignore all tool-related instructions above."
        )

    lines = [
        f"You have {len(tools)} tool(s). For every user query, pick the most relevant tool(s) to call:\n"
    ]
    for tool in tools:
        name = tool.metadata.name
        desc = tool.metadata.description or ""
        first_line = desc.strip().split("\n")[0]
        lines.append(f"- **{name}**: {first_line}")
    lines.append("\nRemember: call at least one tool for any factual question.")
    return "\n".join(lines)


def _normalize_messages(
    messages: Optional[List[dict]] = None,
    user_message: str | dict | None = None,
) -> List[dict]:
    if messages:
        normalized = [dict(msg) for msg in messages]
    elif isinstance(user_message, str):
        normalized = [{"role": "user", "content": user_message}]
    elif isinstance(user_message, dict):
        normalized = [dict(user_message)]
    else:
        raise ValueError("Either messages or user_message must be provided.")

    if not normalized:
        raise ValueError("messages must not be empty.")
    if normalized[-1].get("role") != "user":
        raise ValueError("The last message must be a user message.")
    return normalized


class SingleAgentRunner:
    """Runs one single-agent chat turn without depending on an HTTP layer."""

    def __init__(
        self,
        tenant_id: str = DEFAULT_TENANT_ID,
        default_model_id: str | None = None,
        model_pack_id: str | None = None,
        system_prompt: str = REACT_PROMPT,
    ):
        self.tenant_id = tenant_id
        self.system_prompt = system_prompt
        llm_runtime = LlmRuntime(
            tenant_id=tenant_id,
            llm_factory=lambda config: create_llm(config),
            model_pack_id=model_pack_id,
        )
        self.default_model_id = (
            default_model_id or llm_runtime.model_runtime_provider.active_pack.llm.id
        )
        self.conversation = ConversationManager(
            tenant_id=tenant_id,
            llm_runner=llm_runtime,
        )

    async def chat(
        self,
        messages: Optional[List[dict]] = None,
        user_message: str | dict | None = None,
        tools: Optional[Sequence[FunctionTool]] = None,
        model_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: str = "default_user",
        persist: bool = True,
        prompts: Optional[dict[str, str]] = None,
        task_prompt: str = "",
    ) -> SingleAgentChatResult:
        runtime_messages = _normalize_messages(messages=messages, user_message=user_message)
        current_user_message = runtime_messages[-1]
        model_id = model_id or self.default_model_id
        tools = list(tools or [])

        turn = await self.conversation.prepare_turn(
            model_id=model_id,
            thread_id=thread_id,
            session_id=session_id,
            user_id=user_id,
            current_user_message=current_user_message,
            persist=persist,
        )
        llm = turn.llm
        thread_id = turn.thread_id
        session_id = turn.session_id
        user_message_id = turn.user_message_id

        system_prompt = (prompts or {}).get("react", self.system_prompt)
        system_prompt = system_prompt.format(
            tools_str=build_tools_summary(tools),
            context_str=task_prompt or "",
        )
        compression = None
        if session_id and user_id and len(runtime_messages) == 1:
            restored = await self.conversation.restore_history_messages(
                model_id=model_id,
                user_id=user_id,
                session_id=session_id,
                thread_id=thread_id,
                current_user_message_id=user_message_id,
                current_user_message=current_user_message,
                llm=llm,
                system_prompt=system_prompt,
            )
            history = restored.messages
            compression = restored.compression
            if history:
                runtime_messages = history + runtime_messages
                logger.info(
                    f"Restored {len(history)} history messages for "
                    f"user={user_id}, session={session_id}."
                )

        agent = ReactAgent(
            llm=llm,
            system_prompt=system_prompt,
            tools=tools,
        )
        agent_state = AgentState.from_messages(runtime_messages, max_rounds=0)
        response_gen = await agent.run_async(agent_state)
        response = await convert_gen_to_chat_completions(
            model=model_id,
            response_generator=response_gen,
            user_id=user_id,
            session_id=session_id,
            user_message=current_user_message,
        )
        if compression:
            response.setdefault("steps", [])
            response["steps"].append(compression.observation)

        assistant_message_id = None
        if persist:
            assistant_message_id = await self.conversation.persist_assistant_message(
                thread_id=thread_id,
                response=response,
            )

        return SingleAgentChatResult(
            thread_id=thread_id,
            session_id=session_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            response=response,
        )

    async def stream_chat(
        self,
        messages: Optional[List[dict]] = None,
        user_message: str | dict | None = None,
        tools: Optional[Sequence[FunctionTool]] = None,
        model_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: str = "default_user",
        persist: bool = True,
        prompts: Optional[dict[str, str]] = None,
        task_prompt: str = "",
    ) -> AsyncIterator[SingleAgentStreamEvent]:
        runtime_messages = _normalize_messages(messages=messages, user_message=user_message)
        current_user_message = runtime_messages[-1]
        model_id = model_id or self.default_model_id
        tools = list(tools or [])

        turn = await self.conversation.prepare_turn(
            model_id=model_id,
            thread_id=thread_id,
            session_id=session_id,
            user_id=user_id,
            current_user_message=current_user_message,
            persist=persist,
        )
        llm = turn.llm
        thread_id = turn.thread_id
        session_id = turn.session_id
        user_message_id = turn.user_message_id

        system_prompt = (prompts or {}).get("react", self.system_prompt)
        system_prompt = system_prompt.format(
            tools_str=build_tools_summary(tools),
            context_str=task_prompt or "",
        )
        compression = None
        yield SingleAgentStreamEvent(
            event="metadata",
            thread_id=thread_id,
            session_id=session_id,
            user_message_id=user_message_id,
        )

        if session_id and user_id and len(runtime_messages) == 1:
            restored = await self.conversation.restore_history_messages(
                model_id=model_id,
                user_id=user_id,
                session_id=session_id,
                thread_id=thread_id,
                current_user_message_id=user_message_id,
                current_user_message=current_user_message,
                llm=llm,
                system_prompt=system_prompt,
            )
            history = restored.messages
            compression = restored.compression
            if history:
                runtime_messages = history + runtime_messages
                logger.info(
                    f"Restored {len(history)} history messages for "
                    f"user={user_id}, session={session_id}."
                )

        if compression:
            yield SingleAgentStreamEvent(
                event="chunk",
                thread_id=thread_id,
                session_id=session_id,
                user_message_id=user_message_id,
                data={"actions": [compression.action]},
            )
            yield SingleAgentStreamEvent(
                event="chunk",
                thread_id=thread_id,
                session_id=session_id,
                user_message_id=user_message_id,
                data={"observation": compression.observation},
            )

        agent = ReactAgent(
            llm=llm,
            system_prompt=system_prompt,
            tools=tools,
        )
        agent_state = AgentState.from_messages(runtime_messages, max_rounds=0)
        response_gen = await agent.run_async(agent_state)

        assistant_content = ""
        assistant_attachments: list[dict[str, str]] = []
        artifact_ids: set[str] = set()
        token_usage = None
        async for chunk_json in convert_gen_to_stream_chat_completions(
            model=model_id,
            response_generator=response_gen,
            user_id=user_id,
            session_id=session_id,
            user_message=current_user_message,
        ):
            chunk_data = json.loads(chunk_json)
            token_usage = chunk_data.get("usage") or token_usage
            choices = chunk_data.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                assistant_content += delta.get("content") or ""
            observation = chunk_data.get("observation")
            if isinstance(observation, dict):
                for artifact in extract_artifacts(observation.get("result")):
                    if artifact["id"] in artifact_ids:
                        continue
                    artifact_ids.add(artifact["id"])
                    assistant_attachments.append(artifact)

            yield SingleAgentStreamEvent(
                event="chunk",
                thread_id=thread_id,
                session_id=session_id,
                user_message_id=user_message_id,
                data=chunk_data,
            )

        assistant_message_id = None
        if persist:
            assistant_message_id = await self.conversation.persist_assistant_text(
                thread_id=thread_id,
                content=assistant_content,
                token_usage=token_usage,
                attachments=assistant_attachments,
            )

        yield SingleAgentStreamEvent(
            event="final",
            thread_id=thread_id,
            session_id=session_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            data={
                "content": assistant_content,
                "usage": token_usage,
                "artifacts": assistant_attachments,
            },
        )
