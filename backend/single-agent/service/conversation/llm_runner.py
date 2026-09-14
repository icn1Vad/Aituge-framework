import asyncio
import time
import math
import uuid
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Callable, Mapping, Optional
from urllib.parse import urlparse

from aituge_model.config import ModelRuntimeProvider, ResolvedLlmModel
from common.encrypt_utils import decrypt_key
from common.llm.constants import DEFAULT_LLM_MODEL_ID
from common.llm.llm_model import PaiLlm, normalize_model_exception
from common.llm.models import (
    DEFAULT_MAX_RETRIES,
    ErrorChunk,
    ModelInvocationError,
    TextChunk,
)
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from db.models.llm import LlmModelEntity
from loguru import logger
from model_observability.domain import (
    DispatchStatus,
    DuplicateModelInvocationAttemptError,
    InvocationMetrics,
    MODEL_PROVIDER_FINISH_REASONS,
    RouteType,
)
from model_observability.runtime import (
    ModelInvocationRuntimeRecorder,
    RuntimeInvocationHandle,
    RuntimeInvocationFinalizer,
    RuntimeModelDescriptor,
    RuntimeObservabilityContext,
    classify_dispatch_exception,
    ModelRequestNotDispatchedError,
    recorder_from_environment,
    stable_provider_error_code,
)
from service.model.llm_service import LlmService
from utils.lru_cache import LruCache


llm_cache = LruCache(max_size=20)
_MAX_SINGLE_RETRY_DELAY_SECONDS = 5.0
_MAX_TOTAL_RETRY_DELAY_SECONDS = 30.0
_PROVIDER_REQUEST_ID_MAX_LENGTH = 512
_FINISH_REASON_MAX_LENGTH = 160


@dataclass(frozen=True, slots=True)
class LlmCompletionResult:
    content: str
    prompt_tokens: int | None
    cached_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    time_to_first_token_ms: int | None
    model_duration_ms: int
    trace_id: str
    provider_request_id: str | None
    finish_reason: str | None
    review_unit_id: str | None
    review_id: str | None
    framework_run_id: str | None
    attempt_no: int | None
    repair_no: int
    logical_call_id: str | None = None
    invocation_id: str | None = None
    model_attempt_no: int | None = None
    terminal_finalizer: RuntimeInvocationFinalizer | None = None


@dataclass(frozen=True, slots=True)
class _ValidatedUsage:
    prompt_tokens: int | None
    cached_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


class _ObservedProviderAttemptError(RuntimeError):
    def __init__(
        self,
        cause: BaseException,
        invocation: RuntimeInvocationHandle | None,
        *,
        retry_allowed: bool,
    ) -> None:
        super().__init__(cause.__class__.__name__)
        self.cause = cause
        self.invocation = invocation
        self.retry_allowed = retry_allowed



def _llm_cache_key(config: LlmModelEntity | ResolvedLlmModel) -> str:
    if isinstance(config, ResolvedLlmModel):
        return (
            f"llm:{config.base_url}:{config.api_key}:"
            f"{config.id}:{config.provider}:{config.model}:{config.enable_thinking}:{config.vision_support}:"
            f"{config.temperature}:{config.context_window}:{config.max_tokens}"
        )
    return (
        f"llm:{config.base_url}:{config.encrypted_api_key}:"
        f"{config.model}:{config.enable_thinking}:{config.vision_support}:"
        f"{config.temperature}:{config.context_window}:{config.max_tokens}"
    )


def create_llm(config: LlmModelEntity | ResolvedLlmModel) -> PaiLlm:
    cache_key = _llm_cache_key(config)
    cached = llm_cache.get(cache_key)
    if cached:
        model_id = config.id if isinstance(config, ResolvedLlmModel) else config.model_id
        logger.info(f"Using cached LLM: model_id={model_id}")
        return cached

    if isinstance(config, ResolvedLlmModel):
        if config.mode == "api" and not config.api_key:
            raise ValueError(
                f"LLM model `{config.id}` has no configured credential."
            )
        api_key = config.api_key
        model = config.model
    else:
        api_key = decrypt_key(config.encrypted_api_key)
        model = config.model or config.model_name or config.model_id
    llm = PaiLlm(
        api_base=config.base_url,
        api_key=api_key,
        model=model,
        enable_thinking=config.enable_thinking,
        vision_support=config.vision_support,
        temperature=config.temperature,
        context_window=config.context_window,
        max_retries=0,
        max_tokens=config.max_tokens,
        component_id=config.id if isinstance(config, ResolvedLlmModel) else "",
        provider=config.provider if isinstance(config, ResolvedLlmModel) else "",
        via_gateway=config.via_gateway if isinstance(config, ResolvedLlmModel) else False,
    )
    llm_cache.put(cache_key, llm)
    return llm


class LlmRuntime:
    """Loads model configs and supports shared LLM access."""

    def __init__(
        self,
        tenant_id: str = DEFAULT_TENANT_ID,
        llm_factory: Callable[[LlmModelEntity | ResolvedLlmModel], Any] = create_llm,
        model_runtime_provider: ModelRuntimeProvider | None = None,
        model_pack_id: str | None = None,
        invocation_recorder: ModelInvocationRuntimeRecorder | None = None,
        provider_max_retries: int = DEFAULT_MAX_RETRIES,
        provider_retry_backoff_seconds: float = 0.25,
        observability_context: RuntimeObservabilityContext | None = None,
    ):
        self.tenant_id = tenant_id
        self.llm_factory = llm_factory
        self.model_runtime_provider = (
            model_runtime_provider
            or ModelRuntimeProvider.from_environment(pack_id=model_pack_id or "")
        )
        self.invocation_recorder = (
            invocation_recorder
            if invocation_recorder is not None
            else recorder_from_environment()
        )
        self.observability_context = (
            observability_context or RuntimeObservabilityContext()
        )

        self.provider_max_retries = _validated_retry_count(provider_max_retries)
        self.provider_retry_backoff_seconds = _validated_retry_backoff(
            provider_retry_backoff_seconds
        )
        self._provider_retry_delays = _bounded_retry_delays(
            self.provider_max_retries,
            self.provider_retry_backoff_seconds,
        )

    async def get_llm(self, model_id: Optional[str] = None) -> Any:
        resolved_model_id = model_id or self.model_runtime_provider.active_pack.llm.id
        registration = self.model_runtime_provider.llm_registration(
            resolved_model_id
        )
        credential = self.model_runtime_provider.resolve_optional_credential(
            registration.credential_ref
        )
        if not credential and not self.model_runtime_provider.gateway_enabled:
            credential = await self._legacy_database_credential(resolved_model_id)
        resolved_config = self.model_runtime_provider.resolve_llm(
            resolved_model_id,
            credential_fallback=credential,
            require_credential=False,
        )
        return self.llm_factory(resolved_config)

    async def _legacy_database_credential(self, model_id: str) -> str:
        """Migration fallback; model metadata never comes from this row."""

        async with create_db_session() as session:
            row = await LlmService(session).get_llm_by_model_id(
                model_id=model_id,
                tenant_id=self.tenant_id,
            )
        if row is None or not row.encrypted_api_key:
            return ""
        logger.warning(
            "Model credential for '{}' uses legacy tuge_llm_model fallback; "
            "move it to aituge_model/config/secrets.",
            model_id,
        )
        return decrypt_key(row.encrypted_api_key)

    async def astream(
        self,
        messages: list[dict],
        tools: list[Any] | None = None,
        model_id: str | None = None,
        *,
        logical_call_id: str | None = None,
        model_attempt_no: int = 1,
        fallback_from_invocation_id: str | None = None,
    ) -> AsyncIterator[TextChunk]:
        """Stream with bounded explicit retries before any output is exposed."""

        if model_attempt_no < 1:
            raise ValueError("model_attempt_no must be >= 1")
        if (
            model_attempt_no > 1
            and not logical_call_id
            and not self.observability_context.logical_call_id
        ):
            raise ValueError(
                "logical_call_id is required when model_attempt_no is greater than 1"
            )
        stable_logical_call_id = (
            logical_call_id
            or self.observability_context.logical_call_id
            or f"logical_{uuid.uuid4().hex}"
        )
        initial_fallback_id = (
            fallback_from_invocation_id
            or self.observability_context.fallback_from_invocation_id
        )

        async def retrying_gen() -> AsyncIterator[TextChunk]:
            fallback_id = initial_fallback_id
            for retry_index in range(self.provider_max_retries + 1):
                stream = await self._astream_once(
                    messages=messages,
                    tools=tools,
                    model_id=model_id,
                    logical_call_id=stable_logical_call_id,
                    model_attempt_no=model_attempt_no + retry_index,
                    fallback_from_invocation_id=fallback_id,
                )
                emitted = False
                retry = False
                try:
                    async for chunk in stream:
                        if (
                            isinstance(chunk, ErrorChunk)
                            and chunk.retryable
                            and not emitted
                            and retry_index < self.provider_max_retries
                        ):
                            fallback_id = (
                                chunk.observability_retry_from_invocation_id
                            )
                            retry = True
                            break
                        if not isinstance(chunk, ErrorChunk):
                            emitted = True
                        yield chunk
                except _ObservedProviderAttemptError as observed:
                    if (
                        emitted
                        or retry_index >= self.provider_max_retries
                        or not observed.retry_allowed
                        or not _is_retryable_provider_error(observed.cause)
                    ):
                        raise observed.cause
                    fallback_id = (
                        observed.invocation.invocation_id
                        if observed.invocation is not None
                        else None
                    )
                    retry = True
                finally:
                    await stream.aclose()
                if not retry:
                    return
                await self._wait_before_retry(retry_index)

        return retrying_gen()


    async def _astream_once(
        self,
        messages: list[dict],
        tools: list[Any] | None = None,
        model_id: str | None = None,
        *,
        logical_call_id: str | None = None,
        model_attempt_no: int = 1,
        fallback_from_invocation_id: str | None = None,
    ) -> AsyncIterator[TextChunk]:
        """Stream one physical Provider request and defer its local terminal decision."""

        if model_attempt_no < 1:
            raise ValueError("model_attempt_no must be >= 1")
        llm = await self.get_llm(model_id)
        call_context = replace(
            self.observability_context,
            logical_call_id=(
                logical_call_id or self.observability_context.logical_call_id
            ),
            attempt_no=model_attempt_no,
            fallback_from_invocation_id=(
                fallback_from_invocation_id
                or self.observability_context.fallback_from_invocation_id
            ),
        )
        invocation = await self._begin_invocation(model_id, llm, call_context)

        async def gen() -> AsyncIterator[TextChunk]:
            started = time.perf_counter()
            dispatched = False
            terminal_recorded = False
            terminal_delegated = False
            usage = _validated_usage(None)

            def current_metrics() -> InvocationMetrics:
                return InvocationMetrics(
                    input_tokens=usage.prompt_tokens,
                    output_tokens=usage.completion_tokens,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                )

            try:
                stream = await llm.astream(messages=messages, tools=tools)
                async for chunk in stream:
                    if isinstance(chunk, ErrorChunk):
                        await self._record_failure(
                            invocation,
                            exc=RuntimeError("MODEL_PROVIDER_STREAM_ERROR"),
                            dispatch_status=(
                                DispatchStatus.DISPATCHED
                                if dispatched
                                else DispatchStatus.DISPATCH_UNKNOWN
                            ),
                            metrics=current_metrics(),
                        )
                        terminal_recorded = True
                        chunk.observability_retry_from_invocation_id = (
                            invocation.invocation_id
                            if invocation is not None
                            else None
                        )
                        yield chunk
                        return
                    if not dispatched:
                        await self._record_dispatched(
                            invocation,
                            provider_request_id=None,
                        )
                        dispatched = True
                    try:
                        chunk_usage = _optional_field(chunk, "usage")
                        if chunk_usage is not None:
                            usage = _validated_usage(chunk_usage)
                    except Exception:
                        await self._record_validation_failure(
                            invocation,
                            metrics=InvocationMetrics(
                                latency_ms=round(
                                    (time.perf_counter() - started) * 1000
                                )
                            ),
                            validation_code="MODEL_PROVIDER_USAGE_INVALID",
                            provider_request_id=None,
                        )
                        terminal_recorded = True
                        yield ErrorChunk(
                            error_message="MODEL_PROVIDER_USAGE_INVALID",
                            error_type="model_provider_usage_invalid",
                        )
                        return
                    yield chunk

                if self.invocation_recorder is not None and invocation is not None:
                    terminal_delegated = True
                    yield TextChunk(
                        observability_finalizer=RuntimeInvocationFinalizer(
                            recorder=self.invocation_recorder,
                            handle=invocation,
                            metrics=current_metrics(),
                        )
                    )
            except GeneratorExit:
                if not terminal_recorded and not terminal_delegated:
                    await self._record_failure(
                        invocation,
                        exc=RuntimeError("MODEL_STREAM_ABANDONED"),
                        dispatch_status=(
                            DispatchStatus.DISPATCHED
                            if dispatched
                            else DispatchStatus.DISPATCH_UNKNOWN
                        ),
                        metrics=current_metrics(),
                        error_code="MODEL_STREAM_ABANDONED",
                    )
                raise
            except asyncio.CancelledError:
                if not terminal_recorded and not terminal_delegated:
                    await self._record_failure(
                        invocation,
                        exc=RuntimeError("MODEL_STREAM_CANCELLED"),
                        dispatch_status=(
                            DispatchStatus.DISPATCHED
                            if dispatched
                            else DispatchStatus.DISPATCH_UNKNOWN
                        ),
                        metrics=current_metrics(),
                        error_code="MODEL_STREAM_CANCELLED",
                    )
                raise
            except Exception as exc:
                if not terminal_recorded and not terminal_delegated:
                    await self._record_failure(
                        invocation,
                        exc=exc,
                        dispatch_status=(
                            DispatchStatus.DISPATCHED
                            if dispatched
                            else None
                        ),
                        metrics=current_metrics(),
                    )
                    raise _ObservedProviderAttemptError(
                        exc,
                        invocation,
                        retry_allowed=True,
                    ) from exc
                raise

        return gen()
    async def complete(
        self,
        messages: list[dict],
        model_id: Optional[str] = None,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        thinking_override: Optional[bool] = None,
        *,
        logical_call_id: str | None = None,
        model_attempt_no: int = 1,
        fallback_from_invocation_id: str | None = None,
    ) -> str:
        if model_attempt_no < 1:
            raise ValueError("model_attempt_no must be >= 1")
        if (
            model_attempt_no > 1
            and not logical_call_id
            and not self.observability_context.logical_call_id
        ):
            raise ValueError(
                "logical_call_id is required when model_attempt_no is greater than 1"
            )
        stable_logical_call_id = (
            logical_call_id
            or self.observability_context.logical_call_id
            or f"logical_{uuid.uuid4().hex}"
        )
        fallback_id = (
            fallback_from_invocation_id
            or self.observability_context.fallback_from_invocation_id
        )
        for retry_index in range(self.provider_max_retries + 1):
            try:
                return await self._complete_once(
                    messages=messages,
                    model_id=model_id,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    thinking_override=thinking_override,
                    logical_call_id=stable_logical_call_id,
                    model_attempt_no=model_attempt_no + retry_index,
                    fallback_from_invocation_id=fallback_id,
                )
            except _ObservedProviderAttemptError as observed:
                if (
                    retry_index >= self.provider_max_retries
                    or not observed.retry_allowed
                    or not _is_retryable_provider_error(observed.cause)
                ):
                    raise observed.cause
                fallback_id = (
                    observed.invocation.invocation_id
                    if observed.invocation is not None
                    else None
                )
                await self._wait_before_retry(retry_index)


    async def _complete_once(
        self,
        *,
        logical_call_id: str,
        model_attempt_no: int,
        fallback_from_invocation_id: str | None,
        messages: list[dict],
        model_id: Optional[str] = None,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        thinking_override: Optional[bool] = None,
    ) -> str:
        llm = await self.get_llm(model_id)
        runtime_messages = list(messages)
        if system_prompt:
            runtime_messages = [
                {"role": "system", "content": system_prompt},
                *runtime_messages,
            ]

        call_context = replace(
            self.observability_context,
            logical_call_id=logical_call_id,
            attempt_no=model_attempt_no,
            fallback_from_invocation_id=fallback_from_invocation_id,
        )
        invocation = await self._begin_invocation(model_id, llm, call_context)
        started = time.perf_counter()
        try:
            response = await llm.client.chat.completions.create(
                model=llm.model,
                messages=runtime_messages,
                stream=False,
                temperature=llm.temperature if temperature is None else temperature,
                max_tokens=llm.max_tokens if max_tokens is None else max_tokens,
                extra_body=_build_thinking_extra_body(llm, thinking_override),
            )
        except asyncio.CancelledError:
            await self._record_failure(
                invocation,
                exc=RuntimeError("MODEL_REQUEST_CANCELLED"),
                dispatch_status=DispatchStatus.DISPATCH_UNKNOWN,
                metrics=InvocationMetrics(
                    latency_ms=round((time.perf_counter() - started) * 1000)
                ),
                error_code="MODEL_REQUEST_CANCELLED",
            )
            raise
        except Exception as exc:
            await self._record_failure(
                invocation,
                exc=exc,
                metrics=InvocationMetrics(
                    latency_ms=round((time.perf_counter() - started) * 1000)
                ),
            )
            raise _ObservedProviderAttemptError(
                exc, invocation, retry_allowed=True
            ) from exc
        try:
            provider_request_id = _validated_provider_request_id(response)
        except Exception:
            await self._record_validation_failure(
                invocation,
                metrics=InvocationMetrics(
                    latency_ms=round((time.perf_counter() - started) * 1000)
                ),
                validation_code="MODEL_PROVIDER_RESPONSE_INVALID",
                provider_request_id=None,
            )
            raise RuntimeError("MODEL_PROVIDER_RESPONSE_INVALID") from None
        try:
            usage = _validated_usage(_optional_field(response, "usage"))
        except Exception:
            await self._record_validation_failure(
                invocation,
                metrics=InvocationMetrics(
                    latency_ms=round((time.perf_counter() - started) * 1000)
                ),
                validation_code="MODEL_PROVIDER_USAGE_INVALID",
                provider_request_id=provider_request_id,
            )
            raise RuntimeError("MODEL_PROVIDER_USAGE_INVALID") from None
        try:
            content = _validated_completion_content(response)
            finish_reason = _validated_completion_finish_reason(response)
        except Exception:
            await self._record_validation_failure(
                invocation,
                metrics=InvocationMetrics(
                    latency_ms=round((time.perf_counter() - started) * 1000)
                ),
                validation_code="MODEL_PROVIDER_RESPONSE_INVALID",
                provider_request_id=provider_request_id,
            )
            raise RuntimeError("MODEL_PROVIDER_RESPONSE_INVALID") from None
        await self._record_success(
            invocation,
            metrics=InvocationMetrics(
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                latency_ms=round((time.perf_counter() - started) * 1000),
            ),
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
        )
        return content

    async def complete_with_usage(
        self,
        messages: list[dict],
        model_id: Optional[str] = None,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        thinking_override: Optional[bool] = None,
        response_format: Optional[dict[str, str]] = None,
        *,
        review_unit_id: str | None = None,
        review_id: str | None = None,
        framework_run_id: str | None = None,
        attempt_no: int | None = None,
        repair_no: int = 0,
        trace_id: str | None = None,
        logical_call_id: str | None = None,
        model_attempt_no: int = 1,
        fallback_from_invocation_id: str | None = None,
        defer_terminal: bool = False,
        use_provider_output_default: bool = False,
    ) -> LlmCompletionResult:
        if use_provider_output_default and max_tokens is not None:
            raise ValueError("Provider-default output cannot also specify max_tokens")
        if model_attempt_no < 1:
            raise ValueError("model_attempt_no must be >= 1")
        if (
            model_attempt_no > 1
            and not logical_call_id
            and not self.observability_context.logical_call_id
        ):
            raise ValueError(
                "logical_call_id is required when model_attempt_no is greater than 1"
            )
        stable_logical_call_id = (
            logical_call_id
            or self.observability_context.logical_call_id
            or f"logical_{uuid.uuid4().hex}"
        )
        fallback_id = (
            fallback_from_invocation_id
            or self.observability_context.fallback_from_invocation_id
        )
        stable_trace_id = trace_id or f"llm-{uuid.uuid4().hex}"
        for retry_index in range(self.provider_max_retries + 1):
            try:
                return await self._complete_with_usage_once(
                    messages=messages,
                    model_id=model_id,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    thinking_override=thinking_override,
                    response_format=response_format,
                    review_unit_id=review_unit_id,
                    review_id=review_id,
                    framework_run_id=framework_run_id,
                    attempt_no=attempt_no,
                    repair_no=repair_no,
                    trace_id=stable_trace_id,
                    logical_call_id=stable_logical_call_id,
                    model_attempt_no=model_attempt_no + retry_index,
                    fallback_from_invocation_id=fallback_id,
                    defer_terminal=defer_terminal,
                    use_provider_output_default=use_provider_output_default,
                )
            except _ObservedProviderAttemptError as observed:
                if (
                    retry_index >= self.provider_max_retries
                    or not observed.retry_allowed
                    or not _is_retryable_provider_error(observed.cause)
                ):
                    raise observed.cause
                fallback_id = (
                    observed.invocation.invocation_id
                    if observed.invocation is not None
                    else None
                )
                await self._wait_before_retry(retry_index)


    async def _complete_with_usage_once(
        self,
        messages: list[dict],
        model_id: Optional[str] = None,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        thinking_override: Optional[bool] = None,
        response_format: Optional[dict[str, str]] = None,
        *,
        review_unit_id: str | None = None,
        review_id: str | None = None,
        framework_run_id: str | None = None,
        attempt_no: int | None = None,
        repair_no: int = 0,
        trace_id: str | None = None,
        logical_call_id: str | None = None,
        model_attempt_no: int = 1,
        fallback_from_invocation_id: str | None = None,
        defer_terminal: bool = False,
        use_provider_output_default: bool = False,
    ) -> LlmCompletionResult:
        """Complete one request while collecting provider usage and streaming TTFT.

        Missing provider usage remains ``None``. It is never estimated from text
        length because that would make model-cost metrics look exact when they are
        not. Both completion paths emit lifecycle facts; this method additionally
        returns usage and correlation fields needed by contract-review callers.
        """
        if model_attempt_no < 1:
            raise ValueError("model_attempt_no must be >= 1")
        if (
            model_attempt_no > 1
            and not logical_call_id
            and not self.observability_context.logical_call_id
        ):
            raise ValueError(
                "logical_call_id is required when model_attempt_no is greater than 1"
            )
        llm = await self.get_llm(model_id)
        runtime_messages = list(messages)
        if system_prompt:
            runtime_messages = [
                {"role": "system", "content": system_prompt},
                *runtime_messages,
            ]

        call_context = replace(
            self.observability_context,
            logical_call_id=(
                logical_call_id or self.observability_context.logical_call_id
            ),
            attempt_no=model_attempt_no,
            fallback_from_invocation_id=(
                fallback_from_invocation_id
                or self.observability_context.fallback_from_invocation_id
            ),
            task_id=self.observability_context.task_id or review_id,
            run_id=self.observability_context.run_id or framework_run_id,
            stage_id=self.observability_context.stage_id or review_unit_id,
            trace_id=self.observability_context.trace_id or trace_id,
        )
        invocation = await self._begin_invocation(model_id, llm, call_context)
        started = time.perf_counter()
        first_token_at: float | None = None
        content_parts: list[str] = []
        usage = None
        usage_invalid = False
        response_invalid = False
        saw_chunk = False
        provider_request_id: str | None = None
        finish_reason: str | None = None
        request_kwargs: dict[str, Any] = {
            "model": llm.model,
            "messages": runtime_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": llm.temperature if temperature is None else temperature,
            "max_tokens": llm.max_tokens if max_tokens is None else max_tokens,
            "extra_body": _build_thinking_extra_body(llm, thinking_override),
        }
        if use_provider_output_default:
            # Explicit per-call opt-in; do not silently replace a removed review
            # ceiling with the registry's default (e.g. 8000). Other callers
            # retain their current model-profile/explicit output configuration.
            request_kwargs.pop("max_tokens")
        if response_format is not None:
            request_kwargs["response_format"] = response_format
        transport_accepted = False
        dispatch_recorded = False
        try:
            stream = await llm.client.chat.completions.create(
                **request_kwargs,
            )
            transport_accepted = True
            async for chunk in stream:
                saw_chunk = True
                try:
                    candidate_request_id = _validated_provider_request_id(chunk)
                except Exception:
                    candidate_request_id = None
                    response_invalid = True
                if (
                    provider_request_id is not None
                    and candidate_request_id is not None
                    and provider_request_id != candidate_request_id
                ):
                    response_invalid = True
                provider_request_id = provider_request_id or candidate_request_id
                if not dispatch_recorded:
                    await self._record_dispatched(
                        invocation,
                        provider_request_id=provider_request_id,
                    )
                    dispatch_recorded = True
                try:
                    chunk_usage = _optional_field(chunk, "usage")
                    if chunk_usage is not None:
                        _validated_usage(chunk_usage)
                except Exception:
                    chunk_usage = None
                    usage_invalid = True
                if chunk_usage is not None:
                    usage = chunk_usage
                try:
                    choices = _validated_stream_choices(chunk)
                except Exception:
                    choices = ()
                    response_invalid = True
                for choice_finish_reason, value in choices:
                    if choice_finish_reason is not None:
                        finish_reason = choice_finish_reason
                    if value:
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        content_parts.append(value)
        except asyncio.CancelledError:
            await self._record_failure(
                invocation,
                exc=RuntimeError("MODEL_REQUEST_CANCELLED"),
                dispatch_status=(
                    DispatchStatus.DISPATCHED
                    if transport_accepted
                    else DispatchStatus.DISPATCH_UNKNOWN
                ),
                metrics=InvocationMetrics(
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    time_to_first_token_ms=(
                        round((first_token_at - started) * 1000)
                        if first_token_at is not None
                        else None
                    ),
                ),
                error_code="MODEL_REQUEST_CANCELLED",
            )
            raise
        except Exception as exc:
            await self._record_failure(
                invocation,
                exc=exc,
                dispatch_status=(
                    DispatchStatus.DISPATCHED
                    if transport_accepted
                    else None
                ),
                metrics=InvocationMetrics(
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    time_to_first_token_ms=(
                        round((first_token_at - started) * 1000)
                        if first_token_at is not None
                        else None
                    ),
                ),
            )
            raise _ObservedProviderAttemptError(
                exc, invocation, retry_allowed=True
            ) from exc

        completed = time.perf_counter()
        try:
            if usage_invalid:
                raise TypeError("a prior usage chunk was invalid")
            validated_usage = _validated_usage(usage)
        except Exception:
            await self._record_validation_failure(
                invocation,
                metrics=InvocationMetrics(
                    latency_ms=round((completed - started) * 1000),
                    time_to_first_token_ms=(
                        round((first_token_at - started) * 1000)
                        if first_token_at is not None
                        else None
                    ),
                ),
                validation_code="MODEL_PROVIDER_USAGE_INVALID",
                provider_request_id=provider_request_id,
            )
            raise RuntimeError("MODEL_PROVIDER_USAGE_INVALID") from None
        if response_invalid or not saw_chunk:
            await self._record_validation_failure(
                invocation,
                metrics=InvocationMetrics(
                    latency_ms=round((completed - started) * 1000),
                    time_to_first_token_ms=(
                        round((first_token_at - started) * 1000)
                        if first_token_at is not None
                        else None
                    ),
                ),
                validation_code="MODEL_PROVIDER_RESPONSE_INVALID",
                provider_request_id=provider_request_id,
            )
            raise RuntimeError("MODEL_PROVIDER_RESPONSE_INVALID") from None
        metrics = InvocationMetrics(
            input_tokens=validated_usage.prompt_tokens,
            output_tokens=validated_usage.completion_tokens,
            latency_ms=round((completed - started) * 1000),
            time_to_first_token_ms=(
                round((first_token_at - started) * 1000)
                if first_token_at is not None
                else None
            ),
        )
        terminal_finalizer = (
            RuntimeInvocationFinalizer(
                recorder=self.invocation_recorder,
                handle=invocation,
                metrics=metrics,
                provider_request_id=provider_request_id,
                finish_reason=finish_reason,
            )
            if (
                defer_terminal
                and self.invocation_recorder is not None
                and invocation is not None
            )
            else None
        )
        if not defer_terminal:
            await self._record_success(
                invocation,
                metrics=metrics,
                provider_request_id=provider_request_id,
                finish_reason=finish_reason,
            )
        return LlmCompletionResult(
            content="".join(content_parts),
            prompt_tokens=validated_usage.prompt_tokens,
            cached_tokens=validated_usage.cached_tokens,
            completion_tokens=validated_usage.completion_tokens,
            total_tokens=validated_usage.total_tokens,
            time_to_first_token_ms=(
                round((first_token_at - started) * 1000)
                if first_token_at is not None
                else None
            ),
            model_duration_ms=metrics.latency_ms or 0,
            trace_id=trace_id or f"llm-{uuid.uuid4().hex}",
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
            review_unit_id=review_unit_id,
            review_id=review_id,
            framework_run_id=framework_run_id,
            attempt_no=attempt_no,
            repair_no=repair_no,
            logical_call_id=(invocation.logical_call_id if invocation else None),
            invocation_id=(invocation.invocation_id if invocation else None),
            model_attempt_no=(
                invocation.attempt_no if invocation is not None else None
            ),
            terminal_finalizer=terminal_finalizer,
        )

    async def _wait_before_retry(self, retry_index: int) -> None:
        delay = (
            self._provider_retry_delays[retry_index]
            if retry_index < len(self._provider_retry_delays)
            else 0.0
        )
        if delay > 0:
            await asyncio.sleep(delay)


    async def _begin_invocation(
        self,
        model_id: str | None,
        llm: Any,
        context: RuntimeObservabilityContext | None = None,
    ) -> RuntimeInvocationHandle | None:
        if self.invocation_recorder is None:
            return None
        resolved_model_id = model_id or self.model_runtime_provider.active_pack.llm.id
        registration = self.model_runtime_provider.llm_registration(resolved_model_id)
        descriptor = RuntimeModelDescriptor(
            tenant_id=self.tenant_id,
            provider=registration.provider,
            model_name=str(getattr(llm, "model", "") or registration.model),
            route_type=(
                RouteType.LOCAL if registration.mode == "local" else RouteType.EXTERNAL
            ),
            model_pack_id=self.model_runtime_provider.active_pack.id,
            model_config_id=registration.id,
        )
        try:
            return await self.invocation_recorder.begin(
                descriptor,
                context or self.observability_context,
            )
        except DuplicateModelInvocationAttemptError:
            raise
        except Exception as exc:
            _log_observability_failure("begin", exc)
            return None

    async def _record_dispatched(
        self,
        invocation: RuntimeInvocationHandle | None,
        *,
        provider_request_id: str | None,
    ) -> None:
        if self.invocation_recorder is None or invocation is None:
            return
        try:
            await self.invocation_recorder.dispatched(
                invocation,
                provider_request_id=provider_request_id,
            )
        except Exception as exc:
            _log_observability_failure("dispatch", exc)

    async def _record_failure(
        self,
        invocation: RuntimeInvocationHandle | None,
        *,
        exc: BaseException,
        metrics: InvocationMetrics,
        dispatch_status: DispatchStatus | None = None,
        error_code: str | None = None,
    ) -> None:
        if self.invocation_recorder is None or invocation is None:
            return
        try:
            await self.invocation_recorder.failed(
                invocation,
                dispatch_status=dispatch_status or classify_dispatch_exception(exc),
                error_code=error_code or stable_provider_error_code(exc),
                metrics=metrics,
            )
        except Exception as recorder_exc:
            _log_observability_failure("failure", recorder_exc)

    async def _record_validation_failure(
        self,
        invocation: RuntimeInvocationHandle | None,
        *,
        metrics: InvocationMetrics,
        validation_code: str,
        provider_request_id: str | None,
    ) -> None:
        if self.invocation_recorder is None or invocation is None:
            return
        try:
            await self.invocation_recorder.validation_failed(
                invocation,
                metrics=metrics,
                validation_code=validation_code,
                provider_request_id=provider_request_id,
            )
        except Exception as exc:
            _log_observability_failure("validation", exc)

    async def _record_success(
        self,
        invocation: RuntimeInvocationHandle | None,
        *,
        metrics: InvocationMetrics,
        provider_request_id: str | None,
        finish_reason: str | None,
    ) -> None:
        if self.invocation_recorder is None or invocation is None:
            return
        try:
            await self.invocation_recorder.succeeded(
                invocation,
                metrics=metrics,
                provider_request_id=provider_request_id,
                finish_reason=finish_reason,
            )
        except Exception as exc:
            _log_observability_failure("success", exc)


def _validated_retry_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("provider_max_retries must be an integer")
    if value < 0 or value > 10:
        raise ValueError("provider_max_retries must be between 0 and 10")
    return value


def _validated_retry_backoff(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("provider_retry_backoff_seconds must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0 or normalized > 60:
        raise ValueError(
            "provider_retry_backoff_seconds must be between 0 and 60"
        )
    return normalized


def _bounded_retry_delays(
    max_retries: int,
    base_delay_seconds: float,
) -> tuple[float, ...]:
    remaining = _MAX_TOTAL_RETRY_DELAY_SECONDS
    delays: list[float] = []
    for retry_index in range(max_retries):
        delay = min(
            base_delay_seconds * (2 ** retry_index),
            _MAX_SINGLE_RETRY_DELAY_SECONDS,
            remaining,
        )
        delays.append(delay)
        remaining -= delay
    return tuple(delays)


def _normalized_provider_status_code(exc: BaseException) -> int | None:
    try:
        raw = getattr(exc, "status_code", None)
    except Exception:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        status_code = raw
    elif (
        isinstance(raw, str)
        and len(raw) == 3
        and raw.isascii()
        and raw.isdigit()
    ):
        status_code = int(raw)
    else:
        return None
    return status_code if 100 <= status_code <= 599 else None


def _is_retryable_provider_error(exc: BaseException) -> bool:
    status_code = _normalized_provider_status_code(exc)
    if status_code is not None:
        return status_code in {408, 409, 429} or status_code >= 500
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = exc.__class__.__name__.lower()
    return any(
        marker in name
        for marker in ("timeout", "connection", "ratelimit", "internalserver")
    )




def _build_thinking_extra_body(llm: Any, thinking_override: Optional[bool]) -> dict[str, Any]:
    enabled = llm.enable_thinking if thinking_override is None else thinking_override
    if thinking_override is not None and _is_official_deepseek_v4(llm):
        return {"thinking": {"type": "enabled" if enabled else "disabled"}}
    return {
        "chat_template_kwargs": {"enable_thinking": enabled},
        "enable_thinking": enabled,
    }


def _is_official_deepseek_v4(llm: Any) -> bool:
    provider = str(getattr(llm, "provider", "")).lower()
    hostname = (urlparse(str(getattr(llm, "api_base", ""))).hostname or "").lower()
    model = str(getattr(llm, "model", "")).lower()
    return (provider == "deepseek" or hostname == "api.deepseek.com") and model.startswith("deepseek-v4-")


def _model_invocation_error(exc: BaseException) -> ModelInvocationError:
    if isinstance(exc, ModelInvocationError):
        return exc
    code, message, retryable = normalize_model_exception(exc)
    return ModelInvocationError(code, message, retryable=retryable)
def _optional_field(value: Any, attribute: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(attribute)
    return getattr(value, attribute, None)


def _required_field(value: Any, attribute: str) -> Any:
    if isinstance(value, Mapping):
        if attribute not in value:
            raise TypeError(f"{attribute} is required")
        return value[attribute]
    try:
        return getattr(value, attribute)
    except AttributeError:
        raise TypeError(f"{attribute} is required") from None


def _validated_provider_request_id(payload: Any) -> str | None:
    provider_request_id = _optional_field(payload, "id")
    if provider_request_id is None:
        return None
    return _validated_provider_text(
        provider_request_id,
        field_name="provider request id",
        max_length=_PROVIDER_REQUEST_ID_MAX_LENGTH,
    )


def _validated_provider_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TypeError(f"{field_name} must be a non-empty trimmed string")
    if len(value) > max_length:
        raise ValueError(f"{field_name} is too long")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{field_name} contains a control character")
    return value


def _validated_finish_reason(value: Any) -> str:
    finish_reason = _validated_provider_text(
        value,
        field_name="finish_reason",
        max_length=_FINISH_REASON_MAX_LENGTH,
    )
    if finish_reason not in MODEL_PROVIDER_FINISH_REASONS:
        raise ValueError("finish_reason is not registered")
    return finish_reason


def _validated_completion_content(response: Any) -> str:
    choices = _required_field(response, "choices")
    if not isinstance(choices, (list, tuple)) or not choices:
        raise TypeError("completion choices must be a non-empty sequence")
    message = _required_field(choices[0], "message")
    if message is None:
        raise TypeError("completion message is required")
    content = _required_field(message, "content")
    if content is None:
        return ""
    if not isinstance(content, str):
        raise TypeError("completion content must be a string or null")
    return content


def _validated_completion_finish_reason(response: Any) -> str | None:
    choices = _required_field(response, "choices")
    if not isinstance(choices, (list, tuple)) or not choices:
        raise TypeError("completion choices must be a non-empty sequence")
    finish_reason = _optional_field(choices[0], "finish_reason")
    if finish_reason is None:
        return None
    return _validated_finish_reason(finish_reason)


def _validated_stream_choices(
    chunk: Any,
) -> tuple[tuple[str | None, str | None], ...]:
    choices = _required_field(chunk, "choices")
    if not isinstance(choices, (list, tuple)):
        raise TypeError("stream choices must be a sequence")
    validated: list[tuple[str | None, str | None]] = []
    for choice in choices:
        finish_reason = _optional_field(choice, "finish_reason")
        if finish_reason is not None:
            finish_reason = _validated_finish_reason(finish_reason)
        delta = _required_field(choice, "delta")
        if delta is None:
            raise TypeError("stream delta is required")
        content = _optional_field(delta, "content")
        if content is not None and not isinstance(content, str):
            raise TypeError("stream content must be a string or null")
        validated.append((finish_reason, content))
    return tuple(validated)




def _optional_int(value: Any, attribute: str) -> int | None:
    if isinstance(value, Mapping):
        raw = value.get(attribute)
    else:
        raw = getattr(value, attribute, None) if value is not None else None
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(f"{attribute} must be an integer")
    if raw < 0 or raw > 9_223_372_036_854_775_807:
        raise ValueError(
            f"{attribute} is outside the PostgreSQL bigint range"
        )
    return raw


def _validated_usage(usage: Any) -> _ValidatedUsage:
    if usage is None:
        return _ValidatedUsage(None, None, None, None)
    usage_fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_tokens_details",
    )
    if isinstance(usage, Mapping):
        if not any(field in usage for field in usage_fields):
            raise TypeError("usage payload has no recognized fields")
    elif not any(field in dir(usage) for field in usage_fields):
        raise TypeError("usage payload has no recognized fields")
    prompt_details = _optional_field(usage, "prompt_tokens_details")
    if prompt_details is not None:
        if isinstance(prompt_details, Mapping):
            if "cached_tokens" not in prompt_details:
                raise TypeError("prompt_tokens_details payload is malformed")
        elif "cached_tokens" not in dir(prompt_details):
            raise TypeError("prompt_tokens_details payload is malformed")
    validated = _ValidatedUsage(
        prompt_tokens=_optional_int(usage, "prompt_tokens"),
        cached_tokens=_optional_int(prompt_details, "cached_tokens"),
        completion_tokens=_optional_int(usage, "completion_tokens"),
        total_tokens=_optional_int(usage, "total_tokens"),
    )
    if validated.cached_tokens is not None:
        if (
            validated.prompt_tokens is None
            or validated.cached_tokens > validated.prompt_tokens
        ):
            raise ValueError("cached tokens cannot exceed prompt tokens")
    if (
        validated.prompt_tokens is not None
        and validated.completion_tokens is not None
        and validated.total_tokens is not None
        and validated.total_tokens
        < validated.prompt_tokens + validated.completion_tokens
    ):
        raise ValueError("total tokens cannot be below prompt plus completion")
    return validated


def _log_observability_failure(stage: str, exc: BaseException) -> None:
    # Never render the exception message: provider clients can include request data.
    logger.warning(
        "Model invocation observability write failed: stage={}, error_type={}",
        stage,
        exc.__class__.__name__,
    )
