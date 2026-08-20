from __future__ import annotations

from types import SimpleNamespace

import pytest

from qianxuesen_mentor.config import Settings
from qianxuesen_mentor.embedding_backfill import EmbeddingBackfill
from qianxuesen_mentor.model_clients import _validate_embedding_response


def test_embedding_response_is_sorted_and_validated():
    body = {"data": [
        {"index": 1, "embedding": [0, 1]},
        {"index": 0, "embedding": [1, 0]},
    ]}
    assert _validate_embedding_response(body, expected_count=2, dimensions=2) == [[1.0, 0.0], [0.0, 1.0]]
    with pytest.raises(ValueError, match="dimension"):
        _validate_embedding_response({"data": [{"index": 0, "embedding": [1]}]},
                                     expected_count=1, dimensions=2)


class FakeModels:
    embedding_batch_size = 2
    profile_id = "profile"
    embedding_config = SimpleNamespace(model="qwen-embed", dimensions=2)

    def embed_with_usage(self, texts):
        return [[float(len(text)), 0.0] for text in texts], sum(map(len, texts))


class FakeRepository:
    def __init__(self):
        self.chunks = [{"id": "c1", "content": "甲"}, {"id": "c2", "content": "乙"}]
        self.cards = [{"evidence_type": "fact", "id": "f1", "text": "事实"}]
        self.chunk_vectors = []
        self.card_vectors = []

    def embedding_progress(self, **_kwargs):
        return {"chunks_total": 2, "chunks_indexed": len(self.chunk_vectors),
                "cards_total": 1, "cards_indexed": len(self.card_vectors)}

    def pending_chunk_embeddings(self, *, limit, **_kwargs):
        return self.chunks[len(self.chunk_vectors):len(self.chunk_vectors) + limit]

    def pending_card_embeddings(self, *, limit, **_kwargs):
        return self.cards[len(self.card_vectors):len(self.card_vectors) + limit]

    def index_chunk_embeddings(self, items, **_kwargs):
        self.chunk_vectors.extend(items)

    def index_card_embeddings(self, items, **_kwargs):
        self.card_vectors.extend(items)

    def refresh_document_index_status(self, **_kwargs):
        return None


def test_embedding_backfill_processes_existing_text_without_ingestion():
    repository = FakeRepository()
    result = EmbeddingBackfill(
        Settings(_env_file=None, embedding_progress_interval=1), repository, FakeModels(),
    ).run()
    assert result["new_embeddings"] == 3
    assert result["complete"] is True
    assert len(repository.chunk_vectors) == 2
    assert len(repository.card_vectors) == 1
