from __future__ import annotations

import pytest

from proof.application.intra_conflict_retrieval import IntraConflictRetrievalService
from proof.errors import ProofError
from proof.infrastructure.embedding import EmbeddingProfile
from proof.infrastructure.postgres.repository import ProofRepository


class _EmbeddingClient:
    profile = EmbeddingProfile("profile-1", "test", "model", 3)

    def __init__(self, vectors=None):
        self.vectors = vectors or [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return self.vectors


class _Repository:
    def __init__(self, ready=False):
        self.ready = ready
        self.replacements = []
        self.retrieval_call = None

    def draft_embeddings_ready(self, audit_id, document_id, unit_ids, profile):
        self.ready_args = (audit_id, document_id, unit_ids, profile)
        return self.ready

    def replace_draft_embeddings(self, audit_id, document_id, units, vectors, profile):
        self.replacements.append((audit_id, document_id, units, vectors, profile))

    def retrieve_intra_conflict_candidates(self, unit_id, profile, *, top_k):
        self.retrieval_call = (unit_id, profile, top_k)
        source = {
            "id": unit_id, "text": "三十日内提交", "clause_no_raw": "第一条",
            "clause_ordinal": 1,
        }
        results = [{
            "id": "unit-2", "text": "十五日内提交", "clause_no_raw": "第二条",
            "clause_ordinal": 2, "score": 0.9,
        }]
        return source, results


UNITS = [
    {"id": "unit-1", "text": "三十日内提交", "clause_ordinal": 1},
    {"id": "unit-2", "text": "十五日内提交", "clause_ordinal": 2},
]


def test_prepare_generates_complete_temporary_embeddings_atomically():
    repository = _Repository()
    client = _EmbeddingClient()
    service = IntraConflictRetrievalService(repository=repository, embedding_client=client)

    service.prepare("audit-1", "document-1", UNITS)

    assert client.calls == [["三十日内提交", "十五日内提交"]]
    assert repository.replacements[0][0:2] == ("audit-1", "document-1")
    assert repository.replacements[0][3] == client.vectors


def test_prepare_reuses_same_run_profile_and_chunk_set():
    repository = _Repository(ready=True)
    client = _EmbeddingClient()
    service = IntraConflictRetrievalService(repository=repository, embedding_client=client)

    service.prepare("audit-1", "document-1", UNITS)

    assert client.calls == []
    assert repository.replacements == []


def test_prepare_rejects_incomplete_embedding_response():
    service = IntraConflictRetrievalService(
        repository=_Repository(), embedding_client=_EmbeddingClient(vectors=[[1.0, 0.0, 0.0]])
    )

    with pytest.raises(ProofError) as exc_info:
        service.prepare("audit-1", "document-1", UNITS)

    assert exc_info.value.code == "intra_conflict_embedding_incomplete"


def test_retrieve_uses_fixed_top_ten_and_returns_only_agent_fields():
    repository = _Repository(ready=True)
    client = _EmbeddingClient()
    service = IntraConflictRetrievalService(repository=repository, embedding_client=client)

    result = service.retrieve_for_unit("unit-1")

    assert repository.retrieval_call == ("unit-1", client.profile, 10)
    assert result == {
        "source": {
            "id": "unit-1", "text": "三十日内提交",
            "clause_no_raw": "第一条", "clause_ordinal": 1,
        },
        "results": [{
            "ref": "C01", "id": "unit-2", "text": "十五日内提交",
            "clause_no_raw": "第二条", "clause_ordinal": 2,
        }],
    }


class _SqlRows:
    def fetchall(self):
        return []


class _SqlConnection:
    def __init__(self):
        self.query = ""
        self.params = ()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def execute(self, query, params):
        self.query = query
        self.params = params
        return _SqlRows()


def test_repository_query_limits_candidates_to_same_document_and_excludes_source():
    connection = _SqlConnection()
    repository = object.__new__(ProofRepository)
    repository.connect = lambda: connection
    repository.get_conflict_source_unit = lambda unit_id: {"id": unit_id}
    profile = EmbeddingProfile("profile-1", "test", "model", 3)

    source, results = repository.retrieve_intra_conflict_candidates("unit-1", profile, top_k=10)

    assert source["id"] == "unit-1"
    assert results == []
    assert "u.document_id = ar.document_id" in connection.query
    assert "u.id <> %s" in connection.query
    assert connection.params[-1] == 10
