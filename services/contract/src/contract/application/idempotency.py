from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from contract.api.models import CreateReviewRequest


UNICODE_WHITE_SPACE = frozenset(
    list(range(0x0009, 0x000E))
    + [
        0x0020,
        0x0085,
        0x00A0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
    ]
)


def sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def normalize_party_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value)
    output: list[str] = []
    whitespace_pending = False
    for character in normalized:
        if ord(character) in UNICODE_WHITE_SPACE:
            whitespace_pending = bool(output)
            continue
        if whitespace_pending:
            output.append(" ")
            whitespace_pending = False
        output.append(character)
    result = "".join(output)
    return result or None


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def build_request_fingerprint(
    *,
    tenant_id: str,
    user_id: str,
    request: CreateReviewRequest,
    file_sha256: str,
) -> tuple[str, dict[str, str | None]]:
    payload: dict[str, str | None] = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "business_task_id": request.business_task_id,
        "contract_version_id": request.contract_version_id,
        "file_sha256": file_sha256,
        "perspective": request.perspective.value,
        "our_party_name": normalize_party_name(request.our_party_name),
        "contract_type": request.contract_type,
        "review_attitude": request.review_attitude,
        "schema_version": request.schema_version,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return "sha256:" + digest, payload
