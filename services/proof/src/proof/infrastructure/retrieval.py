from __future__ import annotations

from proof.application.retrieval import RetrievalBranchResult, RetrievalFilters
from proof.config import Settings
from proof.errors import ProofError
from proof.infrastructure.embedding import OpenAICompatibleEmbeddingClient
from proof.infrastructure.postgres.repository import ProofRepository
from proof.model_runtime import EmbeddingRuntimeConfig


class PostgresKeywordRetriever:
    name = "keyword"

    def __init__(self, repository: ProofRepository) -> None:
        self.repository = repository

    def retrieve(
        self,
        query: str,
        *,
        limit: int,
        filters: RetrievalFilters,
    ) -> RetrievalBranchResult:
        return RetrievalBranchResult(
            items=self.repository.keyword_search(
                query=query,
                top_k=limit,
                policy_ids=filters.policy_ids,
                level_codes=filters.level_codes,
                category_codes=filters.category_codes,
            ),
            metadata={"text_search_config": "jiebacfg"},
        )


class PgvectorPolicyRetriever:
    name = "vector"

    def __init__(
        self,
        embedding_config: EmbeddingRuntimeConfig | Settings | None,
        repository: ProofRepository,
    ) -> None:
        self.embedding_config = embedding_config
        self.repository = repository

    def retrieve(
        self,
        query: str,
        *,
        limit: int,
        filters: RetrievalFilters,
    ) -> RetrievalBranchResult:
        client = OpenAICompatibleEmbeddingClient(self.embedding_config)
        if self.repository.count_embeddings(client.profile.id) == 0:
            raise ProofError(
                "index_not_ready",
                "No index exists for the active embedding profile.",
                status_code=409,
            )
        query_vector = client.embed([query])[0]
        items = self.repository.vector_search(
            query_vector=query_vector,
            profile=client.profile,
            top_k=limit,
            policy_ids=filters.policy_ids,
            level_codes=filters.level_codes,
            category_codes=filters.category_codes,
        )
        return RetrievalBranchResult(
            items=items,
            metadata={
                "embedding_profile": {
                    "id": client.profile.id,
                    "provider": client.profile.provider,
                    "model": client.profile.model,
                    "dimensions": client.profile.dimensions,
                }
            },
        )
