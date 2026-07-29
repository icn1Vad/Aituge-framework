from __future__ import annotations

import os
from dataclasses import dataclass

from aituge_model_config import (
    ModelRuntimeProvider,
    ResolvedEmbeddingModel,
    ResolvedRerankerModel,
)

from proof.config import Settings


EmbeddingRuntimeConfig = ResolvedEmbeddingModel
RerankerRuntimeConfig = ResolvedRerankerModel


@dataclass(frozen=True, slots=True)
class ProofModelRuntime:
    pack_id: str
    llm_model_id: str
    embedding: EmbeddingRuntimeConfig | None
    reranker: RerankerRuntimeConfig | None

    @property
    def embedding_configured(self) -> bool:
        return self.embedding is not None

    @property
    def reranker_configured(self) -> bool:
        return self.reranker is not None


def build_proof_model_runtime(
    settings: Settings,
    *,
    model_pack_id: str | None = None,
) -> ProofModelRuntime:
    """Resolve one immutable model runtime for all Proof business paths."""

    configured_pack_id = (
        str(model_pack_id or "").strip()
        or str(getattr(settings, "model_pack_id", "") or "").strip()
        or os.getenv("MODEL_PACK_ID", "").strip()
    )
    if not configured_pack_id:
        return _legacy_runtime(settings)

    configured_dir = str(getattr(settings, "model_config_dir", "") or "").strip()
    provider = ModelRuntimeProvider.from_environment(
        directory=configured_dir,
        pack_id=configured_pack_id,
        secret_dir=(
            str(getattr(settings, "model_secret_dir", "") or "").strip()
            or None
        ),
    )
    embedding = provider.resolve_embedding(
        credential_fallback=settings.embedding_api_key,
    )
    reranker = provider.resolve_reranker(
        credential_fallback=settings.resolved_rerank_api_key,
    )
    return ProofModelRuntime(
        pack_id=provider.active_pack.id,
        llm_model_id=provider.active_pack.llm.id,
        embedding=embedding,
        reranker=reranker,
    )


def _legacy_runtime(settings: Settings) -> ProofModelRuntime:
    embedding = None
    if settings.embedding_configured:
        embedding = EmbeddingRuntimeConfig(
            id="legacy-proof-embedding",
            mode="api",
            provider="openai_compatible",
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimensions=int(settings.embedding_dimensions or 0),
            timeout_seconds=settings.embedding_timeout_seconds,
        )
    reranker = None
    if settings.reranker_configured:
        reranker = RerankerRuntimeConfig(
            id="legacy-proof-reranker",
            mode="api",
            provider="dashscope",
            base_url=settings.rerank_base_url,
            api_key=settings.resolved_rerank_api_key,
            model=settings.rerank_model,
            timeout_seconds=settings.rerank_timeout_seconds,
            instruction=settings.rerank_instruction,
        )
    return ProofModelRuntime(
        pack_id="legacy",
        llm_model_id=(
            ModelRuntimeProvider.from_environment().active_pack.llm.id
        ),
        embedding=embedding,
        reranker=reranker,
    )
