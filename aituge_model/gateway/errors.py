from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiohttp import web


@dataclass(slots=True)
class GatewayError(Exception):
    code: str
    message: str
    status: int = 503
    retryable: bool = True
    retry_after: float | None = None

    def __str__(self) -> str:
        return self.message


class RetryableUpstreamError(GatewayError):
    pass


def error_response(error: GatewayError, request_id: str) -> web.Response:
    payload: dict[str, Any] = {
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "requestId": request_id,
        }
    }
    headers = {"X-Request-ID": request_id}
    if error.retry_after is not None:
        headers["Retry-After"] = str(max(0, round(error.retry_after)))
    return web.json_response(payload, status=error.status, headers=headers)


def request_rejected() -> GatewayError:
    return GatewayError(
        "MODEL_REQUEST_REJECTED",
        "模型请求不符合接口要求",
        status=422,
        retryable=False,
    )


def provider_status_error(status: int, retry_after: float | None = None) -> GatewayError:
    if status == 429:
        return RetryableUpstreamError(
            "MODEL_RATE_LIMITED",
            "模型服务请求过多，请稍后重试",
            status=429,
            retryable=True,
            retry_after=retry_after,
        )
    if status in {408, 409} or status >= 500:
        return RetryableUpstreamError(
            "MODEL_PROVIDER_UNAVAILABLE",
            "模型服务暂时不可用，请稍后重试",
            status=503,
            retryable=True,
            retry_after=retry_after,
        )
    return GatewayError(
        "MODEL_REQUEST_REJECTED",
        "模型服务拒绝了本次请求",
        status=422,
        retryable=False,
    )
