import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus, urlunparse

from loguru import logger


@dataclass
class RedisConfig:
    host: str = "localhost"
    port: int = 6379
    password: str = ""
    username: str = ""
    db: int = 0
    ssl: bool = False


def _as_int(value: Optional[str], default: int) -> int:
    if not value:
        return default
    return int(value)


def load_redis_config() -> RedisConfig:
    return RedisConfig(
        host=os.getenv("REDIS_HOST", "localhost") or "localhost",
        port=_as_int(os.getenv("REDIS_PORT"), 6379),
        password=os.getenv("REDIS_PASSWORD", ""),
        username=os.getenv("REDIS_USERNAME", ""),
        db=_as_int(os.getenv("REDIS_DB"), 0),
        ssl=os.getenv("REDIS_SSL", "false").lower() == "true",
    )


def compose_redis_url(
    host: str,
    port: int,
    username: str = "",
    password: str = "",
    db: int = 0,
    ssl: bool = False,
) -> str:
    scheme = "rediss" if ssl else "redis"
    if username and password:
        netloc = f"{quote_plus(username)}:{quote_plus(password)}@{host}:{port}"
    elif password:
        netloc = f":{quote_plus(password)}@{host}:{port}"
    else:
        netloc = f"{host}:{port}"
    return urlunparse((scheme, netloc, f"/{db}", "", "", ""))


REDIS_CONFIG = load_redis_config()
REDIS_HOST = REDIS_CONFIG.host
REDIS_PORT = REDIS_CONFIG.port
REDIS_PASSWORD = REDIS_CONFIG.password
REDIS_USERNAME = REDIS_CONFIG.username
REDIS_DB = REDIS_CONFIG.db
REDIS_SSL = REDIS_CONFIG.ssl
REDIS_URL = compose_redis_url(
    host=REDIS_HOST,
    port=REDIS_PORT,
    username=REDIS_USERNAME,
    password=REDIS_PASSWORD,
    db=REDIS_DB,
    ssl=REDIS_SSL,
)

logger.info(f"Redis configured: {REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}")
