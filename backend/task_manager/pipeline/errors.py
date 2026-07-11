from __future__ import annotations


class PipelineCancelled(Exception):
    pass


class PipelinePaused(Exception):
    def __init__(self, message: str, *, stage_id: str, payload: dict | None = None) -> None:
        super().__init__(message)
        self.stage_id = stage_id
        self.payload = payload or {}


class StageExecutionError(Exception):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
