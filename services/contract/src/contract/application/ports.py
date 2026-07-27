from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from contract.api.models import (
    CancelReviewData,
    CreateReviewData,
    CreateReviewRequest,
    ReviewResultData,
    ReviewStatusData,
)


@dataclass(frozen=True, slots=True)
class InternalRequestContext:
    tenant_id: str
    user_id: str
    request_id: str
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class UploadedContract:
    filename: str
    content_type: str
    content: bytes


class ContractReviewService(Protocol):
    def health(self) -> dict[str, str]: ...

    def create_review(
        self,
        *,
        upload: UploadedContract,
        request: CreateReviewRequest,
        context: InternalRequestContext,
    ) -> CreateReviewData: ...

    def get_status(self, review_id: str, *, context: InternalRequestContext) -> ReviewStatusData: ...

    def get_result(self, review_id: str, *, context: InternalRequestContext) -> ReviewResultData: ...

    def cancel_review(self, review_id: str, *, context: InternalRequestContext) -> CancelReviewData: ...
