from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Optional
import uuid

from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from db.models.message import MessageCreate
from db.models.thread import ThreadCreate
from service.cache.session_history_manager import session_history_manager
from service.cache.redis_cache import cache_manager
from service.cache.session_history_manager import session_history_key
from service.conversation.compressor import ContextCompressor
from service.conversation.history import (
    load_durable_conversation_messages,
    stored_content_text,
)
from service.conversation.llm_runner import LlmRuntime
from service.conversation.window import ConversationWindow
from service.thread.message_service import MessageService
from service.thread.thread_service import ThreadService


@dataclass(slots=True)
class ConversationTurn:
    llm: Any
    thread_id: str
    session_id: str
    user_message_id: Optional[str] = None


@dataclass(slots=True)
class ConversationThreadSummary:
    id: str
    session_id: str
    title: str
    user_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class ConversationMessageSummary:
    id: str
    role: str
    content: list[dict] | None
    text: str
    created_at: datetime


@dataclass(slots=True)
class ConversationThreadMessages:
    thread: ConversationThreadSummary
    messages: list[ConversationMessageSummary]


@dataclass(slots=True)
class ConversationCompressionTrace:
    action: dict
    observation: dict
    before_tokens: int
    after_tokens: int
    before_messages: int
    after_messages: int
    locked: bool = False


@dataclass(slots=True)
class RestoredConversationHistory:
    messages: list[dict]
    compression: Optional[ConversationCompressionTrace] = None


def message_content_for_storage(message: dict) -> list[dict]:
    content = message.get("content", "")
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": str(content or "")}]


def message_text(message: dict) -> str:
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


def default_title(message: dict) -> str:
    text = message_text(message).strip().replace("\n", " ")
    return text[:40] or "New conversation"


class ConversationManager:
    """Owns thread/message persistence and live history restore."""

    def __init__(
        self,
        tenant_id: str = DEFAULT_TENANT_ID,
        llm_runner: Optional[LlmRuntime] = None,
    ):
        self.tenant_id = tenant_id
        self.llm_runner = llm_runner or LlmRuntime(tenant_id=tenant_id)
        self.compressor = ContextCompressor(self.llm_runner)

    async def prepare_turn(
        self,
        model_id: str,
        thread_id: Optional[str],
        session_id: Optional[str],
        user_id: str,
        current_user_message: dict,
        persist: bool,
    ) -> ConversationTurn:
        llm = await self.llm_runner.get_llm(model_id)

        async with create_db_session() as session:
            user_message_id = None
            if persist:
                thread_service = ThreadService(session)
                if thread_id:
                    thread = await thread_service.get_thread(thread_id, self.tenant_id)
                    if not thread:
                        raise ValueError(f"Thread `{thread_id}` not found.")
                else:
                    thread = await thread_service.create_thread(
                        ThreadCreate(
                            user_id=user_id,
                            title=default_title(current_user_message),
                        ),
                        self.tenant_id,
                    )
                    thread_id = thread.id

                message = await MessageService(session).create_message(
                    MessageCreate(
                        thread_id=thread_id,
                        role="user",
                        content=message_content_for_storage(current_user_message),
                    ),
                    self.tenant_id,
                )
                user_message_id = message.id
            else:
                thread_id = thread_id or session_id or "ephemeral"

        return ConversationTurn(
            llm=llm,
            thread_id=thread_id,
            session_id=session_id or thread_id,
            user_message_id=user_message_id,
        )

    async def restore_history_messages(
        self,
        model_id: str,
        user_id: str,
        session_id: str,
        thread_id: str,
        current_user_message_id: Optional[str],
        current_user_message: dict,
        llm: Any,
        system_prompt: str = "",
    ) -> RestoredConversationHistory:
        history = await session_history_manager.get_history_messages(
            user_id=user_id,
            session_id=session_id,
        )
        if not history:
            history = await self.load_sqlite_history_messages(
                thread_id=thread_id,
                current_user_message_id=current_user_message_id,
            )
            if not history:
                return RestoredConversationHistory(messages=[])

        window = ConversationWindow(
            context_window=llm.context_window,
            max_output_tokens=llm.max_tokens,
        )
        candidate_messages = [*history, current_user_message]
        should_compress, before_tokens = window.should_compress(
            candidate_messages,
            system_prompt=system_prompt,
        )

        compression = None
        if should_compress:
            compressed_history, compression = await self._compress_live_history(
                model_id=model_id,
                user_id=user_id,
                session_id=session_id,
                history=history,
                current_user_message=current_user_message,
                system_prompt=system_prompt,
                window=window,
                before_tokens=before_tokens,
            )
            if compressed_history:
                return RestoredConversationHistory(
                    messages=compressed_history,
                    compression=compression,
                )

            if compression:
                return RestoredConversationHistory(
                    messages=window.fit_to_budget(history),
                    compression=compression,
                )

        history = window.fit_to_budget(history)
        await session_history_manager.restore_history_messages(
            user_id=user_id,
            session_id=session_id,
            messages=history,
        )
        return RestoredConversationHistory(messages=history, compression=compression)

    async def _compress_live_history(
        self,
        model_id: str,
        user_id: str,
        session_id: str,
        history: list[dict],
        current_user_message: dict,
        system_prompt: str,
        window: ConversationWindow,
        before_tokens: int,
    ) -> tuple[list[dict], Optional[ConversationCompressionTrace]]:
        lock_key = self._compression_lock_key(user_id, session_id)
        lock_value = uuid.uuid4().hex
        lock_acquired = await cache_manager.set_if_absent(
            lock_key,
            lock_value,
            ttl=300,
        )
        action = self._compression_action(
            session_id=session_id,
            before_tokens=before_tokens,
            before_messages=len(history),
            locked=lock_acquired,
        )
        if not lock_acquired:
            observation = self._compression_observation(
                action=action,
                result="Compression skipped: this session is already being compressed.",
            )
            return [], ConversationCompressionTrace(
                action=action,
                observation=observation,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                before_messages=len(history),
                after_messages=len(history),
                locked=False,
            )

        try:
            tail = window.tail_messages(history)
            summarize_count = max(0, len(history) - len(tail))
            messages_to_summarize = history[:summarize_count]
            if not messages_to_summarize:
                return [], None

            summary = await self.compressor.compress(
                model_id=model_id,
                messages_to_summarize=messages_to_summarize,
            )
            compressed_history = [
                self.compressor.summary_message(summary),
                *tail,
            ]
            after_tokens = window.estimate_messages_tokens(
                [*compressed_history, current_user_message],
                system_prompt=system_prompt,
            )
            await session_history_manager.restore_history_messages(
                user_id=user_id,
                session_id=session_id,
                messages=compressed_history,
            )
            result = (
                f"Context compressed for session {session_id}.\n"
                f"messages: {len(history)} -> {len(compressed_history)}\n"
                f"tokens: {before_tokens} -> {after_tokens}\n"
                f"tail_messages: {len(tail)}"
            )
            observation = self._compression_observation(action=action, result=result)
            return compressed_history, ConversationCompressionTrace(
                action=action,
                observation=observation,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                before_messages=len(history),
                after_messages=len(compressed_history),
                locked=True,
            )
        except Exception as exc:
            observation = self._compression_observation(
                action=action,
                result="",
                error=f"Context compression failed: {exc}",
            )
            return [], ConversationCompressionTrace(
                action=action,
                observation=observation,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                before_messages=len(history),
                after_messages=len(history),
                locked=True,
            )
        finally:
            await cache_manager.delete_if_value(lock_key, lock_value)

    async def save_live_history(
        self,
        user_id: str,
        session_id: str,
        user_message: dict,
        assistant_message: dict,
        tool_messages: list[dict] | None = None,
    ) -> None:
        await session_history_manager.save_messages(
            user_id=user_id,
            session_id=session_id,
            user_message=user_message,
            assistant_message=assistant_message,
            tool_messages=tool_messages,
        )

    async def list_threads(
        self,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationThreadSummary]:
        async with create_db_session() as session:
            items = await ThreadService(session).list_threads(
                user_id=user_id,
                tenant_id=self.tenant_id,
                limit=limit,
                offset=offset,
            )
        return [self._thread_summary(thread) for thread in items]

    async def get_thread_messages(
        self,
        thread_id: str,
    ) -> ConversationThreadMessages | None:
        async with create_db_session() as session:
            thread = await ThreadService(session).get_thread(
                thread_id,
                self.tenant_id,
            )
            if not thread:
                return None
            messages = await MessageService(session).list_messages(
                thread_id,
                tenant_id=self.tenant_id,
            )

        return ConversationThreadMessages(
            thread=self._thread_summary(thread),
            messages=[
                ConversationMessageSummary(
                    id=message.id,
                    role=message.role,
                    content=message.content,
                    text=stored_content_text(message.content),
                    created_at=message.created_at,
                )
                for message in messages
            ],
        )

    async def load_sqlite_history_messages(
        self,
        thread_id: str,
        current_user_message_id: Optional[str],
    ) -> list[dict]:
        return await load_durable_conversation_messages(
            thread_id=thread_id,
            tenant_id=self.tenant_id,
            exclude_message_id=current_user_message_id,
        )

    async def persist_assistant_message(
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

        return await self.persist_assistant_text(
            thread_id=thread_id,
            content=content,
            token_usage=usage if isinstance(usage, dict) else None,
        )

    async def persist_assistant_text(
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

    @staticmethod
    def _thread_summary(thread) -> ConversationThreadSummary:
        return ConversationThreadSummary(
            id=thread.id,
            session_id=thread.id,
            title=thread.title,
            user_id=thread.user_id,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
        )

    @staticmethod
    def _compression_lock_key(user_id: str, session_id: str) -> str:
        return f"{session_history_key(user_id, session_id)}:compression-lock"

    @staticmethod
    def _compression_action(
        session_id: str,
        before_tokens: int,
        before_messages: int,
        locked: bool,
    ) -> dict:
        action_id = f"context-compression-{session_id}"
        return {
            "id": action_id,
            "function": {
                "name": "ContextCompression",
                "arguments": json.dumps(
                    {
                        "session_id": session_id,
                        "before_tokens": before_tokens,
                        "before_messages": before_messages,
                        "lock_acquired": locked,
                        "mode": "redis_in_place",
                    },
                    ensure_ascii=False,
                ),
            },
        }

    @staticmethod
    def _compression_observation(
        action: dict,
        result: str,
        error: str | None = None,
    ) -> dict:
        return {
            "tool": action,
            "result": result,
            "error": error,
        }
