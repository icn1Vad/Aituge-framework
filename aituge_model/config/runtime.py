"""Resolve model registrations for direct or gateway-backed runtimes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .registry import (
    EmbeddingModelRegistration,
    LlmModelRegistration,
    ModelRegistry,
    RerankerModelRegistration,
    ResolvedModelPack,
    SpeechRecognitionModelRegistration,
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
    identity_base_url: str = ""
    via_gateway: bool = False


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
    identity_base_url: str = ""
    via_gateway: bool = False


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
    identity_base_url: str = ""
    via_gateway: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedSpeechRecognitionModel:
    id: str
    mode: str
    provider: str
    model: str
    base_url: str
    app_key: str
    access_key_id: str
    access_key_secret: str
    token: str
    region: str
    meta_endpoint: str
    audio_format: str
    sample_rate: int
    channels: int
    recommended_chunk_ms: int


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
    ) -> ModelRuntimeProvider:
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

    @property
    def gateway_enabled(self) -> bool:
        return bool(_gateway_base_url())

    def embedding_registration(self) -> EmbeddingModelRegistration:
        return self.active_pack.embedding

    def reranker_registration(self) -> RerankerModelRegistration:
        return self.active_pack.reranker

    def speech_recognition_registration(
        self,
        model_id: str | None = None,
    ) -> SpeechRecognitionModelRegistration:
        resolved_id = (
            model_id
            or os.getenv("SPEECH_RECOGNITION_MODEL_ID", "")
            or self.registry.default_speech_recognition_id
        ).strip()
        registration = self.registry.speech_recognizers.get(resolved_id)
        if registration is None:
            raise ValueError(
                f"Speech recognition model {resolved_id!r} is not registered."
            )
        return registration

    def resolve_llm(
        self,
        model_id: str | None = None,
        *,
        credential_fallback: str = "",
        require_credential: bool = True,
    ) -> ResolvedLlmModel:
        registration = self.llm_registration(model_id)
        gateway_base_url = _gateway_base_url()
        api_key = (
            _gateway_token()
            if gateway_base_url
            else self._credential(
                registration.credential_ref,
                required=registration.mode == "api" and require_credential,
                fallback=credential_fallback,
            )
        )
        return ResolvedLlmModel(
            id=registration.id,
            mode=registration.mode,
            provider=registration.provider,
            model=registration.model,
            base_url=f"{gateway_base_url}/v1" if gateway_base_url else registration.base_url,
            api_key=api_key,
            context_window=registration.context_window,
            max_tokens=registration.max_tokens,
            temperature=registration.temperature,
            enable_thinking=registration.enable_thinking,
            vision_support=registration.vision_support,
            identity_base_url=registration.base_url,
            via_gateway=bool(gateway_base_url),
        )

    def resolve_embedding(
        self,
        *,
        credential_fallback: str = "",
    ) -> ResolvedEmbeddingModel:
        registration = self.embedding_registration()
        gateway_base_url = _gateway_base_url()
        api_key = (
            _gateway_token()
            if gateway_base_url
            else self._credential(
                registration.credential_ref,
                required=registration.mode == "api",
                fallback=credential_fallback,
            )
        )
        return ResolvedEmbeddingModel(
            id=registration.id,
            mode=registration.mode,
            provider=registration.provider,
            model=registration.model,
            base_url=f"{gateway_base_url}/v1" if gateway_base_url else registration.base_url,
            api_key=api_key,
            dimensions=registration.dimensions,
            timeout_seconds=registration.timeout_seconds,
            identity_base_url=registration.base_url,
            via_gateway=bool(gateway_base_url),
        )

    def resolve_reranker(
        self,
        *,
        credential_fallback: str = "",
    ) -> ResolvedRerankerModel:
        registration = self.reranker_registration()
        gateway_base_url = _gateway_base_url()
        api_key = (
            _gateway_token()
            if gateway_base_url
            else self._credential(
                registration.credential_ref,
                required=registration.mode == "api",
                fallback=credential_fallback,
            )
        )
        return ResolvedRerankerModel(
            id=registration.id,
            mode=registration.mode,
            provider=registration.provider,
            model=registration.model,
            base_url=f"{gateway_base_url}/v1/reranks" if gateway_base_url else registration.base_url,
            api_key=api_key,
            timeout_seconds=registration.timeout_seconds,
            instruction=registration.instruction,
            identity_base_url=registration.base_url,
            via_gateway=bool(gateway_base_url),
        )

    def resolve_speech_recognition(
        self,
        model_id: str | None = None,
        *,
        require_credentials: bool = True,
    ) -> ResolvedSpeechRecognitionModel:
        registration = self.speech_recognition_registration(model_id)
        token = self.secret_resolver.resolve(registration.token_ref, required=False)
        app_key = self.secret_resolver.resolve(
            registration.app_key_ref, required=require_credentials
        )
        access_key_required = require_credentials and not token
        access_key_id = self.secret_resolver.resolve(
            registration.access_key_id_ref, required=access_key_required
        )
        access_key_secret = self.secret_resolver.resolve(
            registration.access_key_secret_ref, required=access_key_required
        )
        return ResolvedSpeechRecognitionModel(
            id=registration.id,
            mode=registration.mode,
            provider=registration.provider,
            model=registration.model,
            base_url=registration.base_url,
            app_key=app_key,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            token=token,
            region=registration.region,
            meta_endpoint=registration.meta_endpoint,
            audio_format=registration.audio_format,
            sample_rate=registration.sample_rate,
            channels=registration.channels,
            recommended_chunk_ms=registration.recommended_chunk_ms,
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


def _gateway_base_url() -> str:
    return os.getenv("MODEL_GATEWAY_URL", "").strip().rstrip("/")


def _gateway_token() -> str:
    token = os.getenv("MODEL_GATEWAY_TOKEN", "").strip()
    if not token:
        raise ValueError("MODEL_GATEWAY_TOKEN is required when MODEL_GATEWAY_URL is set.")
    return token
