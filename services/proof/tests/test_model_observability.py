from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from proof.config import Settings
from proof.errors import ProofError
from proof.infrastructure.embedding import OpenAICompatibleEmbeddingClient
from proof.infrastructure.model_observability import (
    ProofModelInvocationObserver,
)
from proof.infrastructure.reranking import OpenAICompatiblePolicyReranker
from proof.tenant import tenant_scope


_SECRET = "PROOF_MODEL_SECRET_BODY_VECTOR_CANARY"


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        embedding_base_url="https://models.example/v1",
        embedding_api_key="credential-not-logged",
        embedding_model="embed-model",
        embedding_dimensions=2,
        rerank_base_url="https://models.example/v1/reranks",
        rerank_api_key="credential-not-logged",
        rerank_model="rerank-model",
    )


def _observer() -> ProofModelInvocationObserver:
    return ProofModelInvocationObserver(
        enabled=True,
        database_url="postgresql://unused",
        fallback_tenant_id="",
    )


def _begin(observer: ProofModelInvocationObserver, logical: str = "logical-test"):
    return observer.begin(
        feature_code="proof.embedding",
        provider="openai_compatible",
        model_name="local/model-v1",
        mode="api",
        registration_id="registration-1",
        logical_call_id=logical,
        attempt_no=1,
        fallback_from_invocation_id=None,
    )


def _without_tenant(call):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(call).result()


def _valid_post(url, **_kwargs):
    if "embedding" in url:
        return _Response(
            {
                "data": [{"index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 0},
            }
        )
    return _Response(
        {
            "results": [{"index": 0, "relevance_score": 0.9}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1},
        }
    )


def _run_model(kind: str, observer: ProofModelInvocationObserver):
    settings = _settings()
    if kind == "embedding":
        return OpenAICompatibleEmbeddingClient(
            settings,
            observer=observer,
        ).embed(["sensitive input body"])
    return OpenAICompatiblePolicyReranker(
        settings,
        observer=observer,
    ).rerank(
        "sensitive query",
        [{"policy_title": "P", "text": "sensitive document"}],
        top_n=1,
    )


@pytest.mark.parametrize("kind", ["embedding", "rerank"])
@pytest.mark.parametrize("failure_stage", ["begin", "dispatch", "terminal"])
def test_telemetry_failure_preserves_model_result_and_redacts(
    kind: str,
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    observer = _observer()

    def fail(*_args, **_kwargs):
        raise RuntimeError(_SECRET)

    monkeypatch.setattr(observer, "_start", fail if failure_stage == "begin" else lambda *_: None)
    monkeypatch.setattr(
        observer,
        "_mark_dispatched",
        fail if failure_stage == "dispatch" else lambda *_: None,
    )
    monkeypatch.setattr(
        observer,
        "_terminal",
        fail if failure_stage == "terminal" else lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(httpx, "post", _valid_post)
    caplog.set_level(
        logging.WARNING,
        logger="proof.infrastructure.model_observability",
    )

    with tenant_scope("tenant-a"):
        result = _run_model(kind, observer)

    assert len(result) == 1
    assert _SECRET not in caplog.text
    assert "sensitive input body" not in caplog.text
    assert "sensitive document" not in caplog.text
    assert "proof_model_observability_degraded" in caplog.text


@pytest.mark.parametrize("kind", ["embedding", "rerank"])
def test_terminal_telemetry_failure_does_not_replace_business_validation_error(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = _observer()
    monkeypatch.setattr(observer, "_start", lambda *_: None)
    monkeypatch.setattr(observer, "_mark_dispatched", lambda *_: None)
    monkeypatch.setattr(
        observer,
        "_terminal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(_SECRET)),
    )
    if kind == "embedding":
        response = _Response(
            {"data": [{"index": 0, "embedding": [1.0]}]}
        )
        expected = "embedding_dimension_mismatch"
    else:
        response = _Response(
            {"results": [{"index": 99, "relevance_score": 0.9}]}
        )
        expected = "reranker_invalid_response"
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: response)

    with tenant_scope("tenant-a"), pytest.raises(ProofError) as exc_info:
        _run_model(kind, observer)

    assert exc_info.value.code == expected


def test_online_ledger_requires_authoritative_tenant_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = _observer()
    monkeypatch.setattr(
        observer,
        "_start",
        lambda *_: pytest.fail("must reject before ledger start"),
    )

    with pytest.raises(ProofError) as exc_info:
        _without_tenant(lambda: _begin(observer))

    assert exc_info.value.code == "model_observability_tenant_context_missing"


def test_offline_ledger_requires_explicit_test_tenant_and_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []
    observer = ProofModelInvocationObserver(
        enabled=True,
        database_url="postgresql://unused",
        fallback_tenant_id="proof-test-tenant",
        offline_mode=True,
        offline_run_id="benchmark-run-42",
    )
    monkeypatch.setattr(observer, "_start", captured.append)

    attempt = _without_tenant(lambda: _begin(observer))

    assert attempt.tenant_id == "proof-test-tenant"
    assert attempt.run_id == "benchmark-run-42"
    assert captured == [attempt]


@pytest.mark.parametrize(
    ("tenant_id", "run_id"),
    [("", "run-1"), ("tenant-1", "")],
)
def test_offline_ledger_rejects_incomplete_explicit_context(
    tenant_id: str,
    run_id: str,
) -> None:
    observer = ProofModelInvocationObserver(
        enabled=True,
        database_url="postgresql://unused",
        fallback_tenant_id=tenant_id,
        offline_mode=True,
        offline_run_id=run_id,
    )

    with pytest.raises(ProofError):
        _without_tenant(lambda: _begin(observer))


def test_two_tenant_concurrent_attempts_remain_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = _observer()
    captured: list[tuple[str, str]] = []
    lock = threading.Lock()

    def capture(attempt) -> None:
        with lock:
            captured.append((attempt.logical_call_id, attempt.tenant_id))

    monkeypatch.setattr(observer, "_start", capture)

    def run(tenant_id: str) -> None:
        with tenant_scope(tenant_id):
            _begin(observer, f"logical-{tenant_id}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ("tenant-a", "tenant-b")))

    assert sorted(captured) == [
        ("logical-tenant-a", "tenant-a"),
        ("logical-tenant-b", "tenant-b"),
    ]


def test_identity_conflict_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = _observer()
    monkeypatch.setattr(
        observer,
        "_start",
        lambda *_: (_ for _ in ()).throw(
            RuntimeError("PROOF_MODEL_DUPLICATE_ATTEMPT")
        ),
    )

    with tenant_scope("tenant-a"), pytest.raises(
        RuntimeError,
        match="PROOF_MODEL_DUPLICATE_ATTEMPT",
    ):
        _begin(observer)
