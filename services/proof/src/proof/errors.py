from __future__ import annotations

from typing import Any


class ProofError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}


class ConfigurationError(ProofError):
    def __init__(self, message: str) -> None:
        super().__init__("configuration_error", message, status_code=503)
