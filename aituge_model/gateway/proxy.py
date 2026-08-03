from __future__ import annotations

import asyncio
import json
import logging
import socket
import uuid
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
)
from tenacity.wait import wait_random_exponential

from aituge_model.config import ModelRegistry, SecretResolver

from .circuit import CircuitBreaker
from .errors import (
    GatewayError,
    RetryableUpstreamError,
    provider_status_error,
    request_rejected,
)
from .resolver import SharedDualResolver
from .settings import GatewaySettings

LOGGER = logging.getLogger(__name__)
COMPONENT_HEADER = "X-Aituge-Model-Component-ID"


@dataclass(frozen=True, slots=True)
class ComponentRoute:
    id: str
    kind: str
    mode: str
    provider: str
    model: str
    target_url: str
    api_key: str
    timeout_seconds: float


class ModelProxy:
    def __init__(
        self,
        *,
        settings: GatewaySettings,
        registry: ModelRegistry,
        secret_resolver: SecretResolver,
        session: aiohttp.ClientSession,
        connector: aiohttp.TCPConnector,
        resolver: SharedDualResolver,
        circuit: CircuitBreaker,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.session = session
        self.connector = connector
        self.resolver = resolver
        self.circuit = circuit
        self.routes = _build_routes(registry, secret_resolver)
        self._fallback_wait = wait_random_exponential(multiplier=0.5, max=2.0)

    async def handle(self, request: web.Request, kind: str) -> web.StreamResponse:
        request_id = request.headers.get("X-Request-ID", "").strip() or uuid.uuid4().hex
        component_id = request.headers.get(COMPONENT_HEADER, "").strip()
        route = self.routes.get(component_id)
        if route is None or route.kind != kind:
            raise request_rejected()
        try:
            body = await request.read()
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise request_rejected() from None
        if not isinstance(payload, dict) or str(payload.get("model", "")).strip() != route.model:
            raise request_rejected()

        if not await self.circuit.allow(route.id, request_id):
            raise GatewayError(
                "MODEL_CIRCUIT_OPEN",
                "模型服务暂时不可用，请稍后重试",
                status=503,
                retryable=True,
            )

        is_stream = kind == "llm" and payload.get("stream") is True
        try:
            upstream, first_chunk = await self._open_with_retry(
                route,
                body,
                request_id=request_id,
                prefetch=is_stream,
            )
        except RetryableUpstreamError:
            await self.circuit.record_failure(route.id)
            raise

        if is_stream:
            await self.circuit.record_success(route.id)
            return await self._stream_response(
                request,
                route,
                upstream,
                first_chunk,
                request_id,
            )

        try:
            response_body = await asyncio.wait_for(
                upstream.read(), timeout=route.timeout_seconds
            )
        except (TimeoutError, aiohttp.ClientError) as exc:
            upstream.close()
            await self.circuit.record_failure(route.id)
            raise _transport_error(exc) from exc
        finally:
            upstream.release()
        await self.circuit.record_success(route.id)
        return web.Response(
            body=response_body,
            status=upstream.status,
            headers=_response_headers(upstream.headers, request_id),
        )

    async def _open_with_retry(
        self,
        route: ComponentRoute,
        body: bytes,
        *,
        request_id: str,
        prefetch: bool,
    ) -> tuple[aiohttp.ClientResponse, bytes]:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.settings.max_attempts),
            wait=self._wait,
            retry=retry_if_exception_type(RetryableUpstreamError),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                return await self._open_once(
                    route,
                    body,
                    request_id=request_id,
                    prefetch=prefetch,
                )
        raise AssertionError("unreachable")

    async def _open_once(
        self,
        route: ComponentRoute,
        body: bytes,
        *,
        request_id: str,
        prefetch: bool,
    ) -> tuple[aiohttp.ClientResponse, bytes]:
        headers = {
            "Accept": "text/event-stream" if prefetch else "application/json",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        if route.api_key:
            headers["Authorization"] = f"Bearer {route.api_key}"
        try:
            upstream = await asyncio.wait_for(
                self.session.post(route.target_url, data=body, headers=headers),
                timeout=self.settings.first_byte_timeout_seconds,
            )
            if upstream.status >= 400:
                retry_after = _retry_after_seconds(upstream.headers.get("Retry-After"))
                error = provider_status_error(upstream.status, retry_after)
                upstream.release()
                raise error
            first_chunk = b""
            if prefetch:
                first_chunk = await asyncio.wait_for(
                    upstream.content.readany(),
                    timeout=self.settings.first_byte_timeout_seconds,
                )
                if not first_chunk:
                    upstream.release()
                    raise RetryableUpstreamError(
                        "MODEL_PROVIDER_UNAVAILABLE",
                        "模型服务暂时不可用，请稍后重试",
                    )
            return upstream, first_chunk
        except RetryableUpstreamError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            await self._clear_dns(route.target_url)
            raise _transport_error(exc) from exc

    async def _stream_response(
        self,
        request: web.Request,
        route: ComponentRoute,
        upstream: aiohttp.ClientResponse,
        first_chunk: bytes,
        request_id: str,
    ) -> web.StreamResponse:
        downstream = web.StreamResponse(
            status=upstream.status,
            headers=_response_headers(upstream.headers, request_id),
        )
        await downstream.prepare(request)
        try:
            await downstream.write(first_chunk)
            while True:
                chunk = await asyncio.wait_for(
                    upstream.content.readany(),
                    timeout=self.settings.stream_idle_timeout_seconds,
                )
                if not chunk:
                    break
                await downstream.write(chunk)
            await downstream.write_eof()
            return downstream
        except (TimeoutError, aiohttp.ClientError, ConnectionError):
            await self.circuit.record_failure(route.id)
            downstream.force_close()
            transport = request.transport
            if transport is not None:
                transport.abort()
            return downstream
        finally:
            upstream.release()

    async def _clear_dns(self, target_url: str) -> None:
        parsed = urlparse(target_url)
        if not parsed.hostname:
            return
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.connector.clear_dns_cache(parsed.hostname, port)
        await self.resolver.clear_host(parsed.hostname, port, socket.AF_UNSPEC)

    def _wait(self, retry_state: RetryCallState) -> float:
        outcome = retry_state.outcome
        error = outcome.exception() if outcome is not None else None
        if isinstance(error, RetryableUpstreamError) and error.retry_after is not None:
            return min(max(0.0, error.retry_after), self.settings.retry_after_max_seconds)
        return float(self._fallback_wait(retry_state))


def _build_routes(
    registry: ModelRegistry,
    secret_resolver: SecretResolver,
) -> dict[str, ComponentRoute]:
    routes: dict[str, ComponentRoute] = {}
    for kind, registrations in (
        ("llm", registry.llms),
        ("embedding", registry.embeddings),
        ("reranker", registry.rerankers),
    ):
        for registration in registrations.values():
            if registration.id in routes:
                raise ValueError(f"Duplicate model component id: {registration.id}")
            credential_ref = registration.credential_ref
            api_key = secret_resolver.resolve(
                credential_ref,
                required=registration.mode == "api",
            )
            timeout_seconds = float(getattr(registration, "timeout_seconds", 120.0))
            routes[registration.id] = ComponentRoute(
                id=registration.id,
                kind=kind,
                mode=registration.mode,
                provider=registration.provider,
                model=registration.model,
                target_url=_target_url(kind, registration.base_url),
                api_key=api_key,
                timeout_seconds=timeout_seconds,
            )
    return routes


def _target_url(kind: str, base_url: str) -> str:
    base = base_url.rstrip("/")
    suffixes = {
        "llm": ("/chat/completions", "/chat/completions"),
        "embedding": ("/embeddings", "/embeddings"),
        "reranker": (("/rerank", "/reranks"), "/reranks"),
    }
    accepted, suffix = suffixes[kind]
    accepted_values = (accepted,) if isinstance(accepted, str) else accepted
    return base if base.endswith(accepted_values) else f"{base}{suffix}"


def _transport_error(exc: BaseException) -> RetryableUpstreamError:
    if isinstance(exc, (asyncio.TimeoutError, aiohttp.ConnectionTimeoutError, aiohttp.SocketTimeoutError)):
        return RetryableUpstreamError(
            "MODEL_CONNECT_TIMEOUT",
            "模型服务连接超时，请稍后重试",
            status=504,
        )
    if isinstance(exc, aiohttp.ClientConnectorDNSError):
        return RetryableUpstreamError(
            "MODEL_DNS_RESOLUTION_FAILED",
            "模型服务域名解析失败，请稍后重试",
            status=503,
        )
    return RetryableUpstreamError(
        "MODEL_PROVIDER_UNAVAILABLE",
        "模型服务暂时不可用，请稍后重试",
        status=503,
    )


def _response_headers(headers: aiohttp.typedefs.LooseHeaders, request_id: str) -> dict[str, str]:
    source = {str(key).lower(): str(value) for key, value in headers.items()}
    result = {"X-Request-ID": request_id}
    for name in (
        "content-type",
        "cache-control",
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
    ):
        if name in source:
            result[name] = source[name]
    return result


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return max(0.0, parsed.timestamp() - __import__("time").time())
        except (TypeError, ValueError, OverflowError):
            return None
