from __future__ import annotations

from typing import Any

from proof.config import Settings
from proof.errors import ProofError
from proof.infrastructure.embedding import OpenAICompatibleEmbeddingClient


class IntraConflictRetrievalService:
    """Prepare and query temporary embeddings for conflicts inside one draft policy."""

    def __init__(
        self,
        *,
        repository,
        settings: Settings | None = None,
        embedding_client=None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self._embedding_client = embedding_client

    @property
    def embedding_client(self):
        if self._embedding_client is None:
            if self.settings is None:
                raise RuntimeError("Embedding settings are not configured.")
            self._embedding_client = OpenAICompatibleEmbeddingClient(self.settings)
        return self._embedding_client

    def prepare(self, audit_run_id: str, document_id: str, units: list[dict[str, Any]]) -> None:
        unit_ids = [str(unit["id"]) for unit in units]
        if self.repository.draft_embeddings_ready(
            audit_run_id,
            document_id,
            unit_ids,
            self.embedding_client.profile,
        ):
            return
        try:
            vectors = self.embedding_client.embed([str(unit["text"]) for unit in units])
        except ProofError as exc:
            if exc.code == "embedding_too_long":
                raise ProofError(
                    "intra_conflict_embedding_incomplete",
                    "Every policy Chunk must have a temporary embedding for intra-policy conflict review.",
                    status_code=422,
                ) from exc
            raise
        if len(vectors) != len(units):
            raise ProofError(
                "intra_conflict_embedding_incomplete",
                "Temporary embedding response did not cover every policy Chunk.",
                status_code=502,
            )
        self.repository.replace_draft_embeddings(
            audit_run_id,
            document_id,
            units,
            vectors,
            self.embedding_client.profile,
        )

    def retrieve_for_unit(self, unit_id: str) -> dict[str, Any]:
        source, results = self.repository.retrieve_intra_conflict_candidates(
            unit_id,
            self.embedding_client.profile,
            top_k=10,
        )
        if source is None:
            raise ProofError("retrieval_unit_not_found", "Retrieval unit not found.", status_code=404)
        compact_source = {
            "id": source.get("id"),
            "text": source.get("text"),
            "clause_no_raw": source.get("clause_no_raw"),
            "clause_ordinal": source.get("clause_ordinal"),
        }
        compact_results = [
            {
                "id": item.get("id"),
                "text": item.get("text"),
                "clause_no_raw": item.get("clause_no_raw"),
                "clause_ordinal": item.get("clause_ordinal"),
            }
            for item in results
        ]
        return {"source": compact_source, "results": compact_results}
