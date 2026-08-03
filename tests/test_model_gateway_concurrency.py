from __future__ import annotations

import asyncio
import os
import socket
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from redis.asyncio import Redis
from redis.exceptions import RedisError

from aituge_model.config import LlmModelRegistration, ModelRegistry, SecretResolver
from aituge_model.gateway.circuit import CircuitBreaker
from aituge_model.gateway.proxy import COMPONENT_HEADER, ModelProxy
from aituge_model.gateway.resolver import SharedDualResolver, _row
from aituge_model.gateway.settings import GatewaySettings


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
    async def clear_host(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class SwitchableRedis:
    """Small fault-injection wrapper around a real Redis connection."""

    def __init__(self, delegate: Redis) -> None:
        self.delegate = delegate
        self.readable = True
        self.writable = True

    async def get(self, *args: Any, **kwargs: Any):
        self._require_readable()
        return await self.delegate.get(*args, **kwargs)

    async def setex(self, *args: Any, **kwargs: Any):
        self._require_writable()
        return await self.delegate.setex(*args, **kwargs)

    async def delete(self, *args: Any, **kwargs: Any):
        self._require_writable()
        return await self.delegate.delete(*args, **kwargs)

    async def eval(self, *args: Any, **kwargs: Any):
        self._require_readable()
        self._require_writable()
        return await self.delegate.eval(*args, **kwargs)

    def pipeline(self, *args: Any, **kwargs: Any):
        return SwitchablePipeline(self, self.delegate.pipeline(*args, **kwargs))

    def _require_readable(self) -> None:
        if not self.readable:
            raise RedisError("simulated Redis read outage")

    def _require_writable(self) -> None:
        if not self.writable:
            raise RedisError("simulated Redis write outage")


class SwitchablePipeline:
    def __init__(self, owner: SwitchableRedis, delegate: Any) -> None:
        self.owner = owner
        self.delegate = delegate

    def hgetall(self, *args: Any, **kwargs: Any) -> SwitchablePipeline:
        self.delegate.hgetall(*args, **kwargs)
        return self

    async def execute(self):
        self.owner._require_readable()
        return await self.delegate.execute()


def _redis_url() -> str:
    value = os.getenv("MODEL_GATEWAY_TEST_REDIS_URL", "").strip()
    if not value:
        pytest.skip("MODEL_GATEWAY_TEST_REDIS_URL is not configured")
    return value


@pytest.mark.asyncio
async def test_gateway_handles_ten_parallel_requests_repeatedly(tmp_path: Path) -> None:
    calls = 0
    active = 0
    peak_active = 0
    state_lock = asyncio.Lock()

    async def upstream_handler(_request: web.Request) -> web.Response:
        nonlocal calls, active, peak_active
        async with state_lock:
            calls += 1
            active += 1
            peak_active = max(peak_active, active)
        await asyncio.sleep(0.03)
        async with state_lock:
            active -= 1
        return web.json_response({"choices": [{"message": {"content": "ok"}}]})

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_handler)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()
    registration = LlmModelRegistration(
        id="parallel-llm",
        mode="local",
        provider="openai_compatible",
        model="parallel-model",
        base_url=str(upstream_server.make_url("/v1")),
    )
    registry = ModelRegistry(
        default_pack_id="unused",
        llms={registration.id: registration},
        embeddings={},
        rerankers={},
        packs={},
    )
    connector = aiohttp.TCPConnector(limit=100)
    session = aiohttp.ClientSession(connector=connector)
    circuit = FakeCircuit()
    proxy = ModelProxy(
        settings=GatewaySettings(internal_token="token", max_attempts=1),
        registry=registry,
        secret_resolver=SecretResolver(secret_dir=tmp_path, environment={}),
        session=session,
        connector=connector,
        resolver=NoopResolver(),
        circuit=circuit,
    )
    gateway_app = web.Application()

    async def gateway_handler(request: web.Request) -> web.StreamResponse:
        return await proxy.handle(request, "llm")

    gateway_app.router.add_post("/v1/chat/completions", gateway_handler)
    client = TestClient(TestServer(gateway_app))
    await client.start_server()

    async def invoke(sequence: int) -> tuple[int, str]:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                COMPONENT_HEADER: registration.id,
                "X-Request-ID": f"parallel-{sequence}",
            },
            json={"model": registration.model, "messages": []},
        )
        payload = await response.json()
        return response.status, payload["choices"][0]["message"]["content"]

    try:
        for round_number in range(3):
            results = await asyncio.gather(
                *(invoke(round_number * 10 + index) for index in range(10))
            )
            assert results == [(200, "ok")] * 10
    finally:
        await client.close()
        await session.close()
        await upstream_server.close()

    assert calls == 30
    assert peak_active == 10
    assert circuit.successes == 30
    assert circuit.failures == 0


@pytest.mark.asyncio
async def test_two_workers_share_atomic_circuit_state_under_ten_concurrent_writes() -> None:
    redis = Redis.from_url(_redis_url())
    prefix = f"model-gateway:test:{uuid.uuid4().hex}"
    component = "shared-llm"
    state_key = f"{prefix}:circuit:{component}"
    probe_key = f"{state_key}:probe"
    first = CircuitBreaker(redis, prefix=prefix, failure_threshold=5, open_seconds=30)
    second = CircuitBreaker(redis, prefix=prefix, failure_threshold=5, open_seconds=30)
    try:
        await redis.ping()
        await asyncio.gather(
            *(
                (first if index % 2 == 0 else second).record_failure(component)
                for index in range(10)
            )
        )
        first_status, second_status = await asyncio.gather(
            first.status([component]),
            second.status([component]),
        )
        assert first_status[component] == {"state": "OPEN", "failures": 10}
        assert second_status == first_status

        await redis.hset(state_key, mapping={"state": "OPEN", "opened_at": 0})
        await redis.delete(probe_key)
        decisions = await asyncio.gather(
            *(
                (first if index % 2 == 0 else second).allow(component, f"probe-{index}")
                for index in range(10)
            )
        )
        assert decisions.count(True) == 1
        assert decisions.count(False) == 9

        await second.record_success(component)
        recovered = await first.status([component])
        assert recovered[component] == {"state": "CLOSED", "failures": 0}
    finally:
        await redis.delete(state_key, probe_key)
        await redis.aclose()


@pytest.mark.asyncio
async def test_redis_outage_uses_memory_then_rejoins_shared_state() -> None:
    redis = Redis.from_url(_redis_url())
    prefix = f"model-gateway:test:{uuid.uuid4().hex}"
    component = "recovering-llm"
    state_key = f"{prefix}:circuit:{component}"
    probe_key = f"{state_key}:probe"
    switchable = SwitchableRedis(redis)
    degraded = CircuitBreaker(
        switchable, prefix=prefix, failure_threshold=2, open_seconds=30
    )
    peer = CircuitBreaker(redis, prefix=prefix, failure_threshold=2, open_seconds=30)
    try:
        await redis.ping()
        switchable.readable = False
        switchable.writable = False
        await degraded.record_failure(component)
        await degraded.record_failure(component)
        assert await degraded.allow(component, "blocked-in-memory") is False
        assert degraded.redis_available is False

        peer_status = await peer.status([component])
        assert peer_status[component] == {"state": "CLOSED", "failures": 0}

        switchable.readable = True
        switchable.writable = True
        recovered_status = await degraded.status([component])
        assert recovered_status[component] == {"state": "CLOSED", "failures": 0}
        assert degraded.redis_available is True

        await degraded.record_failure(component)
        shared_status = await peer.status([component])
        assert shared_status[component] == {"state": "CLOSED", "failures": 1}
    finally:
        await redis.delete(state_key, probe_key)
        await redis.aclose()


@pytest.mark.asyncio
async def test_dns_cache_survives_redis_read_write_outages_and_recovers() -> None:
    redis = Redis.from_url(_redis_url())
    prefix = f"model-gateway:test:{uuid.uuid4().hex}"
    host = "model.example"
    switchable = SwitchableRedis(redis)
    resolver = SharedDualResolver(
        switchable,
        prefix=prefix,
        public_hosts={host},
        fallback_nameservers=("223.5.5.5",),
        ttl_seconds=60,
        timeout_seconds=1,
    )
    system_calls = 0

    async def system(_host: str, port: int, _family: socket.AddressFamily):
        nonlocal system_calls
        system_calls += 1
        return [_row(host, port, socket.AF_INET, "192.0.2.10")]

    async def fallback(_host: str, port: int, _family: socket.AddressFamily):
        return [_row(host, port, socket.AF_INET, "192.0.2.11")]

    resolver._system_resolve = system  # type: ignore[method-assign]
    resolver._fallback_resolve = fallback  # type: ignore[method-assign]
    cache_pattern = f"{prefix}:dns:*"
    try:
        switchable.writable = False
        first = await resolver.resolve(host, 443, socket.AF_INET)
        second = await resolver.resolve(host, 443, socket.AF_INET)
        assert [row["host"] for row in first] == ["192.0.2.10", "192.0.2.11"]
        assert second == first
        assert system_calls == 1
        assert resolver.redis_available is False

        switchable.writable = True
        switchable.readable = False
        await resolver.clear_host(host, 443, socket.AF_INET)
        third = await resolver.resolve(host, 443, socket.AF_INET)
        assert third == first
        assert system_calls == 2

        switchable.readable = True
        await resolver.clear_host(host, 443, socket.AF_INET)
        fourth = await resolver.resolve(host, 443, socket.AF_INET)
        assert fourth == first
        assert resolver.redis_available is True
    finally:
        keys = [key async for key in redis.scan_iter(match=cache_pattern)]
        if keys:
            await redis.delete(*keys)
        await resolver.close()
        await redis.aclose()
