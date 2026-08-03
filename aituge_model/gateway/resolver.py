from __future__ import annotations

import asyncio
import json
import socket
import time
from typing import Any

import dns.asyncresolver
import dns.exception
from aiohttp.abc import AbstractResolver, ResolveResult
from redis.asyncio import Redis
from redis.exceptions import RedisError


class SharedDualResolver(AbstractResolver):
    """Resolve configured public hosts through system and fallback DNS sources."""

    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str,
        public_hosts: set[str],
        fallback_nameservers: tuple[str, ...],
        ttl_seconds: int,
        timeout_seconds: float,
    ) -> None:
        self.redis = redis
        self.prefix = prefix.rstrip(":")
        self.public_hosts = {host.lower() for host in public_hosts}
        self.fallback_nameservers = fallback_nameservers
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._memory: dict[str, tuple[float, list[ResolveResult]]] = {}
        self.redis_available = True

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        key = self._cache_key(host, port, family)
        cached = self._memory.get(key)
        if cached is not None and cached[0] > time.monotonic():
            return list(cached[1])

        shared = await self._shared_get(key, host, port)
        if shared:
            self._memory[key] = (time.monotonic() + self.ttl_seconds, shared)
            return list(shared)

        system_task = asyncio.create_task(self._system_resolve(host, port, family))
        fallback_task = (
            asyncio.create_task(self._fallback_resolve(host, port, family))
            if host.lower() in self.public_hosts and self.fallback_nameservers
            else None
        )
        system_rows = await _safe_task(system_task)
        fallback_rows = await _safe_task(fallback_task) if fallback_task else []
        rows = _deduplicate([*system_rows, *fallback_rows])
        if not rows:
            raise OSError(f"No DNS address available for {host}.")

        self._memory[key] = (time.monotonic() + self.ttl_seconds, rows)
        await self._shared_set(key, rows)
        return list(rows)

    async def clear_host(
        self,
        host: str,
        port: int,
        family: socket.AddressFamily = socket.AF_UNSPEC,
    ) -> None:
        families = (
            (socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6)
            if family == socket.AF_UNSPEC
            else (family,)
        )
        keys = [self._cache_key(host, port, item) for item in families]
        for key in keys:
            self._memory.pop(key, None)
        try:
            if keys:
                await self.redis.delete(*[self._redis_key(key) for key in keys])
            self.redis_available = True
        except RedisError:
            self.redis_available = False

    def status(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [
            {
                "cacheKey": key,
                "expiresInSeconds": max(0, round(expires_at - now)),
                "addressCount": len(rows),
            }
            for key, (expires_at, rows) in sorted(self._memory.items())
            if expires_at > now
        ]

    async def close(self) -> None:
        self._memory.clear()

    async def _system_resolve(
        self,
        host: str,
        port: int,
        family: socket.AddressFamily,
    ) -> list[ResolveResult]:
        loop = asyncio.get_running_loop()
        rows = await asyncio.wait_for(
            loop.getaddrinfo(
                host,
                port,
                family=family,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            ),
            timeout=self.timeout_seconds,
        )
        return [
            _row(host, port, item_family, sockaddr[0])
            for item_family, _type, _proto, _canonname, sockaddr in rows
        ]

    async def _fallback_resolve(
        self,
        host: str,
        port: int,
        family: socket.AddressFamily,
    ) -> list[ResolveResult]:
        record_types: list[tuple[str, socket.AddressFamily]] = []
        if family in {socket.AF_UNSPEC, socket.AF_INET}:
            record_types.append(("A", socket.AF_INET))
        if family in {socket.AF_UNSPEC, socket.AF_INET6}:
            record_types.append(("AAAA", socket.AF_INET6))
        tasks = [
            asyncio.create_task(self._query_one(nameserver, host, record_type, row_family, port))
            for nameserver in self.fallback_nameservers
            for record_type, row_family in record_types
        ]
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            row
            for result in results
            if isinstance(result, list)
            for row in result
        ]

    async def _query_one(
        self,
        nameserver: str,
        host: str,
        record_type: str,
        family: socket.AddressFamily,
        port: int,
    ) -> list[ResolveResult]:
        resolver = dns.asyncresolver.Resolver(configure=False)
        resolver.nameservers = [nameserver]
        resolver.timeout = self.timeout_seconds
        resolver.lifetime = self.timeout_seconds
        try:
            answer = await resolver.resolve(host, record_type, lifetime=self.timeout_seconds)
        except (TimeoutError, dns.exception.DNSException):
            return []
        return [_row(host, port, family, str(item)) for item in answer]

    async def _shared_get(
        self,
        key: str,
        host: str,
        port: int,
    ) -> list[ResolveResult]:
        try:
            raw = await self.redis.get(self._redis_key(key))
            self.redis_available = True
        except RedisError:
            self.redis_available = False
            return []
        if not raw:
            return []
        try:
            values = json.loads(raw)
            return [
                _row(host, port, socket.AddressFamily(int(item["family"])), str(item["host"]))
                for item in values
            ]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return []

    async def _shared_set(self, key: str, rows: list[ResolveResult]) -> None:
        payload = json.dumps(
            [{"host": item["host"], "family": int(item["family"])} for item in rows],
            separators=(",", ":"),
        )
        try:
            await self.redis.setex(self._redis_key(key), self.ttl_seconds, payload)
            self.redis_available = True
        except RedisError:
            self.redis_available = False

    def _cache_key(self, host: str, port: int, family: socket.AddressFamily) -> str:
        return f"{host.lower()}:{port}:{int(family)}"

    def _redis_key(self, key: str) -> str:
        return f"{self.prefix}:dns:{key}"


async def _safe_task(task: asyncio.Task[list[ResolveResult]] | None) -> list[ResolveResult]:
    if task is None:
        return []
    try:
        return await task
    except (TimeoutError, OSError):
        return []


def _row(
    hostname: str,
    port: int,
    family: socket.AddressFamily,
    address: str,
) -> ResolveResult:
    return ResolveResult(
        hostname=hostname,
        host=address,
        port=port,
        family=family,
        proto=socket.IPPROTO_TCP,
        flags=socket.AI_NUMERICHOST,
    )


def _deduplicate(rows: list[ResolveResult]) -> list[ResolveResult]:
    seen: set[tuple[str, int]] = set()
    result: list[ResolveResult] = []
    for row in rows:
        key = (row["host"], int(row["family"]))
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result
