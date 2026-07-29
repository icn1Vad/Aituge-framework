from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from contract.api.models import CreateReviewRequest
from contract.model_pack import resolve_model_pack_id


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


FRAMEWORK_SERVICE = "ai-contract"
FRAMEWORK_TASK_TYPE = "contract.review.run"
FRAMEWORK_PIPELINE_VERSION = "contract-review-pipeline-v1"


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
        "model_pack_id": resolve_model_pack_id(request.model_pack_id),
        "file_sha256": file_sha256,
        "perspective": request.perspective.value,
        "our_party_name": normalize_party_name(request.our_party_name),
        "contract_type": request.contract_type,
        "review_attitude": request.review_attitude,
        "schema_version": request.schema_version,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return "sha256:" + digest, payload


def build_framework_request_fingerprint(
    *,
    tenant_id: str,
    user_id: str,
    review_id: str,
    attempt_no: int,
    business_task_id: str,
    contract_version_id: str,
    document_id: str,
    perspective: str,
    our_party_name: str | None,
    contract_type: str,
    review_attitude: str,
    schema_version: str,
) -> str:
    payload = {
        "service": FRAMEWORK_SERVICE,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "task_type": FRAMEWORK_TASK_TYPE,
        "pipeline_version": FRAMEWORK_PIPELINE_VERSION,
        "input": {
            "review_id": review_id,
            "attempt_no": attempt_no,
            "business_task_id": business_task_id,
            "contract_version_id": contract_version_id,
            "document_id": document_id,
            "perspective": perspective,
            "our_party_name": normalize_party_name(our_party_name),
            "contract_type": contract_type,
            "review_attitude": review_attitude,
            "schema_version": schema_version,
        },
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def build_dispatch_response_fingerprint(
    *,
    review_id: str,
    attempt_no: int,
    tenant_id: str,
    framework_task_id: str,
    framework_run_id: str,
) -> str:
    payload = {
        "review_id": review_id,
        "attempt_no": attempt_no,
        "tenant_id": tenant_id,
        "framework_task_id": framework_task_id,
        "framework_run_id": framework_run_id,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return "sha256:" + digest
