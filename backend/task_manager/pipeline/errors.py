from __future__ import annotations

from typing import Any


class PipelineCancelled(Exception):
    pass


class PipelinePaused(Exception):
    def __init__(self, message: str, *, stage_id: str, payload: dict | None = None) -> None:
        super().__init__(message)
        self.stage_id = stage_id
        self.payload = payload or {}


class StageExecutionError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        domain_error_code: str | None = None,
        domain_retryable: bool = False,
        user_action_required: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.domain_error_code = domain_error_code
        self.domain_retryable = domain_retryable
        self.user_action_required = user_action_required
        self.details = details
