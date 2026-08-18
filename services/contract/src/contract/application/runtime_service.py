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
    PartyResolutionCreateData,
    PartyResolutionCreateRequest,
    PartyResolutionStatusData,
    ReviewResultData,
    PublicReviewResultData,
    ReviewStage,
    PartyResolutionData,
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
from contract.application.idempotency import (
    build_party_resolution_fingerprint,
    build_request_fingerprint,
    sha256_bytes,
)
from contract.application.ports import InternalRequestContext, UploadedContract
from contract.config import Settings
from contract.errors import ContractError
from contract.callback.models import PartyResolutionStageResult
from contract.model_pack import resolve_model_pack_id
from contract.persistence.models import ReviewCreate
from contract.persistence.postgres.callback_repository import FrameworkCallbackRepository
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
        completion_repository: FrameworkCallbackRepository | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.state_repository = state_repository
        self.document_processor = document_processor
        self.framework_gateway = framework_gateway
        self.completion_repository = completion_repository or FrameworkCallbackRepository(settings)

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

    def create_party_resolution(
        self,
        *,
        upload: UploadedContract,
        request: PartyResolutionCreateRequest,
        context: InternalRequestContext,
    ) -> PartyResolutionCreateData:
        idempotency_key = context.idempotency_key
        if not idempotency_key:
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
        fingerprint = build_party_resolution_fingerprint(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            request=request,
            file_sha256=file_sha256,
        )
        business_task_id = self._id("party-resolution")
        existing = self.state_repository.find_for_request(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            business_task_id=business_task_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            execution_mode="PARTY_RESOLUTION",
        )
        reused = existing is not None
        if existing is None:
            resolution_id = self._id("resolution")
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
                    review_id=resolution_id,
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    business_task_id=business_task_id,
                    contract_version_id=request.contract_version_id,
                    model_pack_id=request.model_pack_id or "",
                    document_id=processed.document_id,
                    idempotency_key=idempotency_key,
                    request_id=context.request_id,
                    request_fingerprint=fingerprint,
                    file_sha256=file_sha256,
                    # This is an internal pipeline placeholder only. The preflight API never
                    # accepts or returns a party perspective before the user confirms one.
                    perspective="PARTY_A",
                    our_party_name=None,
                    contract_type="AUTO",
                    review_attitude="NEUTRAL",
                    schema_version=request.schema_version,
                    execution_mode="PARTY_RESOLUTION",
                )
            )
            resolution_id = review["id"]
        else:
            resolution_id = existing["id"]

        self.state_repository.ensure_initial_attempt(
            resolution_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        state = self.state_repository.get_state(
            resolution_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        if state["status"] not in {"CREATED", "RUNNING"}:
            raise ContractError(
                "PARTY_RESOLUTION_TERMINAL",
                "The existing party resolution is terminal; create a new resolution request to retry",
                status_code=409,
                user_action_required=True,
                details={"current_status": state["status"], "resolution_id": resolution_id},
            )
        return self._party_resolution_create_data(state, reused=reused)

    def get_party_resolution(
        self,
        resolution_id: str,
        *,
        context: InternalRequestContext,
    ) -> PartyResolutionStatusData:
        state = self.state_repository.get_state(
            resolution_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        self._require_execution_mode(state, "PARTY_RESOLUTION")
        stage_result = self._party_resolution_stage_result(state)
        return self._party_resolution_status_data(state, stage_result)

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
        request = request.model_copy(
            update={"model_pack_id": resolve_model_pack_id(request.model_pack_id)}
        )
        if request.confirmed_party_a_name is not None:
            expected_our_party = (
                request.confirmed_party_a_name
                if request.perspective.value == "PARTY_A"
                else request.confirmed_party_b_name
            )
            request = request.model_copy(update={"our_party_name": expected_our_party})
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
                    party_resolution_id=request.party_resolution_id,
                    model_pack_id=request.model_pack_id or "",
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
                    confirmed_party_a_name=normalized["confirmed_party_a_name"],
                    confirmed_party_b_name=normalized["confirmed_party_b_name"],
                )
            )
            review_id = review["id"]
        else:
            review_id = existing["id"]

        self.state_repository.ensure_initial_attempt(
            review_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
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
        self._require_execution_mode(state, "FULL_REVIEW")
        return self._status_data(state, self._party_resolution(state))

    def get_result(self, review_id: str, *, context: InternalRequestContext) -> PublicReviewResultData:
        status = self.get_status(review_id, context=context)
        if status.status != ReviewStatus.SUCCEEDED:
            raise ContractError(
                "REVIEW_NOT_READY",
                "合同审查结果尚未就绪",
                status_code=409,
                retryable=status.status in {ReviewStatus.CREATED, ReviewStatus.RUNNING},
                details={"current_status": status.status.value},
            )
        value = self.state_repository.get_result_json(
            review_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        if value is None:
            raise ContractError("RESULT_INVALID", "合同审查结果记录不存在", status_code=500)
        try:
            internal = ReviewResultData.model_validate(value)
            return PublicReviewResultData.from_internal(internal)
        except ValidationError as exc:
            raise ContractError("RESULT_INVALID", "合同审查结果不符合冻结协议", status_code=500) from exc

    def reconcile_nonterminal_reviews(self) -> int:
        """Repair only durable Review/Attempt state; never call Framework."""
        return self.state_repository.reconcile_nonterminal_attempts()

    def dispatch_pending_attempts(self) -> int:
        """Claim and dispatch durable Attempts without depending on a user query."""
        self.state_repository.reconcile_expired_dispatch_leases()
        self.state_repository.reconcile_nonterminal_attempts()
        owner = getattr(self, "_dispatcher_owner", None)
        if owner is None:
            owner = f"contract-dispatcher-{uuid.uuid4().hex}"
            self._dispatcher_owner = owner
        reservations = self.state_repository.claim_pending_attempts(
            owner=owner,
            limit=getattr(self.settings, "dispatcher_batch_size", 8),
            lease_seconds=getattr(self.settings, "dispatch_lease_seconds", 120),
        )
        dispatched = 0
        for reservation in reservations:
            if self.dispatch_claimed_attempt(reservation):
                dispatched += 1
        return dispatched

    def reconcile_active_reviews(self) -> int:
        """Poll active Framework Runs from the reconciler, not from status queries."""
        reconciled = 0
        for item in self.state_repository.list_active_attempts():
            context = InternalRequestContext(
                tenant_id=item["tenant_id"],
                user_id=item["user_id"],
                request_id=f"framework-reconcile-{item['id']}",
                idempotency_key=None,
            )
            try:
                state = self.state_repository.get_state(
                    item["id"],
                    tenant_id=item["tenant_id"],
                    user_id=item["user_id"],
                )
                self._reconcile_running(state, context=context)
                reconciled += 1
            except ContractError:
                logger.exception("Unable to reconcile contract review %s", item["id"])
        return reconciled

    def cancel_review(self, review_id: str, *, context: InternalRequestContext) -> CancelReviewData:
        state = self.state_repository.get_state(
            review_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        self._require_execution_mode(state, "FULL_REVIEW")
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
                snapshots.append(
                    self.framework_gateway.cancel_run(
                        mapping.task_id,
                        mapping.run_id,
                        tenant_id=context.tenant_id,
                        user_id=context.user_id,
                    )
                )
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
                tenant_id=context.tenant_id,
                user_id=context.user_id,
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
            if self.completion_repository.finish_if_ready(
                state["id"],
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                attempt_no=attempt["attempt_no"],
            ):
                return self.state_repository.get_state(
                    state["id"],
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                )
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
        return self.state_repository.get_state(
            state["id"], tenant_id=context.tenant_id, user_id=context.user_id
        )

    def dispatch_claimed_attempt(self, reservation: AttemptReservation) -> bool:
        if reservation.previous_task_id and reservation.previous_run_id:
            try:
                self.framework_gateway.cancel_run(
                    reservation.previous_task_id,
                    reservation.previous_run_id,
                    tenant_id=reservation.tenant_id,
                    user_id=reservation.user_id,
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
            party_resolution_id=reservation.party_resolution_id,
            model_pack_id=reservation.model_pack_id,
            document_id=reservation.document_id,
            perspective=reservation.perspective,
            our_party_name=reservation.our_party_name,
            contract_type=reservation.contract_type,
            review_attitude=reservation.review_attitude,
            schema_version=reservation.schema_version,
            execution_mode=reservation.execution_mode,
            confirmed_party_a_name=reservation.confirmed_party_a_name,
            confirmed_party_b_name=reservation.confirmed_party_b_name,
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
                self.state_repository.mark_dispatch_retry(
                    reservation,
                    error_code=exc.code,
                    error_message=str(exc),
                    retryable=True,
                    retry_delay_seconds=min(
                        60,
                        max(1, 2 ** min(reservation.lease_version, 6)),
                    ),
                )
                return False
            self.state_repository.mark_dispatch_retry(
                reservation,
                error_code=exc.code,
                error_message=str(exc),
                retryable=False,
                retry_delay_seconds=0,
            )
            logger.error(
                "Framework dispatch permanently failed for review %s attempt %s: %s",
                reservation.review_id,
                reservation.attempt_no,
                exc,
            )
            return False
        dispatched = self.state_repository.mark_dispatch_sent(reservation, snapshot)
        if not dispatched and self.state_repository.should_cancel_unaccepted_dispatch(
            reservation, snapshot
        ):
            try:
                self.framework_gateway.cancel_run(
                    snapshot.task_id,
                    snapshot.run_id,
                    tenant_id=reservation.tenant_id,
                    user_id=reservation.user_id,
                )
            except FrameworkGatewayError:
                logger.warning("Unable to cancel superseded Framework Run %s", snapshot.run_id)
        return dispatched

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
                model_pack_id=state["model_pack_id"],
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
            model_pack_id=state["model_pack_id"],
            status=ReviewStatus.CREATED,
            current_stage=None,
            framework_attempt_no=None,
            framework_task_id=None,
            framework_run_id=None,
            reused=reused,
        )

    def _party_resolution_stage_result(self, state: dict) -> PartyResolutionStageResult | None:
        attempt = state["active_attempt"]
        if attempt is None:
            return None
        value = self.completion_repository.get_validated_stage_result(
            state["id"],
            attempt["attempt_no"],
            "resolve_parties",
        )
        if value is None:
            return None
        try:
            result = PartyResolutionStageResult.model_validate(value)
        except ValidationError:
            logger.error(
                "Validated party resolution result has an invalid shape for review %s attempt %s",
                state["id"],
                attempt["attempt_no"],
            )
            return None
        return result

    def _party_resolution(self, state: dict) -> PartyResolutionData | None:
        result = self._party_resolution_stage_result(state)
        if result is None or result.resolution_status != "RESOLVED":
            return None
        assert result.party_a is not None and result.party_b is not None
        assert result.our_party is not None and result.counterparty is not None
        return PartyResolutionData(
            party_a={"name": result.party_a.name},
            party_b={"name": result.party_b.name},
            perspective=result.perspective,
            our_party=result.our_party,
            counterparty=result.counterparty,
        )

    @staticmethod
    def _party_resolution_create_data(
        state: dict,
        *,
        reused: bool,
    ) -> PartyResolutionCreateData:
        attempt = state["active_attempt"]
        mapped = (
            attempt is not None
            and attempt["framework_task_id"] is not None
            and attempt["framework_run_id"] is not None
        )
        if mapped:
            return PartyResolutionCreateData(
                resolution_id=state["id"],
                contract_version_id=state["contract_version_id"],
                document_id=state["document_id"],
                model_pack_id=state["model_pack_id"],
                status=ReviewStatus.RUNNING,
                current_stage=state["current_stage"] or ReviewStage.PARSING,
                framework_attempt_no=attempt["attempt_no"],
                framework_task_id=attempt["framework_task_id"],
                framework_run_id=attempt["framework_run_id"],
                reused=reused,
            )
        return PartyResolutionCreateData(
            resolution_id=state["id"],
            contract_version_id=state["contract_version_id"],
            document_id=state["document_id"],
            model_pack_id=state["model_pack_id"],
            status=ReviewStatus.CREATED,
            current_stage=None,
            framework_attempt_no=None,
            framework_task_id=None,
            framework_run_id=None,
            reused=reused,
        )

    @staticmethod
    def _status_data(
        state: dict,
        party_resolution: PartyResolutionData | None,
    ) -> ReviewStatusData:
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
            model_pack_id=state["model_pack_id"],
            status=state["status"],
            current_stage=state["current_stage"],
            document_id=state["document_id"],
            framework_attempt_no=attempt["attempt_no"] if attempt else None,
            framework_task_id=attempt["framework_task_id"] if attempt else None,
            framework_run_id=attempt["framework_run_id"] if attempt else None,
            error=error,
            updated_at=state["updated_at"],
            party_resolution=party_resolution,
        )

    @staticmethod
    def _party_resolution_status_data(
        state: dict,
        party_resolution: PartyResolutionStageResult | None,
    ) -> PartyResolutionStatusData:
        attempt = state["active_attempt"]
        error = None
        if state["status"] == "FAILED":
            error = ErrorData(
                code=state["error_code"],
                message=state["error_message"] or "Contract party resolution failed",
                retryable=state["retryable"],
                user_action_required=state["user_action_required"],
                details=state["error_details_json"],
            )
        return PartyResolutionStatusData(
            resolution_id=state["id"],
            contract_version_id=state["contract_version_id"],
            model_pack_id=state["model_pack_id"],
            status=state["status"],
            current_stage=state["current_stage"],
            document_id=state["document_id"],
            framework_attempt_no=attempt["attempt_no"] if attempt else None,
            framework_task_id=attempt["framework_task_id"] if attempt else None,
            framework_run_id=attempt["framework_run_id"] if attempt else None,
            error=error,
            party_a_name=(
                party_resolution.party_a.name if party_resolution and party_resolution.party_a else None
            ),
            party_b_name=(
                party_resolution.party_b.name if party_resolution and party_resolution.party_b else None
            ),
            updated_at=state["updated_at"],
        )

    @staticmethod
    def _require_execution_mode(state: dict, expected: str) -> None:
        # Rows written before the preflight migration, and lightweight legacy
        # adapters used by callers, are formal reviews by definition.
        if state.get("execution_mode", "FULL_REVIEW") != expected:
            raise ContractError(
                "REVIEW_NOT_FOUND",
                "Contract review resource does not exist or is not accessible",
                status_code=404,
            )

    @staticmethod
    def _gateway_error(exc: FrameworkGatewayError) -> ContractError:
        return ContractError(
            exc.code,
            str(exc),
            status_code=exc.status_code,
            retryable=exc.retryable,
        )

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"


def build_runtime_contract_review_service(settings: Settings) -> RuntimeContractReviewService:
    from contract.application.framework_http_gateway import FrameworkHttpGateway

    repository = ContractRepository(settings)
    return RuntimeContractReviewService(
        settings,
        repository,
        ReviewStateRepository(settings),
        ContractDocumentProcessor(settings, repository),
        FrameworkHttpGateway(settings),
    )
