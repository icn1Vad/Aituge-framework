from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .registry import (
    EmbeddingModelRegistration,
    LlmModelRegistration,
    ModelRegistry,
    RerankerModelRegistration,
    ResolvedModelPack,
    SecretResolver,
    load_model_registry,
)


@dataclass(frozen=True, slots=True)
class ResolvedLlmModel:
    id: str
    mode: str
    provider: str
    model: str
    base_url: str
    api_key: str
    context_window: int
    max_tokens: int
    temperature: float
    enable_thinking: bool
    vision_support: bool


@dataclass(frozen=True, slots=True)
class ResolvedEmbeddingModel:
    id: str
    mode: str
    provider: str
    model: str
    base_url: str
    api_key: str
    dimensions: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ResolvedRerankerModel:
    id: str
    mode: str
    provider: str
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float
    instruction: str


class ModelRuntimeProvider:
    """Single configuration entry used by model-consuming runtimes."""

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        active_pack: ResolvedModelPack,
        secret_resolver: SecretResolver,
    ) -> None:
        self.registry = registry
        self.active_pack = active_pack
        self.secret_resolver = secret_resolver

    @classmethod
    def from_environment(
        cls,
        *,
        directory: str | Path | None = None,
        pack_id: str = "",
        secret_dir: str | Path | None = None,
        secret_overrides: Mapping[str, str] | None = None,
    ) -> "ModelRuntimeProvider":
        configured_dir = str(directory or os.getenv("MODEL_CONFIG_DIR", "")).strip()
        config_root = (
            Path(configured_dir).expanduser().resolve()
            if configured_dir
            else Path(__file__).resolve().parent
        )
        registry = load_model_registry(configured_dir)
        selected_pack = (
            pack_id.strip()
            or os.getenv("MODEL_PACK_ID", "").strip()
            or registry.default_pack_id
        )
        return cls(
            registry=registry,
            active_pack=registry.resolve_pack(selected_pack),
            secret_resolver=SecretResolver(
                secret_dir=(
                    secret_dir
                    or os.getenv("MODEL_SECRET_DIR", "").strip()
                    or config_root / "secrets"
                ),
                overrides=secret_overrides,
            ),
        )

    def llm_registration(self, model_id: str | None = None) -> LlmModelRegistration:
        resolved_id = (model_id or self.active_pack.llm.id).strip()
        registration = self.registry.llms.get(resolved_id)
        if registration is None:
            raise ValueError(f"LLM model '{resolved_id}' is not registered.")
        return registration

    def embedding_registration(self) -> EmbeddingModelRegistration:
        return self.active_pack.embedding

    def reranker_registration(self) -> RerankerModelRegistration:
        return self.active_pack.reranker

    def resolve_llm(
        self,
        model_id: str | None = None,
        *,
        credential_fallback: str = "",
        require_credential: bool = True,
    ) -> ResolvedLlmModel:
        registration = self.llm_registration(model_id)
        api_key = self._credential(
            registration.credential_ref,
            required=registration.mode == "api" and require_credential,
            fallback=credential_fallback,
        )
        return ResolvedLlmModel(
            id=registration.id,
            mode=registration.mode,
            provider=registration.provider,
            model=registration.model,
            base_url=registration.base_url,
            api_key=api_key,
            context_window=registration.context_window,
            max_tokens=registration.max_tokens,
            temperature=registration.temperature,
            enable_thinking=registration.enable_thinking,
            vision_support=registration.vision_support,
        )

    def resolve_embedding(
        self,
        *,
        credential_fallback: str = "",
    ) -> ResolvedEmbeddingModel:
        registration = self.embedding_registration()
        api_key = self._credential(
            registration.credential_ref,
            required=registration.mode == "api",
            fallback=credential_fallback,
        )
        return ResolvedEmbeddingModel(
            id=registration.id,
            mode=registration.mode,
            provider=registration.provider,
            model=registration.model,
            base_url=registration.base_url,
            api_key=api_key,
            dimensions=registration.dimensions,
            timeout_seconds=registration.timeout_seconds,
        )

    def resolve_reranker(
        self,
        *,
        credential_fallback: str = "",
    ) -> ResolvedRerankerModel:
        registration = self.reranker_registration()
        api_key = self._credential(
            registration.credential_ref,
            required=registration.mode == "api",
            fallback=credential_fallback,
        )
        return ResolvedRerankerModel(
            id=registration.id,
            mode=registration.mode,
            provider=registration.provider,
            model=registration.model,
            base_url=registration.base_url,
            api_key=api_key,
            timeout_seconds=registration.timeout_seconds,
            instruction=registration.instruction,
        )

    def resolve_optional_credential(self, credential_ref: str | None) -> str:
        return self.secret_resolver.resolve(credential_ref, required=False)

    def _credential(
        self,
        credential_ref: str | None,
        *,
        required: bool,
        fallback: str,
    ) -> str:
        if not credential_ref and not required:
            return ""
        resolved = self.secret_resolver.resolve(credential_ref, required=False)
        if resolved:
            return resolved
        normalized_fallback = fallback.strip()
        if normalized_fallback:
            return normalized_fallback
        if required:
            return self.secret_resolver.resolve(credential_ref, required=True)
        return ""
