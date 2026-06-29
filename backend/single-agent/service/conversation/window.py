from agent.message_manager import AgentMessageManager


DEFAULT_COMPRESSION_TRIGGER_TOKENS = 100_000
DEFAULT_COMPRESSION_TAIL_ROUNDS = 2


class ConversationWindow:
    """Token accounting and tail selection for live conversation context."""

    def __init__(
        self,
        context_window: int,
        max_output_tokens: int,
        compression_trigger_tokens: int = DEFAULT_COMPRESSION_TRIGGER_TOKENS,
        compression_tail_rounds: int = DEFAULT_COMPRESSION_TAIL_ROUNDS,
    ):
        self.message_manager = AgentMessageManager(
            context_window=context_window,
            max_output_tokens=max_output_tokens,
        )
        self.compression_trigger_tokens = compression_trigger_tokens
        self.compression_tail_rounds = compression_tail_rounds

    def estimate_messages_tokens(
        self,
        messages: list[dict],
        system_prompt: str = "",
    ) -> int:
        estimated_messages = list(messages)
        if system_prompt:
            estimated_messages = [
                {"role": "system", "content": system_prompt},
                *estimated_messages,
            ]
        return self.message_manager.estimate_messages_tokens(estimated_messages)

    def should_compress(
        self,
        messages: list[dict],
        system_prompt: str = "",
    ) -> tuple[bool, int]:
        tokens = self.estimate_messages_tokens(messages, system_prompt=system_prompt)
        return tokens >= self.compression_trigger_tokens, tokens

    def tail_messages(self, messages: list[dict]) -> list[dict]:
        user_indices = [
            i for i, message in enumerate(messages)
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        if len(user_indices) <= self.compression_tail_rounds:
            return list(messages)
        return list(messages[user_indices[-self.compression_tail_rounds]:])

    def fit_to_budget(self, messages: list[dict]) -> list[dict]:
        return self.message_manager.fit_to_budget(messages)
