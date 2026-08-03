from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from redis.exceptions import RedisError

from aituge_model.config import (
    EmbeddingModelRegistration,
    LlmModelRegistration,
    ModelRegistry,
    RerankerModelRegistration,
    SecretResolver,
)
from aituge_model.gateway.app import create_app
from aituge_model.gateway.circuit import CircuitBreaker
from aituge_model.gateway.errors import GatewayError, error_response
from aituge_model.gateway.proxy import COMPONENT_HEADER, ModelProxy
from aituge_model.gateway.resolver import SharedDualResolver, _row
from aituge_model.gateway.settings import GatewaySettings


class UnavailableRedis:
    async def get(self, *_args, **_kwargs):
        raise RedisError("offline")

    async def setex(self, *_args, **_kwargs):
        raise RedisError("offline")

    async def delete(self, *_args, **_kwargs):
        raise RedisError("offline")

    async def eval(self, *_args, **_kwargs):
        raise RedisError("offline")


class FakeCircuit:
    def __init__(self) -> None:
        self.successes = 0
        self.failures = 0

    async def allow(self, _component_id: str, _probe_id: str) -> bool:
        return True

    async def record_success(self, _component_id: str) -> None:
        self.successes += 1

    async def record_failure(self, _component_id: str) -> None:
        self.failures += 1


class NoopResolver:
    async def clear_host(self, *_args, **_kwargs) -> None:
        return None


@pytest.mark.asyncio
async def test_gateway_health_degrades_without_redis_and_internal_status_is_protected(
    tmp_path: Path,
) -> None:
    (tmp_path / "components.yaml").write_text(
        """\
version: 1
default_pack_id: local
llms:
  local-llm:
    mode: local
    provider: openai_compatible
    model: local-model
    base_url: http://local-llm:8000/v1
embeddings:
  local-embedding:
    mode: local
    provider: openai_compatible
    model: local-embedding
    base_url: http://local-embedding:8000/v1
    dimensions: 8
rerankers:
  local-reranker:
    mode: local
    provider: openai_compatible
    model: local-reranker
    base_url: http://local-reranker:8000/v1/rerank
""",
        encoding="utf-8",
    )
    packs = tmp_path / "packs"
    packs.mkdir()
    (packs / "local.yaml").write_text(
        """\
id: local
display_name: Local
llm: local-llm
embedding: local-embedding
reranker: local-reranker
""",
        encoding="utf-8",
    )
    client = TestClient(
        TestServer(
            create_app(
                GatewaySettings(
                    internal_token="token",
                    model_config_dir=str(tmp_path),
                    model_secret_dir=str(tmp_path / "secrets"),
                    redis_host="127.0.0.1",
                    redis_port=1,
                )
            )
        )
    )
    await client.start_server()
    try:
        health = await client.get("/health")
        unauthorized = await client.get("/internal/status")
        status = await client.get(
            "/internal/status",
            headers={"Authorization": "Bearer token"},
        )
        rejected = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer token"},
            json={"model": "local-model", "messages": []},
        )
        health_body = await health.json()
        status_body = await status.json()
        rejected_body = await rejected.json()
    finally:
        await client.close()

    assert health.status == 200
    assert health_body["status"] == "degraded"
    assert health_body["redis"] == "degraded"
    assert health_body["upstream"] == "ok"
    assert unauthorized.status == 401
    assert status.status == 200
    assert status_body["status"] == "degraded"
    assert status_body["redis"] == "degraded"
    assert status_body["upstream"] == "ok"
    assert rejected.status == 422
    assert rejected_body["error"]["code"] == "MODEL_REQUEST_REJECTED"
    assert rejected_body["error"]["retryable"] is False
    assert rejected_body["error"]["requestId"]


@pytest.mark.asyncio
async def test_dual_resolver_merges_system_and_fallback_addresses(monkeypatch) -> None:
    resolver = SharedDualResolver(
        UnavailableRedis(),
        prefix="test",
        public_hosts={"model.example"},
        fallback_nameservers=("223.5.5.5",),
        ttl_seconds=60,
        timeout_seconds=1,
    )

    async def system(_host, port, _family):
        return [_row("model.example", port, socket.AF_INET, "192.0.2.10")]

    async def fallback(_host, port, _family):
        return [
            _row("model.example", port, socket.AF_INET, "192.0.2.10"),
            _row("model.example", port, socket.AF_INET, "192.0.2.11"),
        ]

    monkeypatch.setattr(resolver, "_system_resolve", system)
    monkeypatch.setattr(resolver, "_fallback_resolve", fallback)

    rows = await resolver.resolve("model.example", 443, socket.AF_INET)

    assert [item["host"] for item in rows] == ["192.0.2.10", "192.0.2.11"]
    assert resolver.redis_available is False


@pytest.mark.asyncio
async def test_circuit_breaker_falls_back_to_memory_and_allows_one_half_open_probe(
    monkeypatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr("aituge_model.gateway.circuit.time.time", lambda: clock["now"])
    circuit = CircuitBreaker(
        UnavailableRedis(),
        prefix="test",
        failure_threshold=2,
        open_seconds=30,
    )

    assert await circuit.allow("llm", "first") is True
    await circuit.record_failure("llm")
    await circuit.record_failure("llm")
    assert await circuit.allow("llm", "blocked") is False

    clock["now"] += 31
    assert await circuit.allow("llm", "probe") is True
    assert await circuit.allow("llm", "second-probe") is False
    await circuit.record_success("llm")
    assert await circuit.allow("llm", "recovered") is True


@pytest.mark.asyncio
async def test_proxy_retries_before_first_stream_chunk_and_preserves_sse(tmp_path: Path) -> None:
    calls = {"count": 0}

    async def upstream_handler(_request: web.Request) -> web.StreamResponse:
        calls["count"] += 1
        if calls["count"] == 1:
            return web.json_response({"error": "temporary"}, status=503)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(_request)
        await response.write(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_handler)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    registration = LlmModelRegistration(
        id="local-test-llm",
        mode="local",
        provider="openai_compatible",
        model="test-model",
        base_url=str(upstream_server.make_url("/v1")),
    )
    registry = ModelRegistry(
        default_pack_id="unused",
        llms={registration.id: registration},
        embeddings={},
        rerankers={},
        packs={},
    )
    connector = aiohttp.TCPConnector()
    session = aiohttp.ClientSession(connector=connector)
    circuit = FakeCircuit()
    proxy = ModelProxy(
        settings=GatewaySettings(
            internal_token="token",
            max_attempts=2,
            first_byte_timeout_seconds=2,
            stream_idle_timeout_seconds=2,
        ),
        registry=registry,
        secret_resolver=SecretResolver(secret_dir=tmp_path, environment={}),
        session=session,
        connector=connector,
        resolver=NoopResolver(),
        circuit=circuit,
    )
    proxy._fallback_wait = lambda _state: 0
    gateway_app = web.Application()

    async def gateway_handler(request: web.Request) -> web.StreamResponse:
        return await proxy.handle(request, "llm")

    gateway_app.router.add_post("/v1/chat/completions", gateway_handler)
    client = TestClient(TestServer(gateway_app))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers={COMPONENT_HEADER: registration.id},
            json={"model": "test-model", "messages": [], "stream": True},
        )
        body = await response.text()
    finally:
        await client.close()
        await session.close()
        await upstream_server.close()

    assert response.status == 200
    assert body.endswith("data: [DONE]\n\n")
    assert calls["count"] == 2
    assert circuit.successes == 1


@pytest.mark.asyncio
async def test_proxy_supports_all_non_stream_routes_and_replaces_worker_auth(
    tmp_path: Path,
) -> None:
    upstream_requests: list[tuple[str, str]] = []

    async def upstream_handler(request: web.Request) -> web.Response:
        upstream_requests.append((request.path, request.headers.get("Authorization", "")))
        if request.path.endswith("/chat/completions"):
            return web.json_response({"choices": [{"message": {"content": "ok"}}]})
        if request.path.endswith("/embeddings"):
            return web.json_response({"data": [{"index": 0, "embedding": [1.0]}]})
        return web.json_response({"results": [{"index": 0, "relevance_score": 0.9}]})

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_handler)
    upstream_app.router.add_post("/v1/embeddings", upstream_handler)
    upstream_app.router.add_post("/v1/reranks", upstream_handler)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()
    base_url = str(upstream_server.make_url("/v1"))

    llm = LlmModelRegistration(
        id="api-llm",
        mode="api",
        provider="test",
        model="llm-model",
        base_url=base_url,
        credential_ref="upstream_key",
    )
    embedding = EmbeddingModelRegistration(
        id="api-embedding",
        mode="api",
        provider="test",
        model="embedding-model",
        base_url=base_url,
        dimensions=1,
        credential_ref="upstream_key",
    )
    reranker = RerankerModelRegistration(
        id="api-reranker",
        mode="api",
        provider="test",
        model="reranker-model",
        base_url=base_url,
        credential_ref="upstream_key",
    )
    registry = ModelRegistry(
        default_pack_id="unused",
        llms={llm.id: llm},
        embeddings={embedding.id: embedding},
        rerankers={reranker.id: reranker},
        packs={},
    )
    connector = aiohttp.TCPConnector()
    session = aiohttp.ClientSession(connector=connector)
    circuit = FakeCircuit()
    proxy = ModelProxy(
        settings=GatewaySettings(internal_token="token", max_attempts=1),
        registry=registry,
        secret_resolver=SecretResolver(
            secret_dir=tmp_path,
            environment={"MODEL_SECRET_UPSTREAM_KEY": "provider-token"},
        ),
        session=session,
        connector=connector,
        resolver=NoopResolver(),
        circuit=circuit,
    )
    gateway_app = web.Application()

    async def llm_handler(request: web.Request) -> web.StreamResponse:
        return await proxy.handle(request, "llm")

    async def embedding_handler(request: web.Request) -> web.StreamResponse:
        return await proxy.handle(request, "embedding")

    async def reranker_handler(request: web.Request) -> web.StreamResponse:
        return await proxy.handle(request, "reranker")

    gateway_app.router.add_post("/v1/chat/completions", llm_handler)
    gateway_app.router.add_post("/v1/embeddings", embedding_handler)
    gateway_app.router.add_post("/v1/rerank", reranker_handler)
    client = TestClient(TestServer(gateway_app))
    await client.start_server()
    try:
        common_headers = {"Authorization": "Bearer worker-token"}
        responses = [
            await client.post(
                "/v1/chat/completions",
                headers={**common_headers, COMPONENT_HEADER: llm.id},
                json={"model": llm.model, "messages": [], "stream": False},
            ),
            await client.post(
                "/v1/embeddings",
                headers={**common_headers, COMPONENT_HEADER: embedding.id},
                json={"model": embedding.model, "input": ["text"]},
            ),
            await client.post(
                "/v1/rerank",
                headers={**common_headers, COMPONENT_HEADER: reranker.id},
                json={"model": reranker.model, "query": "q", "documents": ["d"]},
            ),
        ]
        bodies = [await response.json() for response in responses]
    finally:
        await client.close()
        await session.close()
        await upstream_server.close()

    assert [response.status for response in responses] == [200, 200, 200]
    assert bodies[0]["choices"][0]["message"]["content"] == "ok"
    assert bodies[1]["data"][0]["embedding"] == [1.0]
    assert bodies[2]["results"][0]["relevance_score"] == 0.9
    assert upstream_requests == [
        ("/v1/chat/completions", "Bearer provider-token"),
        ("/v1/embeddings", "Bearer provider-token"),
        ("/v1/reranks", "Bearer provider-token"),
    ]
    assert circuit.successes == 3


@pytest.mark.asyncio
async def test_proxy_honors_retry_after_and_does_not_retry_ordinary_4xx(
    tmp_path: Path,
) -> None:
    calls = {"retry": 0, "reject": 0}

    async def upstream_handler(request: web.Request) -> web.Response:
        payload = await request.json()
        case = payload["case"]
        calls[case] += 1
        if case == "retry" and calls[case] == 1:
            return web.json_response(
                {"provider_secret": "must-not-leak"},
                status=429,
                headers={"Retry-After": "0"},
            )
        if case == "reject":
            return web.json_response(
                {"provider_secret": "must-not-leak"},
                status=400,
            )
        return web.json_response({"choices": [{"message": {"content": "ok"}}]})

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_handler)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()
    registration = LlmModelRegistration(
        id="retry-test-llm",
        mode="local",
        provider="openai_compatible",
        model="test-model",
        base_url=str(upstream_server.make_url("/v1")),
    )
    registry = ModelRegistry(
        default_pack_id="unused",
        llms={registration.id: registration},
        embeddings={},
        rerankers={},
        packs={},
    )
    connector = aiohttp.TCPConnector()
    session = aiohttp.ClientSession(connector=connector)
    circuit = FakeCircuit()
    proxy = ModelProxy(
        settings=GatewaySettings(
            internal_token="token",
            max_attempts=3,
            retry_after_max_seconds=1,
        ),
        registry=registry,
        secret_resolver=SecretResolver(secret_dir=tmp_path, environment={}),
        session=session,
        connector=connector,
        resolver=NoopResolver(),
        circuit=circuit,
    )
    proxy._fallback_wait = lambda _state: 0
    gateway_app = web.Application()

    async def gateway_handler(request: web.Request) -> web.StreamResponse:
        try:
            return await proxy.handle(request, "llm")
        except GatewayError as exc:
            return error_response(exc, "gateway-test-request")

    gateway_app.router.add_post("/v1/chat/completions", gateway_handler)
    client = TestClient(TestServer(gateway_app))
    await client.start_server()
    try:
        headers = {COMPONENT_HEADER: registration.id}
        retried = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": registration.model, "messages": [], "case": "retry"},
        )
        rejected = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": registration.model, "messages": [], "case": "reject"},
        )
        retried_body = await retried.json()
        rejected_body = await rejected.json()
    finally:
        await client.close()
        await session.close()
        await upstream_server.close()

    assert retried.status == 200
    assert retried_body["choices"][0]["message"]["content"] == "ok"
    assert calls["retry"] == 2
    assert rejected.status == 422
    assert rejected_body == {
        "error": {
            "code": "MODEL_REQUEST_REJECTED",
            "message": "模型服务拒绝了本次请求",
            "retryable": False,
            "requestId": "gateway-test-request",
        }
    }
    assert calls["reject"] == 1


@pytest.mark.asyncio
async def test_proxy_never_replays_stream_after_first_upstream_chunk(
    tmp_path: Path,
) -> None:
    calls = {"count": 0}

    async def upstream_handler(request: web.Request) -> web.StreamResponse:
        calls["count"] += 1
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n')
        await asyncio.sleep(0.05)
        transport = request.transport
        assert transport is not None
        transport.abort()
        return response

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_handler)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()
    registration = LlmModelRegistration(
        id="interrupted-stream-llm",
        mode="local",
        provider="openai_compatible",
        model="test-model",
        base_url=str(upstream_server.make_url("/v1")),
    )
    registry = ModelRegistry(
        default_pack_id="unused",
        llms={registration.id: registration},
        embeddings={},
        rerankers={},
        packs={},
    )
    connector = aiohttp.TCPConnector()
    session = aiohttp.ClientSession(connector=connector)
    circuit = FakeCircuit()
    proxy = ModelProxy(
        settings=GatewaySettings(
            internal_token="token",
            max_attempts=3,
            first_byte_timeout_seconds=2,
            stream_idle_timeout_seconds=2,
        ),
        registry=registry,
        secret_resolver=SecretResolver(secret_dir=tmp_path, environment={}),
        session=session,
        connector=connector,
        resolver=NoopResolver(),
        circuit=circuit,
    )
    proxy._fallback_wait = lambda _state: 0
    gateway_app = web.Application()

    async def gateway_handler(request: web.Request) -> web.StreamResponse:
        return await proxy.handle(request, "llm")

    gateway_app.router.add_post("/v1/chat/completions", gateway_handler)
    client = TestClient(TestServer(gateway_app))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers={COMPONENT_HEADER: registration.id},
            json={"model": registration.model, "messages": [], "stream": True},
        )
        with pytest.raises(aiohttp.ClientError):
            await response.read()
    finally:
        await client.close()
        await session.close()
        await upstream_server.close()

    assert calls["count"] == 1
    assert circuit.successes == 1
    assert circuit.failures == 1
