import asyncio
import json
import sys
import time
import uuid
from common.llm.models import ChatResponseGenerator, ErrorChunk, ModelInvocationError, ReasoningChunk, ToolResultChunk, TextChunk
from extensions.guardrail.guardrail_check import TextCheckResult
from openai.types.chat import ChatCompletionChunk, ChatCompletion, ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage
from openai.types.chat.chat_completion_chunk import ChoiceDelta, Choice as ChunkChoice
from openai.types.chat.chat_completion import Choice
from extensions.guardrail.config import (
    CHECK_OUTPUT_CHUNK_OVERLAP,
    CHECK_OUTPUT_CHUNK_SIZE,
    OUTPUT_CHECK_TIMEOUT_SECONDS,
)
from extensions.guardrail.guardrail_check import GuardrailChecker
from sqlmodel.ext.asyncio.session import AsyncSession

from loguru import logger
from extensions.trace.context import get_request_id
from typing import List


def parse_llm_json(json_str: str) -> dict:
    start_pos = json_str.find("{")
    end_pos = json_str.rfind("}")

    if start_pos == -1 or end_pos == -1 or start_pos >= end_pos:
        logger.warning("Invalid JSON string: {json_str}")
        return {}

    return json.loads(json_str[start_pos:end_pos+1])



def get_citation_source(tool_name: str) -> str:
    return "knowledgebase" if tool_name.startswith("search-knowledgebase") else "web"


def extract_citations(tool_chunk: ToolResultChunk):
    citations, citation_details = [], []
    tool_name = tool_chunk.tool.function.name or "dummy"
    seen_files = set()
    if tool_name == "aliyun-websearch" or tool_name == "tavily-websearch" or tool_name.startswith("search-knowledgebase"):
        if not tool_chunk.result:
            return citations, citation_details

        tool_call_results = json.loads(tool_chunk.result).get("result", []) or []

        for result in tool_call_results:
            file_name = result.get("title", "")
            if not file_name or file_name in seen_files:
                continue

            citations.append(file_name)
            citation_details.append({
                "source": get_citation_source(tool_name),
                "text": result.get("content", ""),
                "name": file_name,
                "url": result.get("url", ""),
                "score": result.get("score", 0),
            })

            seen_files.add(file_name)

    return citations, citation_details



MAX_TOOL_HISTORY_CHARS = 20000
TOOL_HISTORY_TRUNCATED_MARKER = "\n...[content truncated]"


def _collect_tool_history(chunk: ToolResultChunk, tool_history_messages: List[dict]):
    tool_call = chunk.tool
    tool_history_messages.append({
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": tool_call.id,
            "type": "function",
            "function": {
                "name": tool_call.function.name,
                "arguments": tool_call.function.arguments,
            }
        }]
    })
    raw_content = chunk.result or chunk.error or ""
    if isinstance(raw_content, str) and len(raw_content) > MAX_TOOL_HISTORY_CHARS:
        raw_content = raw_content[:MAX_TOOL_HISTORY_CHARS] + TOOL_HISTORY_TRUNCATED_MARKER
    tool_history_messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": raw_content,
    })


async def error_chunk_gen(message: str, exception: Exception | None = None) -> ChatResponseGenerator:
    yield ErrorChunk(
        delta=message,
        exception=str(exception) if exception else exception,
    )



async def _finalize_model_observation(
    finalizer,
    *,
    denied: bool,
) -> None:
    if finalizer is None:
        return
    try:
        write_task = None
        if denied:
            write_task = asyncio.create_task(
                finalizer.deny("OUTPUT_POLICY_REJECTED")
            )
        else:
            write_task = asyncio.create_task(finalizer.succeed())
        try:
            await asyncio.shield(write_task)
        except asyncio.CancelledError:
            await write_task
            raise
    except Exception as exc:
        logger.warning(
            "Model invocation finalize failed: error_type={}",
            exc.__class__.__name__,
        )

async def _finalize_model_validation_failure(
    finalizer,
    validation_code: str,
) -> None:
    if finalizer is None:
        return
    try:
        write_task = asyncio.create_task(
            finalizer.validation_failed(validation_code)
        )
        try:
            await asyncio.shield(write_task)
        except asyncio.CancelledError:
            await write_task
            raise
    except Exception as exc:
        logger.warning(
            "Model invocation validation-finalize failed: error_type={}",
            exc.__class__.__name__,
        )

async def _cancel_output_check_tasks(tasks) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

async def convert_gen_to_stream_chat_completions(
    model: str,
    response_generator: ChatResponseGenerator,
    enable_output_check: bool = False,
    guardrail_hint: str | None = None,
    checker: GuardrailChecker | None = None,
    session: AsyncSession = None,
    user_id: str = None,
    session_id: str = None,
    user_message: dict = None,
):
    logger.info(
        "convert_gen_to_stream_chat_completions: model={}, output_check={}",
        model,
        enable_output_check,
    )
    if enable_output_check and not checker:
        logger.warning("convert_gen_to_stream_chat_completions: checker is None, set enable_output_check to False")
        enable_output_check = False

    chunk_index = 0
    chat_id = get_request_id() or uuid.uuid4().hex
    total_usage = CompletionUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    citations, citation_details = [], []

    current_content = ""
    check_tasks = []
    output_check_result = TextCheckResult()
    fail_fast = False
    failure: ModelInvocationError | None = None
    final_content = ""  # 累积完整的助手回复内容
    tool_history_messages = []  # 收集 tool 交互消息用于保存历史
    finalizer = None

    try:
        async for chunk in response_generator:
            candidate_finalizer = getattr(
                chunk, "observability_finalizer", None
            )
            if candidate_finalizer is not None:
                finalizer = candidate_finalizer
                continue

            if isinstance(chunk, ErrorChunk) and chunk.error_type.startswith("MODEL_"):
                fail_fast = True
                failure = ModelInvocationError(
                    chunk.error_type or "MODEL_PROVIDER_UNAVAILABLE",
                    chunk.error_message or chunk.delta or "模型服务暂时不可用，请稍后重试",
                    retryable=chunk.retryable,
                )
                break

            if chunk.usage:
                total_usage.prompt_tokens += chunk.usage.prompt_tokens
                total_usage.completion_tokens += chunk.usage.completion_tokens
                total_usage.total_tokens += chunk.usage.total_tokens
                has_reasoning_delta = isinstance(chunk, ReasoningChunk) and bool(chunk.reasoning_delta)
                if not chunk.delta and not chunk.tool_calls and not has_reasoning_delta:
                    continue
            if output_check_result.reject:
                fail_fast = True
                continue

            if isinstance(chunk, ToolResultChunk):
                citations, citation_details = extract_citations(chunk)
                _collect_tool_history(chunk, tool_history_messages)

            current_content += chunk.delta
            final_content += chunk.delta
            if enable_output_check and checker and len(current_content) >= CHECK_OUTPUT_CHUNK_SIZE:
                check_tasks.append(asyncio.create_task(checker.acheck_output(text=current_content, current_result=output_check_result)))
                current_content = current_content[-CHECK_OUTPUT_CHUNK_OVERLAP:]

            completion_chunk = ChatCompletionChunk(
                id=chat_id,
                choices=[
                    ChunkChoice(
                        delta=ChoiceDelta(
                            role="assistant",
                            content=chunk.delta,
                            reasoning_content=chunk.reasoning_delta if isinstance(chunk, ReasoningChunk) else None,
                        ),
                        index=chunk_index,
                        finish_reason=None,
                    )
                ],
                actions=[action.model_dump(mode="json") for action in chunk.tool_calls] if chunk.tool_calls else None,
                observation=chunk.model_dump(mode="json") if isinstance(chunk, ToolResultChunk) else None,
                trace_id=chunk.trace_id if isinstance(chunk, TextChunk) else None,
                model=model,
                created=int(time.time()),
                citations=citations,
                citation_details=citation_details,
                object="chat.completion.chunk",
            )
            chunk_index += 1

            yield json.dumps(completion_chunk.model_dump(mode="json"), ensure_ascii=False)

            if isinstance(chunk, ErrorChunk):
                fail_fast = True
                break

    finally:
        active_error = sys.exc_info()[1]
        try:
            if response_generator and hasattr(response_generator, "aclose"):
                await response_generator.aclose()
                logger.info("convert_gen_to_stream_chat_completions: response_generator closed.")
            if session:
                await session.close()
                logger.info("convert_gen_to_stream_chat_completions: session closed.")
        except asyncio.CancelledError:
            await _finalize_model_validation_failure(
                finalizer,
                "MODEL_OUTPUT_PROCESSING_CANCELLED",
            )
            finalizer = None
            raise
        except Exception:
            await _finalize_model_validation_failure(
                finalizer,
                "MODEL_OUTPUT_PROCESSING_FAILED",
            )
            finalizer = None
            raise

        if active_error is not None:
            await _finalize_model_validation_failure(
                finalizer,
                (
                    "MODEL_OUTPUT_PROCESSING_CANCELLED"
                    if isinstance(active_error, asyncio.CancelledError)
                    else "MODEL_OUTPUT_PROCESSING_FAILED"
                ),
            )
            finalizer = None

        # 保存会话历史
        if final_content and user_id and session_id and user_message:
            try:
                from service.conversation import ConversationManager
                assistant_message = {
                    "role": "assistant",
                    "content": final_content,
                }
                await ConversationManager().save_live_history(
                    user_id=user_id,
                    session_id=session_id,
                    user_message=user_message,
                    assistant_message=assistant_message,
                    tool_messages=tool_history_messages if tool_history_messages else None,
                )
                logger.info("Session history saved in stream mode.")
            except asyncio.CancelledError:
                await _finalize_model_validation_failure(
                    finalizer,
                    "MODEL_OUTPUT_PROCESSING_CANCELLED",
                )
                finalizer = None
                raise
            except Exception as exc:
                logger.error(
                    "Failed to save session history in stream mode: error_type={}",
                    exc.__class__.__name__,
                )

    if failure is not None:
        raise failure

    if not fail_fast and len(current_content) > CHECK_OUTPUT_CHUNK_OVERLAP and enable_output_check and checker:
        check_tasks.append(asyncio.create_task(checker.acheck_output(text=current_content, current_result=output_check_result)))

    try:
        if check_tasks:
            await asyncio.wait_for(
                asyncio.gather(*check_tasks),
                timeout=OUTPUT_CHECK_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError:
        await _cancel_output_check_tasks(check_tasks)
        await _finalize_model_validation_failure(
            finalizer,
            "OUTPUT_GUARDRAIL_TIMEOUT",
        )
        finalizer = None
        raise TimeoutError("OUTPUT_GUARDRAIL_TIMEOUT") from None
    except asyncio.CancelledError:
        await _cancel_output_check_tasks(check_tasks)
        await _finalize_model_validation_failure(
            finalizer,
            "OUTPUT_GUARDRAIL_CANCELLED",
        )
        finalizer = None
        raise
    except Exception as exc:
        await _cancel_output_check_tasks(check_tasks)
        logger.warning(
            "Output guardrail check failed: error_type={}",
            exc.__class__.__name__,
        )
        await _finalize_model_validation_failure(
            finalizer,
            "OUTPUT_GUARDRAIL_CHECK_FAILED",
        )
        finalizer = None
        raise RuntimeError("OUTPUT_GUARDRAIL_CHECK_FAILED") from None
    await _finalize_model_observation(
        finalizer,
        denied=output_check_result.reject,
    )
    finalizer = None

    if output_check_result.reject:
        error_chunk = ChatCompletionChunk(
            id=chat_id,
            choices=[
                ChunkChoice(
                    delta=ChoiceDelta(
                        role="assistant",
                        content=output_check_result.advice or guardrail_hint,
                    ),
                    index=chunk_index,
                    finish_reason=None,
                )
            ],
            safety_violation=True,
            model=model,
            created=int(time.time()),
            citations=citations,
            citation_details=citation_details,
            object="chat.completion.chunk",
        )
        yield json.dumps(error_chunk.model_dump(mode="json"), ensure_ascii=False)

    stop_chunk = ChatCompletionChunk(
            id=chat_id,
            choices=[
                ChunkChoice(
                    delta=ChoiceDelta(
                        role="assistant",
                        content="",
                    ),
                    index=chunk_index,
                    finish_reason="stop",
                )
            ],
            model=model,
            created=int(time.time()),
            object="chat.completion.chunk",
            citation_details=citation_details,
            citations=citations,
            usage=total_usage
        )
    yield json.dumps(stop_chunk.model_dump(mode="json"), ensure_ascii=False)



async def convert_gen_to_chat_completions(
    model: str,
    response_generator: ChatResponseGenerator,
    enable_output_check: bool = False,
    guardrail_hint: str | None = None,
    checker: GuardrailChecker | None = None,
    user_id: str = None,
    session_id: str = None,
    user_message: dict = None,
):
    chat_id = get_request_id() or uuid.uuid4().hex

    total_usage = CompletionUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    reasoning_content = ""
    content = ""
    steps = []
    citations, citation_details = [], []


    checked = False
    tool_history_messages = []
    finalizer = None
    output_rejected = False

    async for chunk in response_generator:
        candidate_finalizer = getattr(chunk, "observability_finalizer", None)
        if candidate_finalizer is not None:
            finalizer = candidate_finalizer
            continue

        if isinstance(chunk, ErrorChunk):
            if chunk.error_type.startswith("MODEL_"):
                raise ModelInvocationError(
                    chunk.error_type,
                    chunk.error_message or chunk.delta or "模型服务暂时不可用，请稍后重试",
                    retryable=chunk.retryable,
                )
            logger.info(f"Input guardrail failed: {chunk.delta}, directly return.")
            content = chunk.delta
            checked = True
            break

        if chunk.usage:
            total_usage.prompt_tokens += chunk.usage.prompt_tokens
            total_usage.completion_tokens += chunk.usage.completion_tokens
            total_usage.total_tokens += chunk.usage.total_tokens

        if isinstance(chunk, ReasoningChunk) and chunk.reasoning_delta:
            reasoning_content += chunk.reasoning_delta

        content += chunk.delta

        if isinstance(chunk, ToolResultChunk):
            steps.append(chunk)
            citations, citation_details = extract_citations(chunk)
            _collect_tool_history(chunk, tool_history_messages)

    if not checked and enable_output_check and checker:
        current_result = TextCheckResult()
        try:
            await asyncio.wait_for(
                checker.acheck_output(
                    text=content,
                    current_result=current_result,
                ),
                timeout=OUTPUT_CHECK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await _finalize_model_validation_failure(
                finalizer,
                "OUTPUT_GUARDRAIL_TIMEOUT",
            )
            raise TimeoutError("OUTPUT_GUARDRAIL_TIMEOUT") from None
        except asyncio.CancelledError:
            await _finalize_model_validation_failure(
                finalizer,
                "OUTPUT_GUARDRAIL_CANCELLED",
            )
            raise
        except Exception as exc:
            logger.warning(
                "Output guardrail check failed: error_type={}",
                exc.__class__.__name__,
            )
            await _finalize_model_validation_failure(
                finalizer,
                "OUTPUT_GUARDRAIL_CHECK_FAILED",
            )
            raise RuntimeError("OUTPUT_GUARDRAIL_CHECK_FAILED") from None
        if current_result.reject:
            logger.warning("Model output guardrail rejected the response.")
            output_rejected = True
            content = current_result.advice or guardrail_hint

    await _finalize_model_observation(
        finalizer,
        denied=output_rejected,
    )
    message = ChatCompletion(
            id=chat_id,
            model=model,
            created=int(time.time()),
            object="chat.completion",
            choices=[
                Choice(
                    index=0,
                    message=ChatCompletionMessage(
                        role="assistant",
                        content=content,
                        reasoning_content=reasoning_content if reasoning_content else None,
                    ),
                    finish_reason="stop",
                )
            ],
            steps=steps,
            citations=citations,
            citation_details=citation_details,
            usage=total_usage
        )

    # 保存会话历史
    if content and user_id and session_id and user_message:
        try:
            from service.conversation import ConversationManager
            assistant_message = {
                "role": "assistant",
                "content": content,
            }
            await ConversationManager().save_live_history(
                user_id=user_id,
                session_id=session_id,
                user_message=user_message,
                assistant_message=assistant_message,
                tool_messages=tool_history_messages if tool_history_messages else None,
            )
            logger.info("Session history saved in non-stream mode.")
        except Exception as exc:
            logger.error(
                "Failed to save session history in non-stream mode: error_type={}",
                exc.__class__.__name__,
            )

    return message.model_dump(mode="json")
