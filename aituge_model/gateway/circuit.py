from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

_ALLOW_LUA = """
local state = redis.call('HGET', KEYS[1], 'state')
if (not state) or state == 'CLOSED' then
  return 1
end
local opened_at = tonumber(redis.call('HGET', KEYS[1], 'opened_at') or '0')
if state == 'OPEN' and (tonumber(ARGV[1]) - opened_at) < tonumber(ARGV[2]) then
  return 0
end
local acquired = redis.call('SET', KEYS[2], ARGV[3], 'NX', 'PX', ARGV[2])
if not acquired then
  return 0
end
redis.call('HSET', KEYS[1], 'state', 'HALF_OPEN')
redis.call('EXPIRE', KEYS[1], 86400)
return 1
"""

_FAILURE_LUA = """
local state = redis.call('HGET', KEYS[1], 'state') or 'CLOSED'
if state == 'HALF_OPEN' then
  redis.call('HSET', KEYS[1], 'state', 'OPEN', 'failures', ARGV[2], 'opened_at', ARGV[1])
  redis.call('DEL', KEYS[2])
  redis.call('EXPIRE', KEYS[1], 86400)
  return 'OPEN'
end
local failures = redis.call('HINCRBY', KEYS[1], 'failures', 1)
if failures >= tonumber(ARGV[2]) then
  redis.call('HSET', KEYS[1], 'state', 'OPEN', 'opened_at', ARGV[1])
else
  redis.call('HSET', KEYS[1], 'state', 'CLOSED')
end
redis.call('EXPIRE', KEYS[1], 86400)
return redis.call('HGET', KEYS[1], 'state')
"""


@dataclass(slots=True)
class _MemoryState:
    state: str = "CLOSED"
    failures: int = 0
    opened_at_ms: int = 0
    probe_until_ms: int = 0


class CircuitBreaker:
    """Redis-backed circuit state with a process-local fail-open fallback."""

    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str,
        failure_threshold: int,
        open_seconds: int,
    ) -> None:
        self.redis = redis
        self.prefix = prefix.rstrip(":")
        self.failure_threshold = failure_threshold
        self.open_ms = open_seconds * 1000
        self._memory: dict[str, _MemoryState] = {}
        self._memory_lock = asyncio.Lock()
        self.redis_available = True

    def _keys(self, component_id: str) -> tuple[str, str]:
        base = f"{self.prefix}:circuit:{component_id}"
        return base, f"{base}:probe"

    async def allow(self, component_id: str, probe_id: str) -> bool:
        state_key, probe_key = self._keys(component_id)
        now_ms = int(time.time() * 1000)
        try:
            allowed = await self.redis.eval(
                _ALLOW_LUA,
                2,
                state_key,
                probe_key,
                now_ms,
                self.open_ms,
                probe_id,
            )
            self.redis_available = True
            return bool(allowed)
        except RedisError:
            self.redis_available = False
            return await self._memory_allow(component_id, now_ms)

    async def record_success(self, component_id: str) -> None:
        state_key, probe_key = self._keys(component_id)
        try:
            await self.redis.delete(state_key, probe_key)
            self.redis_available = True
        except RedisError:
            self.redis_available = False
        async with self._memory_lock:
            self._memory.pop(component_id, None)

    async def record_failure(self, component_id: str) -> None:
        state_key, probe_key = self._keys(component_id)
        now_ms = int(time.time() * 1000)
        try:
            await self.redis.eval(
                _FAILURE_LUA,
                2,
                state_key,
                probe_key,
                now_ms,
                self.failure_threshold,
            )
            self.redis_available = True
            return
        except RedisError:
            self.redis_available = False
        async with self._memory_lock:
            state = self._memory.setdefault(component_id, _MemoryState())
            state.failures += 1
            if state.state == "HALF_OPEN" or state.failures >= self.failure_threshold:
                state.state = "OPEN"
                state.opened_at_ms = now_ms
                state.probe_until_ms = 0

    async def status(self, component_ids: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        try:
            pipe = self.redis.pipeline(transaction=False)
            for component_id in component_ids:
                pipe.hgetall(self._keys(component_id)[0])
            rows = await pipe.execute()
            self.redis_available = True
            for component_id, raw in zip(component_ids, rows, strict=True):
                decoded = {
                    _text(key): _text(value)
                    for key, value in (raw or {}).items()
                }
                result[component_id] = {
                    "state": decoded.get("state", "CLOSED"),
                    "failures": int(decoded.get("failures", "0")),
                }
            return result
        except RedisError:
            self.redis_available = False
        async with self._memory_lock:
            for component_id in component_ids:
                state = self._memory.get(component_id, _MemoryState())
                result[component_id] = {
                    "state": state.state,
                    "failures": state.failures,
                }
        return result

    async def _memory_allow(self, component_id: str, now_ms: int) -> bool:
        async with self._memory_lock:
            state = self._memory.setdefault(component_id, _MemoryState())
            if state.state == "CLOSED":
                return True
            if state.state == "OPEN" and now_ms - state.opened_at_ms < self.open_ms:
                return False
            if state.probe_until_ms > now_ms:
                return False
            state.state = "HALF_OPEN"
            state.probe_until_ms = now_ms + self.open_ms
            return True


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)
