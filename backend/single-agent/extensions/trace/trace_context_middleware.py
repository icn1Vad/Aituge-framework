from __future__ import annotations

import os
import re
import uuid

from fastapi import Request
from loguru import logger
from opentelemetry import context, trace
from opentelemetry.baggage import get_baggage
from opentelemetry.propagate import get_global_textmap
from opentelemetry.trace import Status, StatusCode
from starlette.middleware.base import BaseHTTPMiddleware

from extensions.trace.context import AGENTSCOPE_REQUEST_ID_KEY, get_request_id, set_request_id

ENABLE_TRACE_CONTEXT_DEBUG = os.getenv("ENABLE_TRACE_CONTEXT_DEBUG", "false").lower() in {"true", "1", "yes", "y"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
_CORRELATION_HEADERS = {
    "x-tenant-id": "tenant.id",
    "x-task-id": "task.id",
    "x-run-id": "run.id",
    "x-review-id": "review.id",
    "x-contract-review-id": "review.id",
    "x-business-task-id": "task.id",
    "x-request-id": "request.id",
}


class TraceContextMiddleware(BaseHTTPMiddleware):
    """Extract W3C context and create a safe server span for each request."""

    async def dispatch(self, request: Request, call_next):
        carrier = dict(request.headers)
        propagator = get_global_textmap()
        extracted_context = propagator.extract(carrier=carrier)
        token = context.attach(extracted_context)
        request_id = get_baggage(AGENTSCOPE_REQUEST_ID_KEY) or uuid.uuid4().hex
        set_request_id(request_id)
        tracer = trace.get_tracer("aituge.http")
        with tracer.start_as_current_span(f"HTTP {request.method}") as span:
            self._set_request_attributes(span, request)
            if ENABLE_TRACE_CONTEXT_DEBUG:
                logger.info("Trace context received for {}", request.method)
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
                # Do not record exception messages; they can contain contract text or provider output.
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                set_request_id(None)
                context.detach(token)

    @staticmethod
    def _set_request_attributes(span, request: Request) -> None:
        span.set_attribute("http.request.method", request.method)
        request_id = get_request_id()
        if request_id and _SAFE_ID.fullmatch(request_id):
            span.set_attribute("request.id", request_id)
        for header, attribute in _CORRELATION_HEADERS.items():
            value = request.headers.get(header)
            if value and _SAFE_ID.fullmatch(value):
                span.set_attribute(attribute, value)
