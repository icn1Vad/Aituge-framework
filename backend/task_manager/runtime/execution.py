from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from threading import Lock
from typing import AsyncIterator

from loguru import logger


_RUN_TASKS: dict[str, asyncio.Task[None]] = {}
_MEMORY_RUN_LOCKS: set[str] = set()
_MEMORY_RUN_LOCKS_GUARD = Lock()


@asynccontextmanager
async def executor_lock(run_id: str) -> AsyncIterator[bool]:
    backend = os.environ.get(
        "TASK_EXECUTOR_LOCK_BACKEND",
        "redis" if os.environ.get("TASK_EVENT_BROKER", "memory").strip().lower() == "redis" else "memory",
    ).strip().lower()
    if backend == "memory":
        with _MEMORY_RUN_LOCKS_GUARD:
            acquired = run_id not in _MEMORY_RUN_LOCKS
            if acquired:
                _MEMORY_RUN_LOCKS.add(run_id)
        try:
            yield acquired
        finally:
            if acquired:
                with _MEMORY_RUN_LOCKS_GUARD:
                    _MEMORY_RUN_LOCKS.discard(run_id)
        return
    if backend != "redis":
        raise ValueError(f"Unsupported TASK_EXECUTOR_LOCK_BACKEND '{backend}'.")

    from db.redis_conn import REDIS_URL
    from redis.asyncio import Redis

    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    key = f"run:{run_id}:executor_lock"
    token = secrets.token_hex(16)
    ttl_seconds = max(30, int(os.environ.get("TASK_EXECUTOR_LOCK_TTL_SECONDS", "1800")))
    acquired = bool(await redis.set(key, token, nx=True, ex=ttl_seconds))
    renewal: asyncio.Task[None] | None = None

    async def renew() -> None:
        while True:
            await asyncio.sleep(max(10, ttl_seconds // 3))
            await redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
                1,
                key,
                token,
                ttl_seconds,
            )

    if acquired:
        renewal = asyncio.create_task(renew(), name=f"task-manager-lock-renewal:{run_id}")
    try:
        yield acquired
    finally:
        if renewal is not None:
            renewal.cancel()
            try:
                await renewal
            except asyncio.CancelledError:
                pass
        if acquired:
            await redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                key,
                token,
            )
        await redis.aclose()


def start_background_run(run_id: str, coroutine: Awaitable[None]) -> None:
    existing = _RUN_TASKS.get(run_id)
    if existing is not None and not existing.done():
        raise ValueError(f"Run '{run_id}' already has an active executor.")
    task = asyncio.create_task(coroutine, name=f"task-manager-run:{run_id}")
    _RUN_TASKS[run_id] = task

    def cleanup(completed: asyncio.Task[None]) -> None:
        _RUN_TASKS.pop(run_id, None)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.error("Background TaskManager run {} failed: {}", run_id, error)

    task.add_done_callback(cleanup)


async def cancel_background_run(run_id: str) -> None:
    task = _RUN_TASKS.get(run_id)
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
