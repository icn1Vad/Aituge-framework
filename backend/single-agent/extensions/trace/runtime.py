"""Environment-gated OpenTelemetry setup for the Framework and workers.

Tracing is deliberately opt-in. The helper reads only deployment-provided
configuration and never puts credentials, prompts, documents, or model output
into span attributes.
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

from extensions.trace.base import init_instrument, setup_propagator
from extensions.trace.trace_config import TraceConfig


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_secret_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value[:4096] if value else None


def configure_tracing_from_env(app=None, *, default_service_name: str) -> bool:
    """Configure tracing once for an API app or a worker process.

    Missing or disabled configuration is a safe no-op so local development and
    existing deployments keep their previous behavior.
    """

    enabled = _truthy(os.getenv("OBSERVABILITY_TRACE_ENABLED")) or _truthy(
        os.getenv("OTEL_TRACES_ENABLED")
    )
    if not enabled:
        return False

    endpoint = (
        os.getenv("OBSERVABILITY_TRACE_ENDPOINT")
        or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or ""
    ).strip()
    service_name = (
        os.getenv("OBSERVABILITY_TRACE_SERVICE_NAME")
        or os.getenv("OTEL_SERVICE_NAME")
        or default_service_name
    ).strip()
    if not endpoint or not service_name:
        logger.warning("Tracing enabled but endpoint/service name is missing; tracing remains disabled")
        return False

    token = os.getenv("OBSERVABILITY_TRACE_TOKEN")
    if not token:
        token = _read_secret_file(os.getenv("OBSERVABILITY_TRACE_TOKEN_FILE"))
    config = TraceConfig(
        exporter_type=(os.getenv("OBSERVABILITY_TRACE_EXPORTER") or "grpc").strip().lower(),
        service_name=service_name,
        token=token,
        endpoint=endpoint,
        enabled=True,
    )
    if app is not None:
        setup_propagator(app)
    init_instrument(config)
    return True
