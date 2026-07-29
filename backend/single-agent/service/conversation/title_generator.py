from __future__ import annotations

import json

from common.chat.prompts import DEFAULT_TITLE_GENERATION_PROMPT_TEMPLATE
from service.conversation.llm_runner import LlmRuntime


MAX_CONVERSATION_TITLE_CHARS = 10


class ConversationTitleGenerator:
    """Generates a short title without entering the agent or tool loop."""

    def __init__(self, llm_runtime: LlmRuntime):
        self.llm_runtime = llm_runtime

    async def generate(self, question: str, *, trace_id: str | None = None) -> str:
        active_model_id = (
            self.llm_runtime.model_runtime_provider.active_pack.llm.id
        )
        completion = await self.llm_runtime.complete_with_usage(
            messages=[{"role": "user", "content": question}],
            model_id=active_model_id,
            system_prompt=DEFAULT_TITLE_GENERATION_PROMPT_TEMPLATE,
            max_tokens=64,
            temperature=0,
            thinking_override=False,
            response_format={"type": "json_object"},
            review_unit_id="conversation_title",
            trace_id=trace_id,
        )
        return normalize_conversation_title(completion.content)


def normalize_conversation_title(content: str) -> str:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Title model returned invalid JSON.") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("title"), str):
        raise ValueError("Title model response must contain a string title.")

    title = payload["title"].strip()
    if not title or "\n" in title or "\r" in title:
        raise ValueError("Title model returned an empty or multiline title.")

    title = title[:MAX_CONVERSATION_TITLE_CHARS]
    if not title:
        raise ValueError("Title model returned an empty title.")
    return title
