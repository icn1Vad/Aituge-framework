from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from contract.api.models import (
    CancelReviewData,
    CreateReviewData,
    CreateReviewRequest,
    ErrorData,
    ReviewResultData,
    ReviewStage,
    ReviewStatus,
    ReviewStatusData,
)
from contract.application.document_processing import ContractDocumentProcessor
from contract.application.framework_gateway import (
    FrameworkExecutionRequest,
    FrameworkGateway,
    FrameworkGatewayError,
    FrameworkProtocolError,
    FrameworkRunSnapshot,
)
from contract.application.idempotency import build_request_fingerprint, sha256_bytes
from contract.application.ports import InternalRequestContext, UploadedContract
from contract.config import Settings
from contract.errors import ContractError
from contract.persistence.models import ReviewCreate
from contract.persistence.postgres.repository import ContractRepository
from contract.persistence.postgres.review_state import AttemptReservation, ReviewStateRepository


logger = logging.getLogger(__name__)


class RuntimeContractReviewService:
    """Database-backed review coordinator; Framework transport is supplied by an adapter."""

    def __init__(
        self,
        settings: Settings,
        repository: ContractRepository,
        state_repository: ReviewStateRepository,
        document_processor: ContractDocumentProcessor,
        framework_gateway: FrameworkGateway,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.state_repository = state_repository
        self.document_processor = document_processor
        self.framework_gateway = framework_gateway

    def health(self) -> dict[str, str]:
        health = self.repository.health()
        if not health["ok"]:
            raise ContractError(
                "INTERNAL_ERROR",
                "合同审查数据库不可用",
                status_code=503,
                retryable=True,
            )
        return {"status": "UP", "service": "contract", "schema_version": "1.0", "mode": "runtime"}

    def create_review(
        self,
        *,
        upload: UploadedContract,
        request: CreateReviewRequest,
        context: InternalRequestContext,
    ) -> CreateReviewData:
        idempotency_key = context.idempotency_key
        if not idempotency_key:
            raise ContractError(
                "INVALID_REQUEST",
                "Idempotency-Key is required",
                status_code=400,
                user_action_required=True,
            )
        file_sha256 = sha256_bytes(upload.content)
        fingerprint, normalized = build_request_fingerprint(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            request=request,
            file_sha256=file_sha256,
        )
        existing = self.state_repository.find_for_request(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            business_task_id=request.business_task_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        reused = existing is not None
        if existing is None:
            review_id = self._id("review")
            processed = self.document_processor.process(
                upload=upload,
                document_id=self._id("document"),
                generation_id=self._id("generation"),
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                contract_version_id=request.contract_version_id,
            )
            review, reused = self.repository.create_review(
                ReviewCreate(
                    review_id=review_id,
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    business_task_id=request.business_task_id,
                    contract_version_id=request.contract_version_id,
                    document_id=processed.document_id,
                    idempotency_key=idempotency_key,
                    request_id=context.request_id,
                    request_fingerprint=fingerprint,
                    file_sha256=file_sha256,
                    perspective=request.perspective.value,
                    our_party_name=normalized["our_party_name"],
                    contract_type=request.contract_type,
                    review_attitude=request.review_attitude,
                    schema_version=request.schema_version,
                )
            )
            review_id = review["id"]
        else:
            review_id = existing["id"]

        reservation = self.state_repository.reserve_initial_attempt(
            review_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        if reservation is not None:
            self._dispatch(reservation)
        state = self.state_repository.get_state(
            review_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        return self._create_data(state, reused=reused)

    def get_status(self, review_id: str, *, context: InternalRequestContext) -> ReviewStatusData:
        state = self.state_repository.get_state(
            review_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        pending = state["pending_attempt"]
        if pending is not None and state["status"] in {"CREATED", "RUNNING"} and not state["cancel_requested"]:
            reservation = self.state_repository.reserve_initial_attempt(
                review_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
            )
            if reservation is not None:
                self._dispatch(reservation)
                state = self.state_repository.get_state(
                    review_id,
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                )

        if state["pending_attempt"] is not None:
            return self._status_data(state)

        attempt = state["active_attempt"]
        if state["status"] == "RUNNING" and attempt is not None and attempt["framework_run_id"]:
            state = self._reconcile_running(state, context=context)
        return self._status_data(state)

    def get_result(self, review_id: str, *, context: InternalRequestContext) -> ReviewResultData:
        state = self.state_repository.get_state(
            review_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        if state["status"] != "SUCCEEDED":
            raise ContractError(
                "REVIEW_NOT_READY",
                "合同审查结果尚未就绪",
                status_code=409,
                retryable=state["status"] in {"CREATED", "RUNNING"},
                details={"current_status": state["status"]},
            )
        value = self.state_repository.get_result_json(
            review_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        if value is None:
            raise ContractError("RESULT_INVALID", "合同审查结果记录不存在", status_code=500)
        try:
            return ReviewResultData.model_validate(value)
        except ValidationError as exc:
            raise ContractError("RESULT_INVALID", "合同审查结果不符合冻结协议", status_code=500) from exc

    def cancel_review(self, review_id: str, *, context: InternalRequestContext) -> CancelReviewData:
        plan = self.state_repository.begin_cancel(
            review_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        if plan.already_cancelled:
            return CancelReviewData(
                review_id=review_id,
                status=ReviewStatus.CANCELLED,
                already_terminal=True,
            )
        snapshots: list[FrameworkRunSnapshot] = []
        first_error: FrameworkGatewayError | None = None
        for mapping in plan.mappings:
            try:
                snapshots.append(self.framework_gateway.cancel_run(mapping.task_id, mapping.run_id))
            except FrameworkGatewayError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise self._gateway_error(first_error) from first_error
        finished = self.state_repository.finish_cancel(
            plan,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            snapshots=tuple(snapshots),
        )
        if not finished:
            state = self.state_repository.get_state(
                review_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
            )
            if state["status"] in {"SUCCEEDED", "FAILED"}:
                raise ContractError(
                    "REVIEW_ALREADY_TERMINAL",
                    "审查任务已经结束，不能取消",
                    status_code=409,
                    details={"current_status": state["status"]},
                )
            raise ContractError(
                "FRAMEWORK_UNAVAILABLE",
                "Framework尚未确认Run取消完成",
                status_code=503,
                retryable=True,
            )
        return CancelReviewData(
            review_id=review_id,
            status=ReviewStatus.CANCELLED,
            already_terminal=False,
        )

    def _reconcile_running(
        self,
        state: dict,
        *,
        context: InternalRequestContext,
    ) -> dict:
        attempt = state["active_attempt"]
        try:
            snapshot = self.framework_gateway.get_run(
                attempt["framework_task_id"],
                attempt["framework_run_id"],
            )
        except FrameworkGatewayError:
            return state
        if snapshot.status in {"failed", "cancelled"}:
            self.state_repository.apply_framework_terminal(
                review_id=state["id"],
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                attempt_no=attempt["attempt_no"],
                snapshot=snapshot,
            )
            return self.state_repository.get_state(
                state["id"], tenant_id=context.tenant_id, user_id=context.user_id
            )
        if snapshot.status == "succeeded":
            return state
        self.state_repository.record_run_snapshot(
            review_id=state["id"],
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            attempt_no=attempt["attempt_no"],
            snapshot=snapshot,
        )
        stale_before = datetime.now(timezone.utc) - timedelta(
            seconds=(
                self.settings.framework_stage_timeout_seconds
                + self.settings.framework_recovery_grace_seconds
            )
        )
        reservation = self.state_repository.prepare_recovery(
            review_id=state["id"],
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            attempt_no=attempt["attempt_no"],
            stale_before=stale_before,
        )
        if reservation is not None:
            self._dispatch(reservation)
        return self.state_repository.get_state(
            state["id"], tenant_id=context.tenant_id, user_id=context.user_id
        )

    def _dispatch(self, reservation: AttemptReservation) -> bool:
        if reservation.previous_task_id and reservation.previous_run_id:
            try:
                self.framework_gateway.cancel_run(
                    reservation.previous_task_id,
                    reservation.previous_run_id,
                )
            except FrameworkGatewayError:
                logger.warning(
                    "Unable to cancel orphaned Framework Run %s",
                    reservation.previous_run_id,
                )
        request = FrameworkExecutionRequest(
            review_id=reservation.review_id,
            attempt_no=reservation.attempt_no,
            tenant_id=reservation.tenant_id,
            user_id=reservation.user_id,
            business_task_id=reservation.business_task_id,
            contract_version_id=reservation.contract_version_id,
            document_id=reservation.document_id,
            perspective=reservation.perspective,
            our_party_name=reservation.our_party_name,
            contract_type=reservation.contract_type,
            review_attitude=reservation.review_attitude,
            schema_version=reservation.schema_version,
        )
        try:
            snapshot = self.framework_gateway.create_execution(request)
            if snapshot.terminal:
                raise FrameworkProtocolError("Framework returned a terminal Run while creating an execution")
        except FrameworkGatewayError as exc:
            if exc.retryable:
                logger.warning(
                    "Framework dispatch is pending for review %s attempt %s: %s",
                    reservation.review_id,
                    reservation.attempt_no,
                    exc,
                )
                return False
            self.state_repository.mark_dispatch_failed(
                reservation,
                error_code="FRAMEWORK_PROTOCOL_ERROR",
                error_message=str(exc),
                retryable=False,
            )
            raise self._gateway_error(exc) from exc
        activated = self.state_repository.activate_attempt(reservation, snapshot)
        if not activated:
            try:
                self.framework_gateway.cancel_run(snapshot.task_id, snapshot.run_id)
            except FrameworkGatewayError:
                logger.warning("Unable to cancel superseded Framework Run %s", snapshot.run_id)
        return activated

    @staticmethod
    def _create_data(state: dict, *, reused: bool) -> CreateReviewData:
        attempt = state["active_attempt"]
        mapped = (
            attempt is not None
            and attempt["framework_task_id"] is not None
            and attempt["framework_run_id"] is not None
        )
        if mapped:
            return CreateReviewData(
                review_id=state["id"],
                document_id=state["document_id"],
                status=ReviewStatus.RUNNING,
                current_stage=state["current_stage"] or ReviewStage.PARSING,
                framework_attempt_no=attempt["attempt_no"],
                framework_task_id=attempt["framework_task_id"],
                framework_run_id=attempt["framework_run_id"],
                reused=reused,
            )
        return CreateReviewData(
            review_id=state["id"],
            document_id=state["document_id"],
            status=ReviewStatus.CREATED,
            current_stage=None,
            framework_attempt_no=None,
            framework_task_id=None,
            framework_run_id=None,
            reused=reused,
        )

    @staticmethod
    def _status_data(state: dict) -> ReviewStatusData:
        attempt = state["active_attempt"]
        error = None
        if state["status"] == "FAILED":
            error = ErrorData(
                code=state["error_code"],
                message=state["error_message"] or "合同审查执行失败",
                retryable=state["retryable"],
                user_action_required=state["user_action_required"],
                details=state["error_details_json"],
            )
        return ReviewStatusData(
            review_id=state["id"],
            business_task_id=state["business_task_id"],
            contract_version_id=state["contract_version_id"],
            status=state["status"],
            current_stage=state["current_stage"],
            document_id=state["document_id"],
            framework_attempt_no=attempt["attempt_no"] if attempt else None,
            framework_task_id=attempt["framework_task_id"] if attempt else None,
            framework_run_id=attempt["framework_run_id"] if attempt else None,
            error=error,
            updated_at=state["updated_at"],
        )

    @staticmethod
    def _gateway_error(exc: FrameworkGatewayError) -> ContractError:
        if exc.retryable:
            return ContractError(
                "FRAMEWORK_UNAVAILABLE",
                str(exc),
                status_code=503,
                retryable=True,
            )
        return ContractError(
            "FRAMEWORK_PROTOCOL_ERROR",
            str(exc),
            status_code=502,
            retryable=False,
        )

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"
