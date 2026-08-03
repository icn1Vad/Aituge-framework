from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import common.llm.utils as llm_utils
from common.llm.models import TextChunk
from common.llm.utils import convert_gen_to_chat_completions, convert_gen_to_stream_chat_completions
from agent.react_agent import _iter_with_idle_timeout

from model_observability.domain import DispatchStatus
from model_observability.runtime import (
    ModelRequestNotDispatchedError,
    RuntimeInvocationHandle,
    classify_dispatch_exception,
)
_RUNNER_PATH = (
    Path(__file__).parents[2]
    / "backend"
    / "single-agent"
    / "service"
    / "conversation"
    / "llm_runner.py"
)
_RUNNER_SPEC = importlib.util.spec_from_file_location(
    "_model_observability_llm_runner_under_test",
    _RUNNER_PATH,
)
assert _RUNNER_SPEC is not None and _RUNNER_SPEC.loader is not None
_RUNNER_MODULE = importlib.util.module_from_spec(_RUNNER_SPEC)
sys.modules[_RUNNER_SPEC.name] = _RUNNER_MODULE
_RUNNER_SPEC.loader.exec_module(_RUNNER_MODULE)
LlmRuntime = _RUNNER_MODULE.LlmRuntime


class RecordingRecorder:
    def __init__(self) -> None:
        self.calls = []

    async def begin(self, descriptor, context):
        self.calls.append(("begin", descriptor, context))
        return RuntimeInvocationHandle("inv-runtime", "logical-runtime", 1)

    async def dispatched(self, handle, *, provider_request_id=None):
        self.calls.append(("dispatched", handle, provider_request_id))

    async def failed(self, handle, *, dispatch_status, error_code, metrics):
        self.calls.append(("failed", handle, dispatch_status, error_code, metrics))

    async def succeeded(
        self,
        handle,
        *,
        metrics,
        provider_request_id=None,
        finish_reason=None,
    ):
        self.calls.append(
            ("succeeded", handle, metrics, provider_request_id, finish_reason)
        )

    async def denied(
        self,
        handle,
        *,
        metrics,
        guardrail_code,
        provider_request_id=None,
    ):
        self.calls.append(
            ("denied", handle, metrics, guardrail_code, provider_request_id)
        )

    async def validation_failed(
        self,
        handle,
        *,
        metrics,
        validation_code,
        provider_request_id=None,
    ):
        self.calls.append(
            ("validation_failed", handle, metrics, validation_code, provider_request_id)
        )



class FakeCompletions:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    async def create(self, **_kwargs):
        if self.error is not None:
            raise self.error
        return self.result


class FakeAsyncStream:
    def __init__(self, chunks) -> None:
        self._chunks = tuple(chunks)

    def __aiter__(self):
        self._iterator = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeRuntimeProvider:
    def __init__(self) -> None:
        self.active_pack = SimpleNamespace(
            id="pack-test",
            llm=SimpleNamespace(id="deepseek-v4-pro"),
        )
        self._registration = SimpleNamespace(
            id="deepseek-v4-pro",
            provider="deepseek",
            mode="api",
            model="deepseek-v4-pro",
        )

    def llm_registration(self, _model_id):
        return self._registration


def _runtime(recorder: RecordingRecorder) -> LlmRuntime:
    return LlmRuntime(
        invocation_recorder=recorder,
        model_runtime_provider=FakeRuntimeProvider(),
    )


def _fake_llm(completions: FakeCompletions):
    return SimpleNamespace(
        model="deepseek-v4-pro",
        api_base="https://provider.example.test/v1",
        temperature=0.1,
        max_tokens=100,
        enable_thinking=False,
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
def _fake_streaming_llm(chunks):
    class FakeStreamingLlm:
        model = "deepseek-v4-pro"
        api_base = "https://provider.example.test/v1"
        temperature = 0.1
        max_tokens = 100
        enable_thinking = False

        async def astream(self, *, messages, tools=None):
            assert messages
            assert tools is None
            return chunks if hasattr(chunks, "__aiter__") else FakeAsyncStream(chunks)

    return FakeStreamingLlm()


class RecordingFinalizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def succeed(self) -> None:
        self.calls.append(("succeed", None))

    async def deny(self, code: str) -> None:
        self.calls.append(("deny", code))

    async def validation_failed(self, code: str) -> None:
        self.calls.append(("validation_failed", code))


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight", ["credential", "route", "privacy", "input_guardrail"])
async def test_pre_dispatch_rejections_do_not_create_invocation(preflight) -> None:
    recorder = RecordingRecorder()
    runtime = _runtime(recorder)

    async def reject(_model_id=None):
        raise ValueError(f"{preflight} rejected")

    runtime.get_llm = reject
    with pytest.raises(ValueError):
        await runtime.complete([{"role": "user", "content": "not persisted"}])
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_success_starts_only_at_provider_boundary_and_records_metrics() -> None:
    recorder = RecordingRecorder()
    response = SimpleNamespace(
        id="provider-request-id",
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok"),
                finish_reason="stop",
            )
        ],
    )
    runtime = _runtime(recorder)

    async def get_llm(_model_id=None):
        return _fake_llm(FakeCompletions(result=response))

    runtime.get_llm = get_llm
    assert await runtime.complete([{"role": "user", "content": "not recorded"}]) == "ok"
    assert [call[0] for call in recorder.calls] == ["begin", "succeeded"]
    assert recorder.calls[1][2].input_tokens == 12
    assert recorder.calls[1][2].output_tokens == 4
    assert recorder.calls[1][3] == "provider-request-id"
    assert recorder.calls[1][4] == "stop"
    assert all("not recorded" not in repr(call) for call in recorder.calls)


@pytest.mark.asyncio
async def test_ambiguous_transport_error_is_dispatch_unknown() -> None:
    recorder = RecordingRecorder()
    runtime = _runtime(recorder)

    async def get_llm(_model_id=None):
        return _fake_llm(FakeCompletions(error=ConnectionError("raw response")))

    runtime.get_llm = get_llm
    with pytest.raises(ConnectionError):
        await runtime.complete([{"role": "user", "content": "secret body"}])
    failed = recorder.calls[-1]
    assert failed[0] == "failed"
    assert failed[2] is DispatchStatus.DISPATCH_UNKNOWN
    assert failed[3] == "MODEL_PROVIDER_DISPATCH_ERROR"
    assert "raw response" not in repr(failed)
    assert "secret body" not in repr(failed)


def test_not_dispatched_requires_explicit_proof_marker() -> None:
    assert classify_dispatch_exception(
        ModelRequestNotDispatchedError("local validation")
    ) is DispatchStatus.NOT_DISPATCHED
    assert classify_dispatch_exception(TimeoutError("unknown")) is DispatchStatus.DISPATCH_UNKNOWN


@pytest.mark.asyncio
async def test_streaming_call_propagates_business_correlation_and_retry_identity() -> None:
    recorder = RecordingRecorder()
    usage = SimpleNamespace(
        prompt_tokens=7,
        completion_tokens=3,
        total_tokens=10,
        prompt_tokens_details=None,
    )
    chunks = [
        SimpleNamespace(
            id="provider-id",
            usage=usage,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    delta=SimpleNamespace(content="ok"),
                )
            ],
        )
    ]
    runtime = _runtime(recorder)

    async def get_llm(_model_id=None):
        return _fake_llm(FakeCompletions(result=FakeAsyncStream(chunks)))

    runtime.get_llm = get_llm
    result = await runtime.complete_with_usage(
        [{"role": "user", "content": "never logged"}],
        review_id="task-1",
        framework_run_id="run-1",
        review_unit_id="stage-1",
        trace_id="trace-1",
        logical_call_id="logical-1",
        model_attempt_no=2,
        fallback_from_invocation_id="inv-previous",
    )

    assert [call[0] for call in recorder.calls] == [
        "begin",
        "dispatched",
        "succeeded",
    ]
    begin_context = recorder.calls[0][2]
    assert begin_context.task_id == "task-1"
    assert begin_context.run_id == "run-1"
    assert begin_context.stage_id == "stage-1"
    assert begin_context.trace_id == "trace-1"
    assert begin_context.logical_call_id == "logical-1"
    assert begin_context.attempt_no == 2
    assert begin_context.fallback_from_invocation_id == "inv-previous"
    assert result.logical_call_id == "logical-runtime"
    assert result.invocation_id == "inv-runtime"
    assert result.terminal_finalizer is None
    assert recorder.calls[-1][4] == "stop"
    assert "never logged" not in repr(recorder.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_request_id", "finish_reason"),
    [
        ("p" * 513, "stop"),
        ("provider\nrequest", "stop"),
        ("provider-id", "f" * 161),
        ("provider-id", "stop\x00"),
        ("provider-id", "end_turn"),
    ],
)
async def test_provider_identifiers_and_finish_reason_are_strictly_validated(
    provider_request_id: str,
    finish_reason: str,
) -> None:
    recorder = RecordingRecorder()
    runtime = _runtime(recorder)
    usage = SimpleNamespace(
        prompt_tokens=7,
        completion_tokens=3,
        total_tokens=10,
        prompt_tokens_details=None,
    )
    chunks = [
        SimpleNamespace(
            id=provider_request_id,
            usage=usage,
            choices=[
                SimpleNamespace(
                    finish_reason=finish_reason,
                    delta=SimpleNamespace(content="must-not-succeed"),
                )
            ],
        )
    ]

    async def get_llm(_model_id=None):
        return _fake_llm(FakeCompletions(result=FakeAsyncStream(chunks)))

    runtime.get_llm = get_llm
    with pytest.raises(RuntimeError, match="MODEL_PROVIDER_RESPONSE_INVALID"):
        await runtime.complete_with_usage(
            [{"role": "user", "content": "never persisted"}],
            defer_terminal=True,
        )

    assert [call[0] for call in recorder.calls] == [
        "begin",
        "dispatched",
        "validation_failed",
    ]
    assert all(call[0] != "succeeded" for call in recorder.calls)


@pytest.mark.asyncio
async def test_retry_number_without_logical_call_is_rejected_before_dispatch() -> None:
    recorder = RecordingRecorder()
    runtime = _runtime(recorder)

    with pytest.raises(ValueError, match="logical_call_id"):
        await runtime.complete_with_usage([], model_attempt_no=2)

    assert recorder.calls == []

@pytest.mark.asyncio
async def test_astream_defers_terminal_until_output_policy_decides() -> None:
    recorder = RecordingRecorder()
    runtime = _runtime(recorder)

    async def get_llm(_model_id=None):
        return _fake_streaming_llm([TextChunk(delta="provider output")])

    runtime.get_llm = get_llm
    stream = await runtime.astream(
        [{"role": "user", "content": "not persisted"}],
        logical_call_id="logical-stream",
    )
    chunks = [chunk async for chunk in stream]

    assert [call[0] for call in recorder.calls] == ["begin", "dispatched"]
    assert chunks[0].delta == "provider output"
    finalizer = chunks[-1].observability_finalizer
    assert finalizer is not None

    await finalizer.deny("OUTPUT_POLICY_REJECTED")
    await finalizer.deny("OUTPUT_POLICY_REJECTED")
    assert [call[0] for call in recorder.calls] == [
        "begin",
        "dispatched",
        "denied",
    ]
    assert recorder.calls[-1][3] == "OUTPUT_POLICY_REJECTED"
    with pytest.raises(RuntimeError, match="already finalized"):
        await finalizer.succeed()


@pytest.mark.asyncio
async def test_astream_proven_local_rejection_is_not_dispatched() -> None:
    recorder = RecordingRecorder()
    runtime = _runtime(recorder)

    class RejectedLlm:
        model = "deepseek-v4-pro"

        async def astream(self, **_kwargs):
            raise ModelRequestNotDispatchedError("sensitive local detail")

    async def get_llm(_model_id=None):
        return RejectedLlm()

    runtime.get_llm = get_llm
    stream = await runtime.astream([{"role": "user", "content": "secret"}])
    with pytest.raises(ModelRequestNotDispatchedError):
        await stream.__anext__()

    assert [call[0] for call in recorder.calls] == ["begin", "failed"]
    assert recorder.calls[-1][2] is DispatchStatus.NOT_DISPATCHED
    assert recorder.calls[-1][3] == "MODEL_REQUEST_NOT_DISPATCHED"
    assert "sensitive local detail" not in repr(recorder.calls)
    assert "secret" not in repr(recorder.calls)


@pytest.mark.asyncio
async def test_astream_iteration_error_after_chunk_is_dispatched_failure() -> None:
    recorder = RecordingRecorder()
    runtime = _runtime(recorder)

    class FailingStream:
        def __init__(self) -> None:
            self.count = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.count += 1
            if self.count == 1:
                return TextChunk(delta="partial")
            raise ConnectionError("sensitive provider response")

    async def get_llm(_model_id=None):
        return _fake_streaming_llm(FailingStream())

    runtime.get_llm = get_llm
    stream = await runtime.astream([{"role": "user", "content": "secret"}])
    assert (await stream.__anext__()).delta == "partial"
    with pytest.raises(ConnectionError):
        await stream.__anext__()

    assert [call[0] for call in recorder.calls] == [
        "begin",
        "dispatched",
        "failed",
    ]
    assert recorder.calls[-1][2] is DispatchStatus.DISPATCHED
    assert recorder.calls[-1][3] == "MODEL_PROVIDER_DISPATCH_ERROR"
    assert "sensitive provider response" not in repr(recorder.calls)


@pytest.mark.asyncio
async def test_astream_idle_timeout_closes_dispatched_attempt() -> None:
    recorder = RecordingRecorder()
    runtime = _runtime(recorder)

    class HangingStream:
        def __init__(self) -> None:
            self.count = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.count += 1
            if self.count == 1:
                return TextChunk(delta="partial")
            await asyncio.Future()

    async def get_llm(_model_id=None):
        return _fake_streaming_llm(HangingStream())

    runtime.get_llm = get_llm
    stream = await runtime.astream([{"role": "user", "content": "secret"}])
    timed_stream = _iter_with_idle_timeout(stream, timeout=0.01)
    assert (await timed_stream.__anext__()).delta == "partial"
    with pytest.raises(asyncio.TimeoutError):
        await timed_stream.__anext__()

    assert [call[0] for call in recorder.calls] == [
        "begin",
        "dispatched",
        "failed",
    ]
    assert recorder.calls[-1][2] is DispatchStatus.DISPATCHED
    assert recorder.calls[-1][3] == "MODEL_STREAM_CANCELLED"


@pytest.mark.asyncio
async def test_astream_early_close_records_abandoned_terminal() -> None:
    recorder = RecordingRecorder()
    runtime = _runtime(recorder)

    async def get_llm(_model_id=None):
        return _fake_streaming_llm(
            [TextChunk(delta="first"), TextChunk(delta="second")]
        )

    runtime.get_llm = get_llm
    stream = await runtime.astream([{"role": "user", "content": "secret"}])
    assert (await stream.__anext__()).delta == "first"
    await stream.aclose()

    assert [call[0] for call in recorder.calls] == [
        "begin",
        "dispatched",
        "failed",
    ]
    assert recorder.calls[-1][2] is DispatchStatus.DISPATCHED
    assert recorder.calls[-1][3] == "MODEL_STREAM_ABANDONED"

@pytest.mark.asyncio
async def test_non_stream_guardrail_decides_before_terminal_fact() -> None:
    finalizer = RecordingFinalizer()

    class RejectingChecker:
        async def acheck_output(self, *, text, current_result):
            assert text == "unsafe response"
            assert finalizer.calls == []
            current_result.reject = True
            current_result.advice = "blocked"

    async def response():
        yield TextChunk(delta="unsafe response")
        yield TextChunk(observability_finalizer=finalizer)

    result = await convert_gen_to_chat_completions(
        "model",
        response(),
        enable_output_check=True,
        checker=RejectingChecker(),
    )

    assert result["choices"][0]["message"]["content"] == "blocked"
    assert finalizer.calls == [("deny", "OUTPUT_POLICY_REJECTED")]


@pytest.mark.asyncio
async def test_stream_guardrail_drains_provider_before_denied_terminal() -> None:
    finalizer = RecordingFinalizer()
    provider_drained = False

    class RejectingChecker:
        async def acheck_output(self, *, text, current_result):
            assert finalizer.calls == []
            current_result.reject = True
            current_result.advice = "blocked"

    async def response():
        nonlocal provider_drained
        yield TextChunk(delta="unsafe " * 400)
        yield TextChunk(delta="tail")
        provider_drained = True
        yield TextChunk(observability_finalizer=finalizer)

    encoded_chunks = [
        item
        async for item in convert_gen_to_stream_chat_completions(
            "model",
            response(),
            enable_output_check=True,
            checker=RejectingChecker(),
        )
    ]

    assert provider_drained is True
    assert finalizer.calls == [("deny", "OUTPUT_POLICY_REJECTED")]
    assert any('"safety_violation": true' in item for item in encoded_chunks)

@pytest.mark.asyncio
async def test_complete_with_usage_exposes_finalizer_only_when_deferred() -> None:
    recorder = RecordingRecorder()
    runtime = _runtime(recorder)
    chunks = [
        SimpleNamespace(
            id="provider-id",
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    delta=SimpleNamespace(content="ok"),
                )
            ],
        )
    ]

    async def get_llm(_model_id=None):
        return _fake_llm(FakeCompletions(result=FakeAsyncStream(chunks)))

    runtime.get_llm = get_llm
    result = await runtime.complete_with_usage(
        [{"role": "user", "content": "not persisted"}],
        defer_terminal=True,
    )

    assert [call[0] for call in recorder.calls] == ["begin", "dispatched"]
    assert result.terminal_finalizer is not None
    await result.terminal_finalizer.validation_failed(
        "MODEL_OUTPUT_SCHEMA_INVALID"
    )
    assert [call[0] for call in recorder.calls] == [
        "begin",
        "dispatched",
        "validation_failed",
    ]


@pytest.mark.asyncio
async def test_stream_conversion_cancellation_during_close_finalizes_pending_invocation() -> None:
    finalizer = RecordingFinalizer()
    close_entered = asyncio.Event()

    class CancelDuringClose:
        def __init__(self) -> None:
            self._sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._sent:
                raise StopAsyncIteration
            self._sent = True
            return TextChunk(observability_finalizer=finalizer)

        async def aclose(self):
            close_entered.set()
            await asyncio.Future()

    async def consume() -> None:
        async for _ in convert_gen_to_stream_chat_completions(
            "model",
            CancelDuringClose(),
        ):
            pass

    task = asyncio.create_task(consume())
    await close_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finalizer.calls == [
        ("validation_failed", "MODEL_OUTPUT_PROCESSING_CANCELLED")
    ]


@pytest.mark.asyncio
async def test_stream_conversion_generator_error_finalizes_pending_invocation() -> None:
    finalizer = RecordingFinalizer()

    async def response():
        yield TextChunk(observability_finalizer=finalizer)
        raise RuntimeError("provider wrapper failed")

    async def consume() -> None:
        async for _ in convert_gen_to_stream_chat_completions(
            "model",
            response(),
        ):
            pass

    with pytest.raises(RuntimeError, match="provider wrapper failed"):
        await consume()
    assert finalizer.calls == [
        ("validation_failed", "MODEL_OUTPUT_PROCESSING_FAILED")
    ]


@pytest.mark.asyncio
async def test_stream_conversion_session_close_error_finalizes_pending_invocation() -> None:
    finalizer = RecordingFinalizer()

    class FailingSession:
        async def close(self):
            raise RuntimeError("session close failed")

    async def response():
        yield TextChunk(observability_finalizer=finalizer)

    async def consume() -> None:
        async for _ in convert_gen_to_stream_chat_completions(
            "model",
            response(),
            session=FailingSession(),
        ):
            pass

    with pytest.raises(RuntimeError, match="session close failed"):
        await consume()
    assert finalizer.calls == [
        ("validation_failed", "MODEL_OUTPUT_PROCESSING_FAILED")
    ]


@pytest.mark.asyncio
async def test_non_stream_checker_exception_writes_stable_validation_terminal() -> None:
    finalizer = RecordingFinalizer()

    class FailingChecker:
        async def acheck_output(self, *, text, current_result):
            raise RuntimeError("CHECKER_SECRET_CANARY")

    async def response():
        yield TextChunk(delta="sensitive response")
        yield TextChunk(observability_finalizer=finalizer)

    with pytest.raises(RuntimeError, match="OUTPUT_GUARDRAIL_CHECK_FAILED") as exc:
        await convert_gen_to_chat_completions(
            "model",
            response(),
            enable_output_check=True,
            checker=FailingChecker(),
        )

    assert "CHECKER_SECRET_CANARY" not in str(exc.value)
    assert finalizer.calls == [
        ("validation_failed", "OUTPUT_GUARDRAIL_CHECK_FAILED")
    ]


@pytest.mark.asyncio
async def test_stream_checker_exception_writes_stable_validation_terminal() -> None:
    finalizer = RecordingFinalizer()

    class FailingChecker:
        async def acheck_output(self, *, text, current_result):
            raise RuntimeError("CHECKER_SECRET_CANARY")

    async def response():
        yield TextChunk(delta="sensitive " * 400)
        yield TextChunk(observability_finalizer=finalizer)

    async def consume() -> None:
        async for _ in convert_gen_to_stream_chat_completions(
            "model",
            response(),
            enable_output_check=True,
            checker=FailingChecker(),
        ):
            pass

    with pytest.raises(RuntimeError, match="OUTPUT_GUARDRAIL_CHECK_FAILED") as exc:
        await consume()

    assert "CHECKER_SECRET_CANARY" not in str(exc.value)
    assert finalizer.calls == [
        ("validation_failed", "OUTPUT_GUARDRAIL_CHECK_FAILED")
    ]


@pytest.mark.asyncio
async def test_non_stream_hanging_checker_times_out_and_closes_terminal(
    monkeypatch,
) -> None:
    finalizer = RecordingFinalizer()
    monkeypatch.setattr(llm_utils, "OUTPUT_CHECK_TIMEOUT_SECONDS", 0.01)

    class HangingChecker:
        async def acheck_output(self, *, text, current_result):
            await asyncio.Future()

    async def response():
        yield TextChunk(delta="sensitive response")
        yield TextChunk(observability_finalizer=finalizer)

    with pytest.raises(TimeoutError, match="OUTPUT_GUARDRAIL_TIMEOUT"):
        await convert_gen_to_chat_completions(
            "model",
            response(),
            enable_output_check=True,
            checker=HangingChecker(),
        )

    assert finalizer.calls == [
        ("validation_failed", "OUTPUT_GUARDRAIL_TIMEOUT")
    ]


@pytest.mark.asyncio
async def test_stream_hanging_checker_times_out_and_closes_terminal(
    monkeypatch,
) -> None:
    finalizer = RecordingFinalizer()
    monkeypatch.setattr(llm_utils, "OUTPUT_CHECK_TIMEOUT_SECONDS", 0.01)

    class HangingChecker:
        async def acheck_output(self, *, text, current_result):
            await asyncio.Future()

    async def response():
        yield TextChunk(delta="sensitive " * 400)
        yield TextChunk(observability_finalizer=finalizer)

    async def consume() -> None:
        async for _ in convert_gen_to_stream_chat_completions(
            "model",
            response(),
            enable_output_check=True,
            checker=HangingChecker(),
        ):
            pass

    with pytest.raises(TimeoutError, match="OUTPUT_GUARDRAIL_TIMEOUT"):
        await consume()

    assert finalizer.calls == [
        ("validation_failed", "OUTPUT_GUARDRAIL_TIMEOUT")
    ]


@pytest.mark.asyncio
async def test_non_stream_checker_cancellation_closes_terminal() -> None:
    finalizer = RecordingFinalizer()
    checker_started = asyncio.Event()

    class HangingChecker:
        async def acheck_output(self, *, text, current_result):
            checker_started.set()
            await asyncio.Future()

    async def response():
        yield TextChunk(delta="sensitive response")
        yield TextChunk(observability_finalizer=finalizer)

    task = asyncio.create_task(
        convert_gen_to_chat_completions(
            "model",
            response(),
            enable_output_check=True,
            checker=HangingChecker(),
        )
    )
    await checker_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finalizer.calls == [
        ("validation_failed", "OUTPUT_GUARDRAIL_CANCELLED")
    ]


@pytest.mark.asyncio
async def test_complete_cancellation_before_response_records_unknown_terminal() -> None:
    recorder = RecordingRecorder()
    runtime = _runtime(recorder)
    entered = asyncio.Event()

    class HangingCompletions:
        async def create(self, **_kwargs):
            entered.set()
            await asyncio.Future()

    async def get_llm(_model_id=None):
        return _fake_llm(HangingCompletions())

    runtime.get_llm = get_llm
    task = asyncio.create_task(
        runtime.complete([{"role": "user", "content": "secret"}])
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [call[0] for call in recorder.calls] == ["begin", "failed"]
    assert recorder.calls[-1][2] is DispatchStatus.DISPATCH_UNKNOWN
    assert recorder.calls[-1][3] == "MODEL_REQUEST_CANCELLED"


@pytest.mark.asyncio
async def test_complete_with_usage_cancellation_mid_stream_records_dispatched_terminal() -> None:
    recorder = RecordingRecorder()
    runtime = _runtime(recorder)
    waiting_for_next = asyncio.Event()

    class HangingProviderStream:
        def __init__(self) -> None:
            self.count = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.count += 1
            if self.count == 1:
                return SimpleNamespace(
                    id="provider-id",
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            finish_reason=None,
                            delta=SimpleNamespace(content="partial"),
                        )
                    ],
                )
            waiting_for_next.set()
            await asyncio.Future()

    async def get_llm(_model_id=None):
        return _fake_llm(
            FakeCompletions(result=HangingProviderStream())
        )

    runtime.get_llm = get_llm
    task = asyncio.create_task(
        runtime.complete_with_usage(
            [{"role": "user", "content": "secret"}]
        )
    )
    await waiting_for_next.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [call[0] for call in recorder.calls] == [
        "begin",
        "dispatched",
        "failed",
    ]
    assert recorder.calls[-1][2] is DispatchStatus.DISPATCHED
    assert recorder.calls[-1][3] == "MODEL_REQUEST_CANCELLED"
