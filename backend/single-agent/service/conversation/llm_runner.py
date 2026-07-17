from typing import Any, Callable, Optional

from common.encrypt_utils import decrypt_key
from common.llm.constants import DEFAULT_LLM_MODEL_ID
from common.llm.llm_model import PaiLlm
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session
from db.models.llm import LlmModelEntity
from loguru import logger
from service.model.llm_service import LlmService
from utils.lru_cache import LruCache


llm_cache = LruCache(max_size=20)


def _llm_cache_key(config: LlmModelEntity) -> str:
    return (
        f"llm:{config.base_url}:{config.encrypted_api_key}:"
        f"{config.model}:{config.enable_thinking}:{config.vision_support}:"
        f"{config.temperature}:{config.context_window}:{config.max_tokens}"
    )


def create_llm(config: LlmModelEntity) -> PaiLlm:
    cache_key = _llm_cache_key(config)
    cached = llm_cache.get(cache_key)
    if cached:
        logger.info(f"Using cached LLM: model_id={config.model_id}")
        return cached

    llm = PaiLlm(
        api_base=config.base_url,
        api_key=decrypt_key(config.encrypted_api_key),
        model=config.model or config.model_name or config.model_id,
        enable_thinking=config.enable_thinking,
        vision_support=config.vision_support,
        temperature=config.temperature,
        context_window=config.context_window,
        max_tokens=config.max_tokens,
    )
    llm_cache.put(cache_key, llm)
    return llm


class LlmRuntime:
    """Loads model configs and supports shared LLM access."""

    def __init__(
        self,
        tenant_id: str = DEFAULT_TENANT_ID,
        llm_factory: Callable[[LlmModelEntity], Any] = create_llm,
    ):
        self.tenant_id = tenant_id
        self.llm_factory = llm_factory

    async def get_llm(self, model_id: Optional[str] = None) -> Any:
        resolved_model_id = model_id or DEFAULT_LLM_MODEL_ID
        async with create_db_session() as session:
            llm_model = await LlmService(session).get_llm_by_model_id(
                model_id=resolved_model_id,
                tenant_id=self.tenant_id,
            )
            if not llm_model:
                raise ValueError(f"LLM model `{resolved_model_id}` not found.")
            return self.llm_factory(llm_model)

    async def complete(
        self,
        messages: list[dict],
        model_id: Optional[str] = None,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        llm = await self.get_llm(model_id)
        runtime_messages = list(messages)
        if system_prompt:
            runtime_messages = [
                {"role": "system", "content": system_prompt},
                *runtime_messages,
            ]

        response = await llm.client.chat.completions.create(
            model=llm.model,
            messages=runtime_messages,
            stream=False,
            temperature=llm.temperature if temperature is None else temperature,
            max_tokens=llm.max_tokens if max_tokens is None else max_tokens,
            extra_body={
                "chat_template_kwargs": {"enable_thinking": llm.enable_thinking},
                "enable_thinking": llm.enable_thinking,
            },
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""
