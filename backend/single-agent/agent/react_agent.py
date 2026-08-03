import asyncio
import json
import re
import uuid
from typing import Dict, Optional, Tuple
from common.llm.models import ErrorChunk, ReasoningChunk, ToolResultChunk, TextChunk
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall
from utils.time_utils import get_current_time_str
from extensions.trace.pai_agent_wrapper import pai_agent_wrapper
from loguru import logger
from tenacity import RetryError, retry, stop_after_attempt, wait_fixed
from utils.constants import try_get_int_env
from agent.state import AgentState
from llama_index.core.tools.function_tool import FunctionTool, ToolOutput
from common.llm.llm_model import PaiLlm, ChatResponseGenerator
from service.conversation.llm_runner import LlmRuntime
from extensions.trace.base import use_current_span
from opentelemetry import trace
from utils.json_utils import parse_tool_arguments
from agent.tool_utils import check_and_handle_return_direct
from agent.message_manager import AgentMessageManager

MAX_RECURSION_STEPS = try_get_int_env("MAX_RECURSION_STEPS", 20) # 最大循环步数
# 流式调用的"空闲超时":超过该秒数没有收到任何分片(package)即超时(非总时长)
LLM_STREAM_IDLE_TIMEOUT = try_get_int_env("LLM_STREAM_IDLE_TIMEOUT_SECONDS", 30)


async def _iter_with_idle_timeout(stream, timeout: int):
    """Yield chunks from a streaming response, raising ``asyncio.TimeoutError`` if
    no chunk arrives within ``timeout`` seconds. This is an idle/inter-chunk
    timeout (resets on every chunk), NOT a total-duration budget."""
    iterator = stream.__aiter__()
    while True:
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return

        yield chunk

async def _finalize_success(finalizer) -> None:
    await _finalize_observation(finalizer, "SUCCESS")


async def _finalize_validation_failed(finalizer, code: str) -> None:
    await _finalize_observation(finalizer, "VALIDATION_FAILED", code)


async def _finalize_observation(
    finalizer,
    decision: str,
    code: str | None = None,
) -> None:
    if finalizer is None:
        return
    try:
        if decision == "SUCCESS":
            await finalizer.succeed()
        else:
            await finalizer.validation_failed(code)
    except Exception as exc:
        conflict_code = getattr(exc, "code", None)
        if (
            conflict_code
            in {
                "MODEL_INVOCATION_DUPLICATE_ATTEMPT",
                "MODEL_INVOCATION_IDENTITY_CONFLICT",
                "MODEL_INVOCATION_LOGICAL_CALL_CONFLICT",
            }
            or str(exc).startswith("model invocation already finalized as ")
        ):
            raise
        logger.warning(
            "Model invocation finalize degraded: decision={}, error_type={}",
            decision,
            exc.__class__.__name__,
        )


async def _close_async_stream(stream) -> None:
    close = getattr(stream, "aclose", None)
    if not callable(close):
        return
    try:
        await close()
    except (GeneratorExit, asyncio.CancelledError):
        raise
    except Exception as exc:
        logger.warning(
            "Model stream close degraded: error_type={}",
            exc.__class__.__name__,
        )


def _validate_tool_calls(
    tool_calls: list[ChoiceDeltaToolCall],
    tool_fn_map: Dict[str, FunctionTool],
) -> tuple[list[ChoiceDeltaToolCall], str | None]:
    if not tool_calls:
        return [], None

    def reject_duplicate_keys(pairs):
        parsed: dict = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError("duplicate JSON object key")
            parsed[key] = value
        return parsed

    def reject_json_constant(_value):
        raise ValueError("non-finite JSON number")

    seen_ids: set[str] = set()
    seen_indexes: set[int] = set()
    validated: list[ChoiceDeltaToolCall] = []
    for tool_call in tool_calls:
        index = getattr(tool_call, "index", None)
        tool_id = getattr(tool_call, "id", None)
        function = getattr(tool_call, "function", None)
        name = getattr(function, "name", None)
        arguments = getattr(function, "arguments", None)
        if (
            getattr(tool_call, "type", None) != "function"
            or isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index in seen_indexes
            or not isinstance(tool_id, str)
            or not tool_id
            or len(tool_id) > 255
            or tool_id.strip() != tool_id
            or any(ord(char) < 33 or ord(char) == 127 for char in tool_id)
            or tool_id in seen_ids
            or not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None
        ):
            return [], "MODEL_TOOL_CALL_ENVELOPE_INVALID"
        if name not in tool_fn_map:
            return [], "MODEL_TOOL_CALL_UNKNOWN"
        if not isinstance(arguments, str) or not arguments.strip():
            return [], "MODEL_TOOL_ARGUMENTS_INVALID"
        try:
            parsed = json.loads(
                arguments,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_json_constant,
            )
        except (TypeError, ValueError):
            return [], "MODEL_TOOL_ARGUMENTS_INVALID"
        if not isinstance(parsed, dict):
            return [], "MODEL_TOOL_ARGUMENTS_INVALID"

        schema = getattr(tool_fn_map[name].metadata, "fn_schema", None)
        if schema is not None:
            try:
                fields = getattr(schema, "model_fields", None)
                if fields is None:
                    fields = getattr(schema, "__fields__", None)
                if fields is not None:
                    allowed_names = set(fields)
                    for field in fields.values():
                        alias = getattr(field, "alias", None)
                        if isinstance(alias, str):
                            allowed_names.add(alias)
                    if not set(parsed).issubset(allowed_names):
                        raise ValueError("unknown tool argument")
                if hasattr(schema, "model_validate"):
                    schema.model_validate(parsed, strict=True)
                elif hasattr(schema, "parse_obj"):
                    schema.parse_obj(parsed)
                else:
                    schema(**parsed)
            except Exception:
                return [], "MODEL_TOOL_ARGUMENTS_INVALID"

        seen_indexes.add(index)
        seen_ids.add(tool_id)
        validated.append(tool_call)
    return validated, None

# 当模型只"预告"下一步动作却没产生 tool_call 时,最多纠正(轻推)几次
MAX_INTENT_NUDGES = try_get_int_env("MAX_INTENT_NUDGES", 1)
_INTENT_NUDGE_MSG = (
    "你刚才只描述了下一步,但没有真正执行。请在本次响应中**直接调用合适的工具**,"
    "或者直接给出最终答案。不要再预告或描述将要调用的工具。"
)
_OUTPUT_REPAIR_MSG = (
    "上一条模型输出未通过结构校验。请重新生成：如需工具，必须调用已提供的工具并给出"
    "严格 JSON 参数；否则直接给出非空最终答案。"
)
# 行动预告的常见措辞(中英),用于识别"只说不做"的悬空消息。
# 刻意只保留"明显要去用工具"的短语,避免误伤正常答案(去掉了"接下来/下一步/我将"等宽泛词)。
_INTENT_PHRASES = (
    "让我搜索", "让我查", "让我继续", "让我再", "让我先",
    "我需要查找", "我需要搜索", "我来搜", "我来查", "继续搜索",
    "let me search", "let me look", "let me find",
    "i'll search", "i will search", "i need to search", "i need to find", "i'll look",
)


# 一个"行动预告"必然是短消息;超过这个长度就当作正常正文,不再扣留/纠正。
_INTENT_MAX_LEN = 200


def _looks_like_unfinished_intent(text: str) -> bool:
    """A short assistant message that only announces a next action (no tool call)."""
    if not text:
        return False
    t = text.strip().lower()
    return len(text) < _INTENT_MAX_LEN and any(p in t for p in _INTENT_PHRASES)


@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
async def call_tool_with_retry(async_fn, fn_args) -> ToolOutput:
    from extensions.trace.pai_agent_wrapper import instrument_async_call
    return await instrument_async_call(async_fn, fn_args)


async def execute_single_tool_call(
    tool_call: ChoiceDeltaToolCall,
    tool_fn_map: Dict[str, FunctionTool],
) -> Tuple[ChoiceDeltaToolCall, Optional[str], Optional[str], str]:
    """Execute a single tool call and return results.

    Returns:
        Tuple of (tool_call, tool_content, tool_error, message_content)
    """
    function_name = tool_call.function.name

    if not function_name or function_name not in tool_fn_map:
        logger.warning("Unknown tool requested; skipping.")
        return (tool_call, None, "Unknown tool.", "Unknown tool.")

    # Parse tool arguments
    function_args = parse_tool_arguments(
        tool_call.function.arguments,
    )

    # Execute tool with retry
    async_fn = tool_fn_map[function_name]
    logger.info("Calling tool: tool_name={}", function_name)

    try:
        tool_result = await call_tool_with_retry(async_fn, function_args)
        tool_content = tool_result.content
        tool_error = None
        message_content = tool_content
    except RetryError as retry_err:
        inner_exception = retry_err.last_attempt.exception()
        logger.error(
            "Tool call failed after retries: tool_name={}, error_type={}",
            function_name,
            (
                inner_exception.__class__.__name__
                if inner_exception is not None
                else "UnknownError"
            ),
        )
        tool_content = None
        tool_error = "Tool call failed after retries."
        message_content = tool_error
    except Exception as ex:
        logger.error(
            "Tool call failed: tool_name={}, error_type={}",
            function_name,
            ex.__class__.__name__,
        )
        tool_content = None
        tool_error = "Tool call failed."
        message_content = tool_error

    return (tool_call, tool_content, tool_error, message_content)


class ReactAgent:
    """A simplified ReAct agent that manages its own message state and tool execution loop."""

    def __init__(
        self,
        llm: PaiLlm,
        system_prompt: str,
        tools: list[FunctionTool],
        llm_runtime: LlmRuntime | None = None,
        max_steps: int = MAX_RECURSION_STEPS,
        model_id: str | None = None,
    ):
        self.llm = llm
        self.llm_runtime = llm_runtime
        self.system_prompt = system_prompt
        self.model_id = model_id
        self.max_steps = max_steps

        self.tools = tools
        self.tool_fn_map = {tool.metadata.name: tool for tool in self.tools}
        self.tool_metadata = [
            tool.metadata.to_openai_tool(skip_length_check=True) for tool in self.tools
        ]
        self.msg_manager = AgentMessageManager(
            context_window=llm.context_window,
            max_output_tokens=llm.max_tokens,
        )


    @pai_agent_wrapper
    async def run_async(self, state: AgentState) -> ChatResponseGenerator:
        """Execute the ReAct loop with tool calling until completion or max_steps."""
        logger.info("Starting ReAct agent run.")

        @use_current_span(trace.get_current_span())
        async def gen():
            messages = state.messages.copy()
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") != "user":
                    continue
                time_prefix = f"[System Time: {get_current_time_str()}]\n"
                content = messages[i].get("content", "")
                if isinstance(content, str):
                    messages[i]["content"] = time_prefix + content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            block["text"] = time_prefix + (block.get("text") or "")
                            break
                    else:
                        content.insert(0, {"type": "text", "text": time_prefix})
                break
            messages = [{"role": "system", "content": self.system_prompt}] + messages

            tools_to_use = self.tool_metadata.copy()
            react_step = 1
            nudge_count = 0
            repair_logical_call_id: str | None = None
            repair_attempt_no = 1
            repair_fallback_id: str | None = None
            active_finalizer = None

            try:
                while react_step <= self.max_steps:
                    logger.info(
                        "ReAct step {} / {}",
                        react_step,
                        self.max_steps,
                    )
                    if repair_logical_call_id is None:
                        logical_call_id = f"logical_{uuid.uuid4().hex}"
                        physical_attempt_no = 1
                        fallback_from_invocation_id = None
                    else:
                        logical_call_id = repair_logical_call_id
                        physical_attempt_no = repair_attempt_no
                        fallback_from_invocation_id = repair_fallback_id
                    react_step += 1

                    tool_calls: list[ChoiceDeltaToolCall] = []
                    step_content = ""
                    step_reasoning_content = ""
                    finish_reasons: set[str] = set()
                    buffering = True
                    pending = ""
                    pending_usage = None
                    step_finalizer = None
                    stream = None

                    messages = self.msg_manager.fit_to_budget(messages)
                    try:
                        if self.llm_runtime is not None:
                            runtime_kwargs = {
                                "messages": messages,
                                "tools": tools_to_use,
                                "model_id": self.model_id,
                                "logical_call_id": logical_call_id,
                                "model_attempt_no": physical_attempt_no,
                            }
                            if fallback_from_invocation_id is not None:
                                runtime_kwargs["fallback_from_invocation_id"] = (
                                    fallback_from_invocation_id
                                )
                            stream = await self.llm_runtime.astream(**runtime_kwargs)
                        else:
                            stream = await self.llm.astream(
                                messages=messages,
                                tools=tools_to_use,
                            )

                        async for chunk in _iter_with_idle_timeout(
                            stream,
                            LLM_STREAM_IDLE_TIMEOUT,
                        ):
                            finalizer = getattr(
                                chunk,
                                "observability_finalizer",
                                None,
                            )
                            if finalizer is not None:
                                if (
                                    step_finalizer is not None
                                    and finalizer is not step_finalizer
                                ):
                                    raise RuntimeError(
                                        "MODEL_MULTIPLE_TERMINAL_FINALIZERS"
                                    )
                                step_finalizer = finalizer
                                active_finalizer = finalizer
                                continue

                            if isinstance(chunk, ErrorChunk):
                                logger.error(
                                    "LLM call failed: code=MODEL_STREAM_ERROR"
                                )
                                yield chunk
                                return
                            if not isinstance(chunk, TextChunk):
                                raise RuntimeError("MODEL_STREAM_CHUNK_INVALID")

                            finish_reason = chunk.finish_reason
                            if finish_reason is not None:
                                if not isinstance(finish_reason, str):
                                    raise RuntimeError(
                                        "MODEL_OUTPUT_FINISH_REASON_INVALID"
                                    )
                                finish_reasons.add(finish_reason)

                            if chunk.tool_calls:
                                tool_calls = chunk.tool_calls
                                if buffering:
                                    if pending:
                                        yield TextChunk(
                                            delta=pending,
                                            usage=(
                                                chunk.usage or pending_usage
                                            ),
                                        )
                                        pending = ""
                                        pending_usage = None
                                    buffering = False

                            if isinstance(chunk, ReasoningChunk):
                                step_reasoning_content += chunk.reasoning_delta
                                yield chunk
                            elif buffering:
                                step_content += chunk.delta
                                pending += chunk.delta
                                if chunk.usage is not None:
                                    pending_usage = chunk.usage
                                if len(step_content) >= _INTENT_MAX_LEN:
                                    yield TextChunk(
                                        delta=pending,
                                        usage=(
                                            chunk.usage or pending_usage
                                        ),
                                    )
                                    pending = ""
                                    pending_usage = None
                                    buffering = False
                            else:
                                step_content += chunk.delta
                                yield TextChunk(
                                    delta=chunk.delta,
                                    usage=chunk.usage,
                                    finish_reason=chunk.finish_reason,
                                )
                    except asyncio.TimeoutError:
                        logger.error(
                            "LLM stream idle timeout: timeout_seconds={}",
                            LLM_STREAM_IDLE_TIMEOUT,
                        )
                        if active_finalizer is not None:
                            finalizer = active_finalizer
                            active_finalizer = None
                            await _finalize_validation_failed(
                                finalizer,
                                "MODEL_STREAM_IDLE_TIMEOUT",
                            )
                        yield ErrorChunk(
                            error_message=(
                                f"模型调用超时：{LLM_STREAM_IDLE_TIMEOUT}s "
                                "内未收到任何响应分片。"
                            ),
                            error_type="llm_stream_timeout",
                        )
                        return
                    finally:
                        if stream is not None:
                            await _close_async_stream(stream)

                    validation_code: str | None = None
                    if len(finish_reasons) > 1:
                        validation_code = "MODEL_OUTPUT_FINISH_REASON_INVALID"
                    else:
                        finish_reason = next(iter(finish_reasons), None)
                        if tool_calls:
                            if finish_reason not in (None, "tool_calls"):
                                validation_code = (
                                    "MODEL_OUTPUT_FINISH_REASON_INVALID"
                                )
                        elif finish_reason not in (None, "stop"):
                            validation_code = (
                                "MODEL_OUTPUT_FINISH_REASON_INVALID"
                            )

                    valid_tool_calls: list[ChoiceDeltaToolCall] = []
                    if validation_code is None:
                        valid_tool_calls, validation_code = _validate_tool_calls(
                            tool_calls,
                            self.tool_fn_map,
                        )
                    if (
                        validation_code is None
                        and not valid_tool_calls
                        and not step_content.strip()
                    ):
                        validation_code = "MODEL_OUTPUT_EMPTY"
                    if (
                        validation_code is None
                        and not valid_tool_calls
                        and _looks_like_unfinished_intent(step_content)
                    ):
                        validation_code = "MODEL_INTENT_UNFINISHED"

                    if validation_code is not None:
                        if pending_usage is not None:
                            yield TextChunk(delta="", usage=pending_usage)
                        finalizer = active_finalizer
                        active_finalizer = None
                        await _finalize_validation_failed(
                            finalizer,
                            validation_code,
                        )

                        observed_attempt = physical_attempt_no
                        handle = getattr(finalizer, "handle", None)
                        handle_attempt = getattr(handle, "attempt_no", None)
                        if (
                            isinstance(handle_attempt, int)
                            and not isinstance(handle_attempt, bool)
                            and handle_attempt >= observed_attempt
                        ):
                            observed_attempt = handle_attempt
                        repair_logical_call_id = logical_call_id
                        repair_attempt_no = observed_attempt + 1
                        repair_fallback_id = getattr(
                            finalizer,
                            "invocation_id",
                            None,
                        )

                        if step_content:
                            messages.append(
                                {
                                    "role": "assistant",
                                    "content": step_content,
                                }
                            )
                        if validation_code == "MODEL_INTENT_UNFINISHED":
                            nudge_count += 1
                            repair_message = (
                                _INTENT_NUDGE_MSG
                                if nudge_count <= MAX_INTENT_NUDGES
                                else _OUTPUT_REPAIR_MSG
                            )
                        else:
                            repair_message = _OUTPUT_REPAIR_MSG
                        messages.append(
                            {
                                "role": "user",
                                "content": repair_message,
                            }
                        )
                        logger.info(
                            "Model output rejected for repair: code={}, "
                            "attempt_no={}",
                            validation_code,
                            repair_attempt_no,
                        )
                        continue

                    repair_logical_call_id = None
                    repair_attempt_no = 1
                    repair_fallback_id = None

                    if not valid_tool_calls:
                        if pending:
                            yield TextChunk(
                                delta=pending,
                                usage=pending_usage,
                                finish_reason=(
                                    next(iter(finish_reasons), None)
                                ),
                            )
                            pending = ""
                            pending_usage = None
                        if step_content:
                            messages.append(
                                {
                                    "role": "assistant",
                                    "content": step_content,
                                }
                            )
                        logger.info("No tool calls. ReAct loop complete.")
                        if active_finalizer is not None:
                            finalizer = active_finalizer
                            active_finalizer = None
                            yield TextChunk(
                                observability_finalizer=finalizer
                            )
                        return

                    finalizer = active_finalizer
                    active_finalizer = None
                    await _finalize_success(finalizer)

                    narration_content = step_content or None
                    reasoning_content = step_reasoning_content or None
                    for tool_call in valid_tool_calls:
                        yield TextChunk(tool_calls=[tool_call])

                    logger.info(
                        "Executing {} tool calls in parallel",
                        len(valid_tool_calls),
                    )
                    tool_results = await asyncio.gather(
                        *[
                            execute_single_tool_call(tc, self.tool_fn_map)
                            for tc in valid_tool_calls
                        ]
                    )

                    assistant_msg = {
                        "role": "assistant",
                        "content": narration_content,
                        "tool_calls": valid_tool_calls,
                    }
                    if reasoning_content:
                        assistant_msg["reasoning_content"] = reasoning_content
                    messages.append(assistant_msg)

                    should_return = False
                    for (
                        tool_call,
                        tool_content,
                        tool_error,
                        message_content,
                    ) in tool_results:
                        capped_content = (
                            self.msg_manager.cap_tool_result(message_content)
                            if message_content
                            else message_content
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "content": capped_content,
                                "tool_call_id": tool_call.id,
                            }
                        )
                        yield ToolResultChunk(
                            tool=tool_call,
                            result=tool_content,
                            error=tool_error,
                        )

                        function_name = tool_call.function.name
                        tool_obj = self.tool_fn_map[function_name]
                        return_chunk = check_and_handle_return_direct(
                            tool_obj=tool_obj,
                            tool_name=function_name,
                            tool_content=tool_content,
                            tool_error=tool_error,
                        )
                        if return_chunk:
                            yield return_chunk
                            should_return = True
                            break

                    if should_return:
                        return

                warning_msg = (
                    f"Reached max steps: {self.max_steps}. Stopping."
                )
                logger.warning(warning_msg)
                yield TextChunk(
                    delta=(
                        "\n\nReached maximum iteration count "
                        f"({self.max_steps}), task ended."
                    )
                )
            finally:
                if active_finalizer is not None:
                    finalizer = active_finalizer
                    active_finalizer = None
                    await _finalize_validation_failed(
                        finalizer,
                        "MODEL_OUTPUT_PROCESSING_CANCELLED",
                    )

        return gen()
