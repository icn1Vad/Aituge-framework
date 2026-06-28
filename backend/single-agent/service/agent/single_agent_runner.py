import json
from typing import Any, AsyncIterator, List, Optional, Sequence

from agent.prompts import REACT_PROMPT
from agent.react_agent import ReactAgent
from agent.state import AgentState
from common.encrypt_utils import decrypt_key
from common.llm.llm_model import PaiLlm
from common.llm.utils import (
    convert_gen_to_chat_completions,
    convert_gen_to_stream_chat_completions,
)
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from db.models.llm import LlmModelEntity
from db.models.message import MessageCreate
from db.models.thread import ThreadCreate
from llama_index.core.tools.function_tool import FunctionTool
from loguru import logger
from pydantic import BaseModel
from service.cache.session_history_manager import session_history_manager
from service.model.llm_service import LlmService
from service.thread.message_service import MessageService
from service.thread.thread_service import ThreadService
from utils.lru_cache import LruCache


llm_cache = LruCache(max_size=20)


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


def _llm_cache_key(config: LlmModelEntity) -> str:
    return (
        f"llm:{config.base_url}:{config.encrypted_api_key}:"
        f"{config.model}:{config.enable_thinking}:{config.vision_support}:"
        f"{config.temperature}:{config.context_window}:{config.max_tokens}"
    )


def create_llm(config: LlmModelEntity) -> PaiLlm:
    cache_key = _llm_cache_key(config)
    cached = llm_cache.get(cache_key)
    if cached:
        logger.info(f"Using cached LLM: model_id={config.model_id}")
        return cached

    llm = PaiLlm(
        api_base=config.base_url,
        api_key=decrypt_key(config.encrypted_api_key),
        model=config.model or config.model_name or config.model_id,
        enable_thinking=config.enable_thinking,
        vision_support=config.vision_support,
        temperature=config.temperature,
        context_window=config.context_window,
        max_tokens=config.max_tokens,
    )
    llm_cache.put(cache_key, llm)
    return llm


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


def _message_content_for_storage(message: dict) -> List[dict]:
    content = message.get("content", "")
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": str(content or "")}]


def _message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def _default_title(message: dict) -> str:
    text = _message_text(message).strip().replace("\n", " ")
    return text[:40] or "New conversation"


class SingleAgentRunner:
    """Runs one single-agent chat turn without depending on an HTTP layer."""

    def __init__(
        self,
        tenant_id: str = DEFAULT_TENANT_ID,
        default_model_id: str = "deepseek-v4-pro",
        system_prompt: str = REACT_PROMPT,
    ):
        self.tenant_id = tenant_id
        self.default_model_id = default_model_id
        self.system_prompt = system_prompt

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
    ) -> SingleAgentChatResult:
        runtime_messages = _normalize_messages(messages=messages, user_message=user_message)
        current_user_message = runtime_messages[-1]
        model_id = model_id or self.default_model_id
        tools = list(tools or [])

        llm, thread_id, session_id, user_message_id = await self._prepare_turn(
            model_id=model_id,
            thread_id=thread_id,
            session_id=session_id,
            user_id=user_id,
            current_user_message=current_user_message,
            persist=persist,
        )

        if session_id and user_id and len(runtime_messages) == 1:
            history = await session_history_manager.get_history_messages(
                user_id=user_id,
                session_id=session_id,
            )
            if history:
                runtime_messages = history + runtime_messages
                logger.info(
                    f"Restored {len(history)} Redis history messages for "
                    f"user={user_id}, session={session_id}."
                )

        system_prompt = (prompts or {}).get("react", self.system_prompt)
        system_prompt = system_prompt.format(
            tools_str=build_tools_summary(tools),
            context_str="",
        )

        agent = ReactAgent(
            llm=llm,
            system_prompt=system_prompt,
            tools=tools,
        )
        agent_state = AgentState.from_messages(runtime_messages)
        response_gen = await agent.run_async(agent_state)
        response = await convert_gen_to_chat_completions(
            model=model_id,
            response_generator=response_gen,
            user_id=user_id,
            session_id=session_id,
            user_message=current_user_message,
        )

        assistant_message_id = None
        if persist:
            assistant_message_id = await self._persist_assistant_message(
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
    ) -> AsyncIterator[SingleAgentStreamEvent]:
        runtime_messages = _normalize_messages(messages=messages, user_message=user_message)
        current_user_message = runtime_messages[-1]
        model_id = model_id or self.default_model_id
        tools = list(tools or [])

        llm, thread_id, session_id, user_message_id = await self._prepare_turn(
            model_id=model_id,
            thread_id=thread_id,
            session_id=session_id,
            user_id=user_id,
            current_user_message=current_user_message,
            persist=persist,
        )

        yield SingleAgentStreamEvent(
            event="metadata",
            thread_id=thread_id,
            session_id=session_id,
            user_message_id=user_message_id,
        )

        if session_id and user_id and len(runtime_messages) == 1:
            history = await session_history_manager.get_history_messages(
                user_id=user_id,
                session_id=session_id,
            )
            if history:
                runtime_messages = history + runtime_messages
                logger.info(
                    f"Restored {len(history)} Redis history messages for "
                    f"user={user_id}, session={session_id}."
                )

        system_prompt = (prompts or {}).get("react", self.system_prompt)
        system_prompt = system_prompt.format(
            tools_str=build_tools_summary(tools),
            context_str="",
        )

        agent = ReactAgent(
            llm=llm,
            system_prompt=system_prompt,
            tools=tools,
        )
        agent_state = AgentState.from_messages(runtime_messages)
        response_gen = await agent.run_async(agent_state)

        assistant_content = ""
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

            yield SingleAgentStreamEvent(
                event="chunk",
                thread_id=thread_id,
                session_id=session_id,
                user_message_id=user_message_id,
                data=chunk_data,
            )

        assistant_message_id = None
        if persist:
            assistant_message_id = await self._persist_assistant_text(
                thread_id=thread_id,
                content=assistant_content,
                token_usage=token_usage,
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
            },
        )

    async def _prepare_turn(
        self,
        model_id: str,
        thread_id: Optional[str],
        session_id: Optional[str],
        user_id: str,
        current_user_message: dict,
        persist: bool,
    ) -> tuple[PaiLlm, str, str, Optional[str]]:
        async with create_db_session() as session:
            llm_model = await LlmService(session).get_llm_by_model_id(
                model_id=model_id,
                tenant_id=self.tenant_id,
            )
            if not llm_model:
                raise ValueError(f"LLM model `{model_id}` not found.")
            llm = create_llm(llm_model)

            user_message_id = None
            if persist:
                thread_service = ThreadService(session)
                if thread_id:
                    thread = await thread_service.get_thread(thread_id, self.tenant_id)
                    if not thread:
                        raise ValueError(f"Thread `{thread_id}` not found.")
                else:
                    thread = await thread_service.create_thread(
                        ThreadCreate(user_id=user_id, title=_default_title(current_user_message)),
                        self.tenant_id,
                    )
                    thread_id = thread.id

                message = await MessageService(session).create_message(
                    MessageCreate(
                        thread_id=thread_id,
                        role="user",
                        content=_message_content_for_storage(current_user_message),
                    ),
                    self.tenant_id,
                )
                user_message_id = message.id
            else:
                thread_id = thread_id or session_id or "ephemeral"

        return llm, thread_id, session_id or thread_id, user_message_id

    async def _persist_assistant_message(
        self,
        thread_id: str,
        response: dict,
    ) -> Optional[str]:
        choices = response.get("choices") or []
        if not choices:
            return None

        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        usage: Any = response.get("usage")

        return await self._persist_assistant_text(
            thread_id=thread_id,
            content=content,
            token_usage=usage if isinstance(usage, dict) else None,
        )

    async def _persist_assistant_text(
        self,
        thread_id: str,
        content: str,
        token_usage: Optional[dict] = None,
    ) -> Optional[str]:
        async with create_db_session() as session:
            stored = await MessageService(session).create_message(
                MessageCreate(
                    thread_id=thread_id,
                    role="assistant",
                    content=[{"type": "text", "text": content}],
                    token_usage=token_usage,
                ),
                self.tenant_id,
            )
            return stored.id
