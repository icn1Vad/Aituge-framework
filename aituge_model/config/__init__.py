"""Shared model component and model-pack registry."""

from .registry import (
    EmbeddingModelRegistration,
    LlmModelRegistration,
    ModelPackRegistration,
    ModelRegistry,
    RerankerModelRegistration,
    ResolvedModelPack,
    SpeechRecognitionModelRegistration,
    SecretResolver,
    get_active_model_pack,
    get_model_pack_for_ai_mode,
    load_model_registry,
)
from .runtime import (
    ModelRuntimeProvider,
    ResolvedEmbeddingModel,
    ResolvedLlmModel,
    ResolvedRerankerModel,
    ResolvedSpeechRecognitionModel,
)

__all__ = [
    "EmbeddingModelRegistration",
    "LlmModelRegistration",
    "ModelPackRegistration",
    "ModelRegistry",
    "ModelRuntimeProvider",
    "RerankerModelRegistration",
    "ResolvedEmbeddingModel",
    "ResolvedLlmModel",
    "ResolvedModelPack",
    "ResolvedRerankerModel",
    "ResolvedSpeechRecognitionModel",
    "SpeechRecognitionModelRegistration",
    "SecretResolver",
    "get_active_model_pack",
    "get_model_pack_for_ai_mode",
    "load_model_registry",
]
