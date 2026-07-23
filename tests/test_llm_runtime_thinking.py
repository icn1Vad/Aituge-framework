import asyncio
from types import SimpleNamespace

from service.conversation.llm_runner import LlmRuntime, _build_thinking_extra_body


class FakeLlm:
    def __init__(self, *, api_base: str, model: str, enable_thinking: bool) -> None:
        self.api_base = api_base
        self.model = model
        self.enable_thinking = enable_thinking


def test_explicitly_disables_thinking_for_official_deepseek_v4() -> None:
    llm = FakeLlm(
        api_base="https://api.deepseek.com",
        model="deepseek-v4-pro",
        enable_thinking=True,
    )

    assert _build_thinking_extra_body(llm, False) == {
        "thinking": {"type": "disabled"}
    }


def test_keeps_configured_behavior_when_contract_layer_does_not_override() -> None:
    llm = FakeLlm(
        api_base="https://api.deepseek.com",
        model="deepseek-v4-pro",
        enable_thinking=True,
    )

    assert _build_thinking_extra_body(llm, None) == {
        "chat_template_kwargs": {"enable_thinking": True},
        "enable_thinking": True,
    }


def test_uses_existing_compatible_fields_for_other_model_providers() -> None:
    llm = FakeLlm(
        api_base="https://example-model-provider.test/v1",
        model="another-model",
        enable_thinking=True,
    )

    assert _build_thinking_extra_body(llm, False) == {
        "chat_template_kwargs": {"enable_thinking": False},
        "enable_thinking": False,
    }


class FakeAsyncStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        self._values = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._values)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeCompletions:
    def __init__(self, *, streaming_chunks, ordinary_response):
        self.streaming_chunks = streaming_chunks
        self.ordinary_response = ordinary_response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["stream"]:
            return FakeAsyncStream(self.streaming_chunks)
        return self.ordinary_response


def _runtime_with_fake_llm():
    usage = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=30,
        total_tokens=150,
        prompt_tokens_details=SimpleNamespace(cached_tokens=80),
    )
    chunks = [
        SimpleNamespace(
            id="provider-request-1",
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content='{"ok":'),
                    finish_reason=None,
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            id="provider-request-1",
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="true}"),
                    finish_reason="stop",
                )
            ],
            usage=usage,
        ),
    ]
    ordinary = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ordinary"))]
    )
    completions = FakeCompletions(
        streaming_chunks=chunks,
        ordinary_response=ordinary,
    )
    llm = SimpleNamespace(
        model="deepseek-v4-pro",
        api_base="https://api.deepseek.com",
        temperature=0.7,
        max_tokens=8192,
        enable_thinking=True,
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=completions),
        ),
    )
    runtime = LlmRuntime()

    async def get_llm(_model_id=None):
        return llm

    runtime.get_llm = get_llm
    return runtime, completions


def test_complete_with_usage_streams_and_keeps_unit_attribution() -> None:
    runtime, completions = _runtime_with_fake_llm()

    result = asyncio.run(
        runtime.complete_with_usage(
            [{"role": "user", "content": "review"}],
            model_id="deepseek-v4-pro",
            system_prompt="system",
            max_tokens=4000,
            temperature=0,
            thinking_override=False,
            response_format={"type": "json_object"},
            review_unit_id="commercial_financial",
            review_id="review-1",
            framework_run_id="run-1",
            attempt_no=1,
            repair_no=0,
            trace_id="trace-1",
        )
    )

    assert result.content == '{"ok":true}'
    assert result.prompt_tokens == 120
    assert result.cached_tokens == 80
    assert result.completion_tokens == 30
    assert result.total_tokens == 150
    assert result.time_to_first_token_ms is not None
    assert result.provider_request_id == "provider-request-1"
    assert result.finish_reason == "stop"
    assert result.review_unit_id == "commercial_financial"
    assert result.trace_id == "trace-1"
    assert completions.calls[0]["stream"] is True
    assert completions.calls[0]["stream_options"] == {"include_usage": True}
    assert completions.calls[0]["temperature"] == 0
    assert completions.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert completions.calls[0]["response_format"] == {"type": "json_object"}


def test_complete_remains_non_streaming_and_backward_compatible() -> None:
    runtime, completions = _runtime_with_fake_llm()

    value = asyncio.run(
        runtime.complete(
            [{"role": "user", "content": "ordinary"}],
            model_id="deepseek-v4-pro",
            temperature=0,
            thinking_override=False,
        )
    )

    assert value == "ordinary"
    assert completions.calls[0]["stream"] is False
