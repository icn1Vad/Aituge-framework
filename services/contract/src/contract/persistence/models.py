from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class DocumentCreate:
    document_id: str
    tenant_id: str
    user_id: str
    contract_version_id: str
    original_name: str
    content_type: str
    file_type: str
    file_size: int
    content_hash: str
    storage_path: str


@dataclass(frozen=True, slots=True)
class ReviewCreate:
    review_id: str
    tenant_id: str
    user_id: str
    business_task_id: str
    contract_version_id: str
    document_id: str
    idempotency_key: str
    request_id: str
    request_fingerprint: str
    file_sha256: str
    perspective: str
    our_party_name: str | None
    contract_type: str
    review_attitude: str
    schema_version: str
    model_pack_id: str = "api-rerank"
    execution_mode: str = "FULL_REVIEW"
    party_resolution_id: str | None = None
    confirmed_party_a_name: str | None = None
    confirmed_party_b_name: str | None = None
    primary_playbook_id: str | None = None
    selected_playbook_ids: tuple[str, ...] = ("base_neutral",)
    roles_by_playbook: dict[str, str] = field(default_factory=dict)
    rule_release_id: str | None = None


@dataclass(frozen=True, slots=True)
class ParseGenerationReservation:
    generation_id: str
    generation_no: int
    reused: bool
    completed: bool


@dataclass(frozen=True, slots=True)
class DocumentBlockCreate:
    block_id: str
    block_no: int
    block_type: str
    text: str
    page_number: int | None
    paragraph_no: int | None
    char_start: int
    char_end: int
    heading_path: list[str]
    metadata: dict[str, Any]
