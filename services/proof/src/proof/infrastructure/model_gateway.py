from __future__ import annotations

from typing import Any

import httpx

from proof.errors import ProofError

COMPONENT_HEADER = "X-Aituge-Model-Component-ID"


def gateway_headers(
    api_key: str,
    component_id: str,
    *,
    via_gateway: bool,
) -> dict[str, str]:
    headers = {COMPONENT_HEADER: component_id} if via_gateway else {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def raise_gateway_error(response: httpx.Response) -> None:
    try:
        body: Any = response.json()
    except (TypeError, ValueError):
        return
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return
    code = str(error.get("code") or "").strip()
    if not code.startswith("MODEL_"):
        return
    message = str(error.get("message") or "模型服务暂时不可用，请稍后重试").strip()
    raise ProofError(
        code,
        message,
        status_code=response.status_code,
        details={"retryable": bool(error.get("retryable", True))},
    )
