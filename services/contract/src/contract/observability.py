"""Minimal, opt-in tracing for the standalone contract service."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import Iterator

from fastapi import FastAPI, Request
from opentelemetry import context, propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.middleware.base import BaseHTTPMiddleware

_SAFE_ID = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
_CORRELATION_HEADERS = {
    "x-tenant-id": "tenant_id",
    "x-task-id": "task_id",
    "x-run-id": "run_id",
    "x-review-id": "review_id",
    "x-contract-review-id": "review_id",
    "x-business-task-id": "task_id",
    "x-request-id": "request_id",
}
_TRACER = trace.get_tracer("aituge.contract")
_CONFIGURED = False


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def configure_tracing_from_env(app: FastAPI, *, default_service_name: str = "contract-review-contract") -> bool:
    """Install propagation and server spans only when the deployment opts in."""

    global _CONFIGURED, _TRACER
    enabled = _truthy(os.getenv("OBSERVABILITY_TRACE_ENABLED")) or _truthy(os.getenv("OTEL_TRACES_ENABLED"))
    if not enabled:
        return False
    if not _CONFIGURED:
        endpoint = (os.getenv("OBSERVABILITY_TRACE_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "").strip()
        service_name = (os.getenv("OBSERVABILITY_TRACE_SERVICE_NAME") or default_service_name).strip()
        if not endpoint or not service_name:
            return False
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            endpoint = endpoint.split("://", 1)[1]
        resource = Resource.create({
            SERVICE_NAME: service_name,
            DEPLOYMENT_ENVIRONMENT: os.getenv("OBSERVABILITY_TRACE_ENVIRONMENT", "test"),
        })
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
        trace.set_tracer_provider(provider)
        _TRACER = trace.get_tracer("aituge.contract")
        propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))
        _CONFIGURED = True
    app.add_middleware(ContractTraceMiddleware)
    return True


def inject_trace_headers(headers: dict[str, str]) -> None:
    """Inject only W3C propagation headers into an outbound Framework request."""

    propagate.inject(headers)


@contextmanager
def contract_span(name: str, attributes: dict[str, str | int | bool | None] | None = None) -> Iterator[trace.Span]:
    with _TRACER.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            if value is not None and (not isinstance(value, str) or _SAFE_ID.fullmatch(value)):
                span.set_attribute(key, value)
        yield span


class ContractTraceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        extracted = propagate.extract(dict(request.headers))
        token = context.attach(extracted)
        with _TRACER.start_as_current_span(f"HTTP {request.method}") as span:
            for header, attribute in _CORRELATION_HEADERS.items():
                value = request.headers.get(header)
                if value and _SAFE_ID.fullmatch(value):
                    span.set_attribute(attribute, value)
            try:
                response = await call_next(request)
                route = request.scope.get("route")
                route_template = getattr(route, "path", None)
                if route_template:
                    span.update_name(f"{request.method} {route_template}")
                    span.set_attribute("http.route", route_template)
                span.set_attribute("http.response.status_code", response.status_code)
                if response.status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                return response
            except Exception as exc:
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                context.detach(token)
