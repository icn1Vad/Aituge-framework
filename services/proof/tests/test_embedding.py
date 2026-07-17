from __future__ import annotations

import httpx
import pytest

from proof.config import Settings
from proof.errors import ProofError
from proof.infrastructure.embedding import OpenAICompatibleEmbeddingClient


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def configured_settings() -> Settings:
    return Settings(
        database_url="postgresql://unused",
        embedding_base_url="https://embedding.example/v1",
        embedding_api_key="secret",
        embedding_model="example-model",
        embedding_dimensions=3,
    )


def test_embedding_is_optional() -> None:
    with pytest.raises(ProofError) as exc_info:
        OpenAICompatibleEmbeddingClient(Settings(_env_file=None))
    assert exc_info.value.code == "embedding_unconfigured"


def test_embedding_batches_ten_and_preserves_order(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_post(url, headers, json, timeout):
        calls.append(json["input"])
        return FakeResponse(
            200,
            {
                "data": [
                    {"index": index, "embedding": [float(index), 0.0, 1.0]}
                    for index, _ in enumerate(json["input"])
                ]
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenAICompatibleEmbeddingClient(configured_settings())
    vectors = client.embed([f"text-{index}" for index in range(11)])
    assert [len(call) for call in calls] == [10, 1]
    assert len(vectors) == 11


def test_embedding_dimension_mismatch_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(200, {"data": [{"index": 0, "embedding": [1.0, 2.0]}]}),
    )
    client = OpenAICompatibleEmbeddingClient(configured_settings())
    with pytest.raises(ProofError) as exc_info:
        client.embed(["text"])
    assert exc_info.value.code == "embedding_dimension_mismatch"


def test_embedding_too_long_response_is_identified(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(400, {"error": {"message": "maximum context length exceeded"}}),
    )
    client = OpenAICompatibleEmbeddingClient(configured_settings())
    with pytest.raises(ProofError) as exc_info:
        client.embed(["one complete clause"])
    assert exc_info.value.code == "embedding_too_long"
