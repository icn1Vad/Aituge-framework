from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


class InMemoryEventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(run_id, ()))
        for queue in queues:
            queue.put_nowait(dict(event))

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._subscribers[run_id].add(queue)

        async def receive() -> AsyncIterator[dict[str, Any]]:
            while True:
                yield await queue.get()

        try:
            yield receive()
        finally:
            async with self._lock:
                self._subscribers[run_id].discard(queue)
                if not self._subscribers[run_id]:
                    self._subscribers.pop(run_id, None)


class RedisEventBroker:
    def __init__(self, redis_url: str) -> None:
        from redis.asyncio import Redis

        self._redis = Redis.from_url(redis_url, decode_responses=True)

    @staticmethod
    def _channel(run_id: str) -> str:
        return f"tuge:task-events:{run_id}"

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        await self._redis.publish(self._channel(run_id), json.dumps(event, ensure_ascii=False))

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel(run_id))

        async def receive() -> AsyncIterator[dict[str, Any]]:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    await asyncio.sleep(0.05)
                    continue
                data = json.loads(message["data"])
                if isinstance(data, dict):
                    yield data

        try:
            yield receive()
        finally:
            await pubsub.unsubscribe(self._channel(run_id))
            await pubsub.aclose()


_BROKER: InMemoryEventBroker | RedisEventBroker | None = None


def get_event_broker() -> InMemoryEventBroker | RedisEventBroker:
    global _BROKER
    if _BROKER is not None:
        return _BROKER
    backend = os.environ.get("TASK_EVENT_BROKER", "memory").strip().lower()
    if backend == "redis":
        from db.redis_conn import REDIS_URL

        _BROKER = RedisEventBroker(REDIS_URL)
    elif backend == "memory":
        _BROKER = InMemoryEventBroker()
    else:
        raise ValueError(f"Unsupported TASK_EVENT_BROKER '{backend}'.")
    return _BROKER


def reset_event_broker_for_test() -> None:
    global _BROKER
    _BROKER = None
