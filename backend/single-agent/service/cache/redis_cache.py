from typing import Optional

from db.redis_conn import REDIS_URL
from loguru import logger
from redis.asyncio import Redis


class RedisCache:
    def __init__(self):
        self._client: Optional[Redis] = None

    def get_client(self) -> Redis:
        if self._client is None:
            logger.info("Connecting to Redis for TUGE session cache.")
            self._client = Redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_timeout=15,
                socket_connect_timeout=15,
            )
        return self._client

    async def get(self, key: str) -> Optional[str]:
        return await self.get_client().get(key)

    async def set(self, key: str, value: str, ttl: Optional[int] = None) -> bool:
        return bool(await self.get_client().set(key, value, ex=ttl))

    async def delete(self, key: str) -> int:
        return await self.get_client().delete(key)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


cache_manager = RedisCache()
