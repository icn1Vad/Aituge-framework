from __future__ import annotations

from contract.callback.models import (
    FinalizeReviewStageResult,
    FrameworkCallback,
    FrameworkCallbackData,
    StageResultCallback,
)
from contract.errors import ContractError
from contract.internal.service import ContractInternalService
from contract.persistence.postgres.callback_repository import FrameworkCallbackRepository


class FrameworkCallbackService:
    def __init__(
        self,
        repository: FrameworkCallbackRepository,
        internal_service: ContractInternalService,
    ) -> None:
        self.repository = repository
        self.internal_service = internal_service

    def accept(
        self,
        path_review_id: str,
        callback: FrameworkCallback,
    ) -> FrameworkCallbackData:
        if callback.review_id != path_review_id:
            raise ContractError(
                "FRAMEWORK_CALLBACK_MISMATCH",
                "Callback review_id does not match the request path",
                status_code=409,
            )
        if isinstance(callback, StageResultCallback) and isinstance(
            callback.result,
            FinalizeReviewStageResult,
        ):
            self.internal_service.validate_final_result(callback.result)
        outcome = self.repository.process(callback)
        return FrameworkCallbackData(
            accepted=outcome.accepted,
            duplicate=outcome.duplicate,
            ignored_reason=outcome.ignored_reason,
        )
