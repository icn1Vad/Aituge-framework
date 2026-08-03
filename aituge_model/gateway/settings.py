from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive.")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive.")
    return value


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    host: str = "0.0.0.0"
    port: int = 18_300
    internal_token: str = ""
    model_config_dir: str = ""
    model_secret_dir: str = ""
    fallback_dns: tuple[str, ...] = ("223.5.5.5", "223.6.6.6")
    dns_ttl_seconds: int = 60
    dns_timeout_seconds: float = 2.0
    happy_eyeballs_delay_seconds: float = 0.05
    connect_timeout_seconds: float = 5.0
    first_byte_timeout_seconds: float = 120.0
    stream_idle_timeout_seconds: float = 30.0
    max_attempts: int = 3
    retry_after_max_seconds: float = 10.0
    circuit_failure_threshold: int = 5
    circuit_open_seconds: int = 30
    redis_host: str = "framework-redis"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0
    redis_prefix: str = "model-gateway:v1"

    @classmethod
    def from_environment(cls) -> GatewaySettings:
        fallback_dns = tuple(
            value.strip()
            for value in os.getenv(
                "MODEL_GATEWAY_FALLBACK_DNS", "223.5.5.5,223.6.6.6"
            ).split(",")
            if value.strip()
        )
        config_dir = os.getenv("MODEL_CONFIG_DIR", "").strip()
        if not config_dir:
            config_dir = str(Path(__file__).resolve().parents[1] / "config")
        return cls(
            host=os.getenv("MODEL_GATEWAY_HOST", "0.0.0.0").strip(),
            port=_positive_int("MODEL_GATEWAY_PORT", 18_300),
            internal_token=os.getenv("MODEL_GATEWAY_TOKEN", "").strip(),
            model_config_dir=config_dir,
            model_secret_dir=os.getenv("MODEL_SECRET_DIR", "").strip(),
            fallback_dns=fallback_dns,
            dns_ttl_seconds=_positive_int("MODEL_GATEWAY_DNS_TTL_SECONDS", 60),
            dns_timeout_seconds=_positive_float("MODEL_GATEWAY_DNS_TIMEOUT_SECONDS", 2.0),
            happy_eyeballs_delay_seconds=_positive_float(
                "MODEL_GATEWAY_HAPPY_EYEBALLS_DELAY_SECONDS", 0.05
            ),
            connect_timeout_seconds=_positive_float(
                "MODEL_GATEWAY_CONNECT_TIMEOUT_SECONDS", 5.0
            ),
            first_byte_timeout_seconds=_positive_float(
                "MODEL_GATEWAY_FIRST_BYTE_TIMEOUT_SECONDS", 120.0
            ),
            stream_idle_timeout_seconds=_positive_float(
                "MODEL_GATEWAY_STREAM_IDLE_TIMEOUT_SECONDS", 30.0
            ),
            max_attempts=_positive_int("MODEL_GATEWAY_MAX_ATTEMPTS", 3),
            retry_after_max_seconds=_positive_float(
                "MODEL_GATEWAY_RETRY_AFTER_MAX_SECONDS", 10.0
            ),
            circuit_failure_threshold=_positive_int(
                "MODEL_GATEWAY_CIRCUIT_FAILURE_THRESHOLD", 5
            ),
            circuit_open_seconds=_positive_int(
                "MODEL_GATEWAY_CIRCUIT_OPEN_SECONDS", 30
            ),
            redis_host=os.getenv("MODEL_GATEWAY_REDIS_HOST", "framework-redis").strip(),
            redis_port=_positive_int("MODEL_GATEWAY_REDIS_PORT", 6379),
            redis_password=os.getenv("MODEL_GATEWAY_REDIS_PASSWORD", "").strip(),
            redis_db=int(os.getenv("MODEL_GATEWAY_REDIS_DB", "0").strip()),
            redis_prefix=os.getenv(
                "MODEL_GATEWAY_REDIS_PREFIX", "model-gateway:v1"
            ).strip(),
        )
