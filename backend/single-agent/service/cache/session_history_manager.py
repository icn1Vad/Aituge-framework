import json
import re
from typing import List

from loguru import logger
from openai.types.chat import ChatCompletionMessageParam
from service.cache.redis_cache import cache_manager


_SYS_TIME_PREFIX_RE = re.compile(r"^\[System Time:[^\]]*\]\n")


def _clean_user_message(msg):
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        cleaned = _SYS_TIME_PREFIX_RE.sub("", msg["content"])
        if cleaned != msg["content"]:
            return {**msg, "content": cleaned}
    return msg


def session_history_key(user_id: str, session_id: str) -> str:
    return f"tuge:session:uid:{user_id}:sid:{session_id}"


class SessionHistoryManager:
    MAX_HISTORY_ROUNDS = 5
    MAX_HISTORY_MESSAGES = 40
    TTL_SECONDS = 7 * 24 * 60 * 60

    async def save_messages(
        self,
        user_id: str,
        session_id: str,
        user_message: ChatCompletionMessageParam,
        assistant_message: ChatCompletionMessageParam,
        tool_messages: List[ChatCompletionMessageParam] | None = None,
    ) -> None:
        if not user_id or not session_id:
            logger.debug("Skip saving session history without user_id/session_id.")
            return

        try:
            key = session_history_key(user_id, session_id)
            history = await self._get_history_list(key)
            history.append(_clean_user_message(user_message))
            history.append(assistant_message)
            history = self._trim_to_rounds(history)
            await cache_manager.set(
                key,
                json.dumps(history, ensure_ascii=False),
                ttl=self.TTL_SECONDS,
            )
            logger.info(
                f"Saved TUGE session history: user={user_id}, "
                f"session={session_id}, messages={len(history)}"
            )
        except Exception as e:
            logger.error(f"Failed to save TUGE session history: {e}", exc_info=True)

    async def get_history_messages(
        self,
        user_id: str,
        session_id: str,
    ) -> List[ChatCompletionMessageParam]:
        if not user_id or not session_id:
            return []

        try:
            return await self._get_history_list(session_history_key(user_id, session_id))
        except Exception as e:
            logger.error(f"Failed to load TUGE session history: {e}", exc_info=True)
            return []

    async def restore_history_messages(
        self,
        user_id: str,
        session_id: str,
        messages: List[ChatCompletionMessageParam],
    ) -> None:
        if not user_id or not session_id or not messages:
            return

        try:
            history = self._trim_to_rounds([_clean_user_message(msg) for msg in messages])
            await cache_manager.set(
                session_history_key(user_id, session_id),
                json.dumps(history, ensure_ascii=False),
                ttl=self.TTL_SECONDS,
            )
            logger.info(
                f"Restored TUGE session history to Redis: user={user_id}, "
                f"session={session_id}, messages={len(history)}"
            )
        except Exception as e:
            logger.error(f"Failed to restore TUGE session history: {e}", exc_info=True)

    async def clear_history(self, user_id: str, session_id: str, model: str | None = None) -> None:
        if not user_id or not session_id:
            return

        try:
            await cache_manager.delete(session_history_key(user_id, session_id))
            logger.info(f"Cleared TUGE session history: user={user_id}, session={session_id}")
        except Exception as e:
            logger.error(f"Failed to clear TUGE session history: {e}", exc_info=True)

    async def _get_history_list(self, key: str) -> List[ChatCompletionMessageParam]:
        history_json = await cache_manager.get(key)
        if not history_json:
            return []
        try:
            return json.loads(history_json)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode TUGE session history JSON: {e}")
            return []

    @classmethod
    def _trim_to_rounds(cls, messages: List[ChatCompletionMessageParam]) -> List[ChatCompletionMessageParam]:
        user_indices = [
            i for i, m in enumerate(messages)
            if isinstance(m, dict) and m.get("role") == "user"
        ]
        if len(user_indices) > cls.MAX_HISTORY_ROUNDS:
            messages = messages[user_indices[-cls.MAX_HISTORY_ROUNDS]:]

        while len(messages) > cls.MAX_HISTORY_MESSAGES:
            user_indices = [
                i for i, m in enumerate(messages)
                if isinstance(m, dict) and m.get("role") == "user"
            ]
            if len(user_indices) <= 1:
                break
            messages = messages[user_indices[1]:]

        return messages


session_history_manager = SessionHistoryManager()
