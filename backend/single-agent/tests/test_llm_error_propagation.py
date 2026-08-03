from __future__ import annotations

import pytest
from common.llm.models import ErrorChunk, ModelInvocationError
from common.llm.utils import convert_gen_to_stream_chat_completions


@pytest.mark.asyncio
async def test_stream_model_error_raises_instead_of_finishing_with_empty_success() -> None:
    async def chunks():
        yield ErrorChunk(
            delta="模型服务暂时不可用，请稍后重试",
            error_message="模型服务暂时不可用，请稍后重试",
            error_type="MODEL_PROVIDER_UNAVAILABLE",
        )

    stream = convert_gen_to_stream_chat_completions(
        model="test-model",
        response_generator=chunks(),
    )

    with pytest.raises(ModelInvocationError) as exc_info:
        _ = [item async for item in stream]

    assert exc_info.value.code == "MODEL_PROVIDER_UNAVAILABLE"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_stream_model_rejection_preserves_non_retryable_flag() -> None:
    async def chunks():
        yield ErrorChunk(
            error_message="模型服务拒绝了本次请求",
            error_type="MODEL_REQUEST_REJECTED",
            retryable=False,
        )

    stream = convert_gen_to_stream_chat_completions(
        model="test-model",
        response_generator=chunks(),
    )

    with pytest.raises(ModelInvocationError) as exc_info:
        _ = [item async for item in stream]

    assert exc_info.value.code == "MODEL_REQUEST_REJECTED"
    assert exc_info.value.retryable is False
