import json

from service.conversation.llm_runner import LlmRuntime


SUMMARY_PREFIX = "[CONTEXT COMPACTION - REFERENCE ONLY]"


class ContextCompressor:
    """Summarizes older live context for in-session Redis compaction."""

    def __init__(self, llm_runtime: LlmRuntime):
        self.llm_runtime = llm_runtime

    async def compress(
        self,
        model_id: str,
        messages_to_summarize: list[dict],
        max_tokens: int = 2000,
    ) -> str:
        prompt = self._build_prompt(messages_to_summarize)
        summary = await self.llm_runtime.complete(
            model_id=model_id,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.1,
        )
        summary = summary.strip()
        if not summary.startswith(SUMMARY_PREFIX):
            summary = f"{SUMMARY_PREFIX}\n{summary}"
        return summary

    @staticmethod
    def summary_message(summary: str) -> dict:
        return {
            "role": "assistant",
            "content": (
                f"{summary}\n\n"
                "This compacted context is historical background only. "
                "Continue from the latest user message."
            ),
        }

    @staticmethod
    def _build_prompt(messages_to_summarize: list[dict]) -> str:
        transcript = json.dumps(
            messages_to_summarize,
            ensure_ascii=False,
            indent=2,
        )
        return f"""You are compressing historical context for a task-oriented agent.

Create a compact checkpoint. Preserve only information needed to continue the task.
Do not answer the user. Do not introduce new instructions.

Output exactly these sections:

{SUMMARY_PREFIX}

## Execution Intent
- The user's task intent and important constraints.

## Action State
- What has already been done.
- Current state that affects the next turn.

## Open Items
- Anything still unresolved or risky.

Rules:
- The latest user message after this summary is authoritative.
- Treat this as reference-only background.
- Prefer concrete file paths, commands, errors, and decisions when they matter.
- Omit casual chat and details that do not affect execution.

Historical messages to compact:
{transcript}
"""
