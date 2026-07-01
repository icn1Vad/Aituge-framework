from pathlib import Path
from typing import Any, List, Tuple

from llama_index.core.base.llms.types import ChatMessage, MessageRole
from transformers import AutoTokenizer


TOKENIZATION_MODEL = str(
    Path(__file__).resolve().parent.parent
    / "resources"
    / "tokenizer"
    / "Qwen3-32B-Tokenizer"
)


def get_tokenizer():
    tokenizer = AutoTokenizer.from_pretrained(TOKENIZATION_MODEL, local_files_only=True, use_fast=True)
    return tokenizer


def estimate_tokens_in_text(
    text: str,
    return_offsets_mapping: bool = True,
    tokenizer: Any = None,
) -> int:
    if not text:
        return 0
    tokenizer = tokenizer or get_tokenizer()
    result = tokenizer(text, return_offsets_mapping=return_offsets_mapping, return_attention_mask=False, add_special_tokens=False)
    token_ids = result["input_ids"]
    return len(token_ids)


def truncate(
    text: str,
    max_token: int,
    start_token: int = 0,
    tokenizer: Any = None,
) -> Tuple[str, int]:
    if not text:
        return text, 0
    if max_token <= start_token:
        return "", 0
    assert start_token >= 0, "start_token must be >= 0"

    tokenizer = tokenizer or get_tokenizer()
    result = tokenizer(text, return_offsets_mapping=True)
    token_ids, offset_mapping = result["input_ids"], result["offset_mapping"]
    start_token_offset_mapping_left = offset_mapping[start_token][0]
    if max_token > len(token_ids):
        text = text[start_token_offset_mapping_left:]
        return text, len(token_ids[start_token:])
    last_token_offset_mapping_right = offset_mapping[max_token - 1][1]
    text = text[start_token_offset_mapping_left:last_token_offset_mapping_right]
    return text, max_token - start_token


def get_message_context(msg: ChatMessage) -> str:
    if not msg.content:
        return ""
    if isinstance(msg.content, str):
        return msg.content
    text = []
    for item in msg.content:
        if not item.text:
            return None
        text.append(item.text)
    return "\n".join(text)


def estimate_tokens_in_message(message: ChatMessage, tokenizer: Any = None) -> int:
    tokens = 0

    if message.role:
        tokens += estimate_tokens_in_text(message.role, tokenizer)

    text = get_message_context(message)
    tokens += estimate_tokens_in_text(text, tokenizer)

    additional_kwargs = {**message.additional_kwargs}

    if "tool_calls" in additional_kwargs:
        for tool_call in additional_kwargs["tool_calls"]:
            tokens += estimate_tokens_in_text(str(tool_call), tokenizer)

    return tokens


def get_last_n_msgs_skip_first(msgs: List[ChatMessage], n) -> List[ChatMessage]:
    last_n_messages = msgs[1:][-n:]
    if last_n_messages and last_n_messages[0].role == MessageRole.TOOL:
        return last_n_messages[1:]
    return last_n_messages
