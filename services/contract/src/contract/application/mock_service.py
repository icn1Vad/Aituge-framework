from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from contract.api.models import (
    CancelReviewData,
    CreateReviewData,
    CreateReviewRequest,
    PartyResolutionCreateData,
    PartyResolutionCreateRequest,
    PartyResolutionStatusData,
    ReviewResultData,
    ReviewStatus,
    ReviewStatusData,
)
from contract.application.idempotency import (
    build_party_resolution_fingerprint,
    build_request_fingerprint,
    sha256_bytes,
)
from contract.application.ports import InternalRequestContext, UploadedContract
from contract.errors import ContractError
from contract.model_pack import resolve_model_pack_id


@dataclass(frozen=True, slots=True)
class MockReviewRecord:
    review_id: str
    document_id: str
    tenant_id: str
    user_id: str
    business_task_id: str
    contract_version_id: str
    model_pack_id: str
    idempotency_key: str
    request_fingerprint: str
    file_sha256: str
    status: ReviewStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MockPartyResolutionRecord:
    resolution_id: str
    document_id: str
    tenant_id: str
    user_id: str
    contract_version_id: str
    model_pack_id: str
    idempotency_key: str
    request_fingerprint: str
    status: ReviewStatus
    party_a_name: str
    party_b_name: str
    created_at: datetime
    updated_at: datetime


class InMemoryContractReviewService:
    """Protocol-faithful development adapter; it never returns a fabricated AI result."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, MockReviewRecord] = {}
        self._business_keys: dict[tuple[str, str], str] = {}
        self._idempotency_keys: dict[tuple[str, str, str], str] = {}
        self._party_resolutions: dict[str, MockPartyResolutionRecord] = {}
        self._party_resolution_idempotency_keys: dict[tuple[str, str, str], str] = {}

    def health(self) -> dict[str, str]:
        return {"status": "UP", "service": "contract", "schema_version": "1.0", "mode": "mock"}

    def create_party_resolution(
        self,
        *,
        upload: UploadedContract,
        request: PartyResolutionCreateRequest,
        context: InternalRequestContext,
    ) -> PartyResolutionCreateData:
        if not context.idempotency_key:
            raise ContractError(
                "INVALID_REQUEST",
                "Idempotency-Key is required",
                status_code=400,
                user_action_required=True,
            )
        request = request.model_copy(
            update={"model_pack_id": resolve_model_pack_id(request.model_pack_id)}
        )
        fingerprint = build_party_resolution_fingerprint(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            request=request,
            file_sha256=sha256_bytes(upload.content),
        )
        key = (context.tenant_id, context.user_id, context.idempotency_key)
        with self._lock:
            existing_id = self._party_resolution_idempotency_keys.get(key)
            if existing_id is not None:
                record = self._party_resolutions[existing_id]
                if record.request_fingerprint != fingerprint:
                    raise self._idempotency_conflict(record.resolution_id)
                return self._party_resolution_create_data(record, reused=True)
            now = datetime.now(timezone.utc)
            record = MockPartyResolutionRecord(
                resolution_id=f"resolution-{uuid.uuid4().hex}",
                document_id=f"document-{uuid.uuid4().hex}",
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                contract_version_id=request.contract_version_id,
                model_pack_id=request.model_pack_id or "",
                idempotency_key=context.idempotency_key,
                request_fingerprint=fingerprint,
                status=ReviewStatus.SUCCEEDED,
                party_a_name="甲方单位",
                party_b_name="乙方单位",
                created_at=now,
                updated_at=now,
            )
            self._party_resolutions[record.resolution_id] = record
            self._party_resolution_idempotency_keys[key] = record.resolution_id
            return self._party_resolution_create_data(record, reused=False)

    def get_party_resolution(
        self,
        resolution_id: str,
        *,
        context: InternalRequestContext,
    ) -> PartyResolutionStatusData:
        with self._lock:
            record = self._party_resolutions.get(resolution_id)
            if record is None:
                raise ContractError("REVIEW_NOT_FOUND", "Contract party resolution does not exist", status_code=404)
            if record.tenant_id != context.tenant_id or record.user_id != context.user_id:
                raise ContractError("ACCESS_DENIED", "Contract party resolution is not accessible", status_code=403)
            return PartyResolutionStatusData(
                resolution_id=record.resolution_id,
                contract_version_id=record.contract_version_id,
                model_pack_id=record.model_pack_id,
                status=record.status,
                current_stage=None,
                document_id=record.document_id,
                framework_task_id=None,
                framework_run_id=None,
                framework_attempt_no=None,
                error=None,
                party_a_name=record.party_a_name,
                party_b_name=record.party_b_name,
                updated_at=record.updated_at,
            )

    def create_review(
        self,
        *,
        upload: UploadedContract,
        request: CreateReviewRequest,
        context: InternalRequestContext,
    ) -> CreateReviewData:
        if not context.idempotency_key:
            raise ContractError(
                "INVALID_REQUEST",
                "Idempotency-Key is required",
                status_code=400,
                user_action_required=True,
            )
        request = request.model_copy(
            update={"model_pack_id": resolve_model_pack_id(request.model_pack_id)}
        )
        file_sha256 = sha256_bytes(upload.content)
        fingerprint, _ = build_request_fingerprint(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            request=request,
            file_sha256=file_sha256,
        )
        business_key = (context.tenant_id, request.business_task_id)
        idempotency_key = (context.tenant_id, context.user_id, context.idempotency_key)

        with self._lock:
            existing_ids = {
                review_id
                for review_id in (
                    self._business_keys.get(business_key),
                    self._idempotency_keys.get(idempotency_key),
                )
                if review_id is not None
            }
            if existing_ids:
                if len(existing_ids) != 1:
                    raise self._idempotency_conflict()
                record = self._records[next(iter(existing_ids))]
                if record.request_fingerprint != fingerprint:
                    raise self._idempotency_conflict(record.review_id)
                return self._create_data(record, reused=True)

            now = datetime.now(timezone.utc)
            record = MockReviewRecord(
                review_id=f"review-{uuid.uuid4().hex}",
                document_id=f"document-{uuid.uuid4().hex}",
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                business_task_id=request.business_task_id,
                contract_version_id=request.contract_version_id,
                model_pack_id=request.model_pack_id or "",
                idempotency_key=context.idempotency_key,
                request_fingerprint=fingerprint,
                file_sha256=file_sha256,
                status=ReviewStatus.CREATED,
                created_at=now,
                updated_at=now,
            )
            self._records[record.review_id] = record
            self._business_keys[business_key] = record.review_id
            self._idempotency_keys[idempotency_key] = record.review_id
            return self._create_data(record, reused=False)

    def get_status(self, review_id: str, *, context: InternalRequestContext) -> ReviewStatusData:
        with self._lock:
            record = self._get_owned_record(review_id, context)
            return ReviewStatusData(
                review_id=record.review_id,
                business_task_id=record.business_task_id,
                contract_version_id=record.contract_version_id,
                model_pack_id=record.model_pack_id,
                status=record.status,
                current_stage=None,
                document_id=record.document_id,
                framework_task_id=None,
                framework_run_id=None,
                framework_attempt_no=None,
                error=None,
                updated_at=record.updated_at,
            )

    def get_result(self, review_id: str, *, context: InternalRequestContext) -> ReviewResultData:
        with self._lock:
            record = self._get_owned_record(review_id, context)
            if record.status != ReviewStatus.SUCCEEDED:
                raise ContractError(
                    "REVIEW_NOT_READY",
                    "合同审查结果尚未就绪",
                    status_code=409,
                    retryable=record.status in {ReviewStatus.CREATED, ReviewStatus.RUNNING},
                    details={"current_status": record.status.value},
                )
        raise ContractError(
            "RESULT_INVALID",
            "Mock adapter does not fabricate successful contract review results",
            status_code=500,
        )

    def cancel_review(self, review_id: str, *, context: InternalRequestContext) -> CancelReviewData:
        with self._lock:
            record = self._get_owned_record(review_id, context)
            if record.status == ReviewStatus.CANCELLED:
                return CancelReviewData(
                    review_id=record.review_id,
                    status=ReviewStatus.CANCELLED,
                    already_terminal=True,
                )
            if record.status in {ReviewStatus.SUCCEEDED, ReviewStatus.FAILED}:
                raise ContractError(
                    "REVIEW_ALREADY_TERMINAL",
                    "审查任务已经结束，不能取消",
                    status_code=409,
                    details={"current_status": record.status.value},
                )
            updated = replace(record, status=ReviewStatus.CANCELLED, updated_at=datetime.now(timezone.utc))
            self._records[review_id] = updated
            return CancelReviewData(
                review_id=updated.review_id,
                status=ReviewStatus.CANCELLED,
                already_terminal=False,
            )

    def _get_owned_record(
        self,
        review_id: str,
        context: InternalRequestContext,
    ) -> MockReviewRecord:
        record = self._records.get(review_id)
        if record is None:
            raise ContractError("REVIEW_NOT_FOUND", "合同审查任务不存在", status_code=404)
        if record.tenant_id != context.tenant_id or record.user_id != context.user_id:
            raise ContractError("ACCESS_DENIED", "无权访问该合同审查任务", status_code=403)
        return record

    @staticmethod
    def _create_data(record: MockReviewRecord, *, reused: bool) -> CreateReviewData:
        return CreateReviewData(
            review_id=record.review_id,
            document_id=record.document_id,
            model_pack_id=record.model_pack_id,
            status=ReviewStatus.CREATED,
            current_stage=None,
            framework_task_id=None,
            framework_run_id=None,
            framework_attempt_no=None,
            reused=reused,
        )

    @staticmethod
    def _party_resolution_create_data(
        record: MockPartyResolutionRecord,
        *,
        reused: bool,
    ) -> PartyResolutionCreateData:
        return PartyResolutionCreateData(
            resolution_id=record.resolution_id,
            contract_version_id=record.contract_version_id,
            document_id=record.document_id,
            model_pack_id=record.model_pack_id,
            status=ReviewStatus.CREATED,
            current_stage=None,
            framework_task_id=None,
            framework_run_id=None,
            framework_attempt_no=None,
            reused=reused,
        )

    @staticmethod
    def _idempotency_conflict(review_id: str | None = None) -> ContractError:
        details = {"review_id": review_id} if review_id else None
        return ContractError(
            "IDEMPOTENCY_CONFLICT",
            "幂等键已被不同的合同审查请求使用",
            status_code=409,
            user_action_required=True,
            details=details,
        )
