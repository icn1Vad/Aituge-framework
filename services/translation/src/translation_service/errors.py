from __future__ import annotations


class TranslationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


class ModelCallError(TranslationError):
    def __init__(self, message: str = "Translation model call failed") -> None:
        super().__init__(
            "MODEL_CALL_FAILED",
            message,
            status_code=502,
            retryable=True,
        )
