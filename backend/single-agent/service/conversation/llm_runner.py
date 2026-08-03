import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from aituge_model.config import ModelRuntimeProvider, ResolvedLlmModel
from common.encrypt_utils import decrypt_key
from common.llm.constants import DEFAULT_LLM_MODEL_ID
from common.llm.llm_model import PaiLlm, normalize_model_exception
from common.llm.models import ModelInvocationError
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from db.models.llm import LlmModelEntity
from loguru import logger
from service.model.llm_service import LlmService
from utils.lru_cache import LruCache


llm_cache = LruCache(max_size=20)


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
    ):
        self.tenant_id = tenant_id
        self.llm_factory = llm_factory
        self.model_runtime_provider = (
            model_runtime_provider
            or ModelRuntimeProvider.from_environment(pack_id=model_pack_id or "")
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

    async def complete(
        self,
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

        try:
            response = await llm.client.chat.completions.create(
                model=llm.model,
                messages=runtime_messages,
                stream=False,
                temperature=llm.temperature if temperature is None else temperature,
                max_tokens=llm.max_tokens if max_tokens is None else max_tokens,
                extra_body=_build_thinking_extra_body(llm, thinking_override),
            )
        except Exception as exc:
            raise _model_invocation_error(exc) from exc
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""

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
    ) -> LlmCompletionResult:
        """Complete one request while collecting provider usage and streaming TTFT.

        Missing provider usage remains ``None``. It is never estimated from text
        length because that would make model-cost metrics look exact when they are
        not. The existing ``complete`` method intentionally remains unchanged.
        """
        llm = await self.get_llm(model_id)
        runtime_messages = list(messages)
        if system_prompt:
            runtime_messages = [
                {"role": "system", "content": system_prompt},
                *runtime_messages,
            ]

        started = time.perf_counter()
        first_token_at: float | None = None
        content_parts: list[str] = []
        usage = None
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
        if response_format is not None:
            request_kwargs["response_format"] = response_format
        try:
            stream = await llm.client.chat.completions.create(
                **request_kwargs,
            )
            async for chunk in stream:
                provider_request_id = provider_request_id or getattr(chunk, "id", None)
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = chunk_usage
                choices = getattr(chunk, "choices", None) or []
                for choice in choices:
                    if getattr(choice, "finish_reason", None) is not None:
                        finish_reason = choice.finish_reason
                    delta = getattr(choice, "delta", None)
                    value = getattr(delta, "content", None) if delta is not None else None
                    if value:
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        content_parts.append(value)
        except Exception as exc:
            raise _model_invocation_error(exc) from exc

        completed = time.perf_counter()
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        return LlmCompletionResult(
            content="".join(content_parts),
            prompt_tokens=_optional_int(usage, "prompt_tokens"),
            cached_tokens=_optional_int(prompt_details, "cached_tokens"),
            completion_tokens=_optional_int(usage, "completion_tokens"),
            total_tokens=_optional_int(usage, "total_tokens"),
            time_to_first_token_ms=(
                round((first_token_at - started) * 1000)
                if first_token_at is not None
                else None
            ),
            model_duration_ms=round((completed - started) * 1000),
            trace_id=trace_id or f"llm-{uuid.uuid4().hex}",
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
            review_unit_id=review_unit_id,
            review_id=review_id,
            framework_run_id=framework_run_id,
            attempt_no=attempt_no,
            repair_no=repair_no,
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


def _optional_int(value: Any, attribute: str) -> int | None:
    raw = getattr(value, attribute, None) if value is not None else None
    return int(raw) if raw is not None else None
