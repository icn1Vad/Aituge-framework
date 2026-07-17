from __future__ import annotations

from typing import Optional

from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from service.thread.message_service import MessageService
from service.thread.thread_service import ThreadService


def stored_content_text(content: list[dict] | None) -> str:
    if not content:
        return ""
    return "\n".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


async def load_durable_conversation_messages(
    *,
    thread_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    exclude_message_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> list[dict]:
    """Read the complete, uncompressed user/assistant transcript from SQL."""

    async with create_db_session() as session:
        if user_id is not None:
            thread = await ThreadService(session).get_thread(thread_id, tenant_id)
            if thread is None or thread.user_id != user_id:
                return []
        messages = await MessageService(session).list_messages(
            thread_id=thread_id,
            tenant_id=tenant_id,
        )

    history: list[dict] = []
    for message in messages:
        if message.id == exclude_message_id:
            continue
        if message.role not in {"user", "assistant"}:
            continue
        text = stored_content_text(message.content).strip()
        if not text:
            continue
        history.append({"role": message.role, "content": text})
    return history
