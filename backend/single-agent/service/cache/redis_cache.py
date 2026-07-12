import asyncio
import os
from typing import Optional

from db.redis_conn import REDIS_URL
from loguru import logger
from redis.asyncio import Redis


class RedisCache:
    def __init__(self):
        self._client: Optional[Redis] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def get_client(self) -> Redis:
        current_loop = asyncio.get_running_loop()
        if (
            self._client is None
            or self._loop is None
            or self._loop.is_closed()
            or self._loop is not current_loop
        ):
            logger.info("Connecting to Redis for TUGE session cache.")
            self._client = Redis.from_url(
                REDIS_URL,
                decode_responses=True,
                # Session history is best-effort; an unavailable cache must not
                # hold up an Agent/Pipeline stage for the full network timeout.
                socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "0.5")),
                socket_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT", "0.25")),
            )
            self._loop = current_loop
        return self._client

    async def get(self, key: str) -> Optional[str]:
        return await self.get_client().get(key)

    async def set(self, key: str, value: str, ttl: Optional[int] = None) -> bool:
        return bool(await self.get_client().set(key, value, ex=ttl))

    async def set_if_absent(self, key: str, value: str, ttl: int) -> bool:
        return bool(await self.get_client().set(key, value, ex=ttl, nx=True))

    async def delete_if_value(self, key: str, value: str) -> bool:
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        end
        return 0
        """
        return bool(await self.get_client().eval(script, 1, key, value))

    async def delete(self, key: str) -> int:
        return await self.get_client().delete(key)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._loop = None


cache_manager = RedisCache()
