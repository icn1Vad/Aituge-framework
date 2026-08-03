from __future__ import annotations

import hmac
import logging
import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from redis.asyncio import Redis
from redis.exceptions import RedisError

from aituge_model.config import SecretResolver, load_model_registry

from .circuit import CircuitBreaker
from .errors import GatewayError, error_response
from .proxy import ModelProxy
from .resolver import SharedDualResolver
from .settings import GatewaySettings

LOGGER = logging.getLogger(__name__)
RUNTIME_KEY: web.AppKey[GatewayRuntime] = web.AppKey("runtime")
SETTINGS_KEY: web.AppKey[GatewaySettings] = web.AppKey("settings")


class GatewayRuntime:
    def __init__(
        self,
        *,
        settings: GatewaySettings,
        redis: Redis,
        resolver: SharedDualResolver,
        connector: aiohttp.TCPConnector,
        session: aiohttp.ClientSession,
        circuit: CircuitBreaker,
        proxy: ModelProxy,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.resolver = resolver
        self.connector = connector
        self.session = session
        self.circuit = circuit
        self.proxy = proxy


def create_app(settings: GatewaySettings | None = None) -> web.Application:
    resolved_settings = settings or GatewaySettings.from_environment()
    if not resolved_settings.internal_token:
        raise ValueError("MODEL_GATEWAY_TOKEN is required.")
    app = web.Application(
        middlewares=[_error_middleware, _authentication_middleware],
        client_max_size=50 * 1024 * 1024,
    )
    app[SETTINGS_KEY] = resolved_settings
    app.cleanup_ctx.append(_runtime_context)
    app.router.add_post("/v1/chat/completions", _llm)
    app.router.add_post("/v1/embeddings", _embedding)
    app.router.add_post("/v1/reranks", _reranker)
    app.router.add_post("/v1/rerank", _reranker)
    app.router.add_get("/health", _health)
    app.router.add_get("/internal/status", _status)
    return app


@web.middleware
async def _error_middleware(request: web.Request, handler):
    request_id = request.headers.get("X-Request-ID", "").strip() or uuid.uuid4().hex
    try:
        return await handler(request)
    except GatewayError as exc:
        return error_response(exc, request_id)
    except web.HTTPException:
        raise
    except Exception:
        LOGGER.exception("Unhandled model gateway error request_id=%s", request_id)
        return error_response(
            GatewayError(
                "MODEL_PROVIDER_UNAVAILABLE",
                "模型服务暂时不可用，请稍后重试",
                status=503,
                retryable=True,
            ),
            request_id,
        )


@web.middleware
async def _authentication_middleware(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    settings = request.app[SETTINGS_KEY]
    provided = request.headers.get("Authorization", "")
    expected = f"Bearer {settings.internal_token}"
    if not hmac.compare_digest(provided, expected):
        raise web.HTTPUnauthorized(
            text='{"error":{"code":"MODEL_GATEWAY_UNAUTHORIZED","message":"Unauthorized"}}',
            content_type="application/json",
        )
    return await handler(request)


async def _runtime_context(app: web.Application) -> AsyncIterator[None]:
    settings = app[SETTINGS_KEY]
    registry = load_model_registry(settings.model_config_dir)
    secret_resolver = SecretResolver(
        secret_dir=settings.model_secret_dir or None,
    )
    redis = Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        db=settings.redis_db,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    session: aiohttp.ClientSession | None = None
    try:
        public_hosts = _public_hosts(registry)
        resolver = SharedDualResolver(
            redis,
            prefix=settings.redis_prefix,
            public_hosts=public_hosts,
            fallback_nameservers=settings.fallback_dns,
            ttl_seconds=settings.dns_ttl_seconds,
            timeout_seconds=settings.dns_timeout_seconds,
        )
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            family=socket.AF_UNSPEC,
            ttl_dns_cache=settings.dns_ttl_seconds,
            happy_eyeballs_delay=settings.happy_eyeballs_delay_seconds,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=settings.connect_timeout_seconds,
            sock_connect=settings.connect_timeout_seconds,
            sock_read=None,
        )
        session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        circuit = CircuitBreaker(
            redis,
            prefix=settings.redis_prefix,
            failure_threshold=settings.circuit_failure_threshold,
            open_seconds=settings.circuit_open_seconds,
        )
        proxy = ModelProxy(
            settings=settings,
            registry=registry,
            secret_resolver=secret_resolver,
            session=session,
            connector=connector,
            resolver=resolver,
            circuit=circuit,
        )
        app[RUNTIME_KEY] = GatewayRuntime(
            settings=settings,
            redis=redis,
            resolver=resolver,
            connector=connector,
            session=session,
            circuit=circuit,
            proxy=proxy,
        )
        yield
    finally:
        if session is not None:
            await session.close()
        await redis.aclose()


async def _llm(request: web.Request) -> web.StreamResponse:
    return await request.app[RUNTIME_KEY].proxy.handle(request, "llm")


async def _embedding(request: web.Request) -> web.StreamResponse:
    return await request.app[RUNTIME_KEY].proxy.handle(request, "embedding")


async def _reranker(request: web.Request) -> web.StreamResponse:
    return await request.app[RUNTIME_KEY].proxy.handle(request, "reranker")


async def _health(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    redis_ok = False
    try:
        redis_ok = bool(await runtime.redis.ping())
    except RedisError:
        redis_ok = False
    circuits = await runtime.circuit.status(sorted(runtime.proxy.routes))
    upstream_status = _upstream_status(circuits)
    shared_cache_ok = runtime.circuit.redis_available and runtime.resolver.redis_available
    return web.json_response(
        {
            "status": (
                "ok"
                if redis_ok and shared_cache_ok and upstream_status == "ok"
                else "degraded"
            ),
            "config": "ok",
            "redis": "ok" if redis_ok and shared_cache_ok else "degraded",
            "upstream": upstream_status,
            "components": len(runtime.proxy.routes),
        }
    )


async def _status(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    component_ids = sorted(runtime.proxy.routes)
    circuits = await runtime.circuit.status(component_ids)
    redis_status = (
        "ok"
        if runtime.circuit.redis_available and runtime.resolver.redis_available
        else "degraded"
    )
    upstream_status = _upstream_status(circuits)
    return web.json_response(
        {
            "status": (
                "ok"
                if redis_status == "ok" and upstream_status == "ok"
                else "degraded"
            ),
            "redis": redis_status,
            "upstream": upstream_status,
            "circuits": circuits,
            "dnsCache": runtime.resolver.status(),
        }
    )


def _upstream_status(circuits: dict[str, dict[str, Any]]) -> str:
    return (
        "degraded"
        if any(item.get("state") in {"OPEN", "HALF_OPEN"} for item in circuits.values())
        else "ok"
    )


def _public_hosts(registry: Any) -> set[str]:
    hosts: set[str] = set()
    for registrations in (registry.llms, registry.embeddings, registry.rerankers):
        for registration in registrations.values():
            if registration.mode != "api":
                continue
            parsed = urlparse(registration.base_url)
            if parsed.hostname:
                hosts.add(parsed.hostname.lower())
    return hosts
