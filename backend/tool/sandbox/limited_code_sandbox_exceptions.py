"""Exceptions for limited code sandbox execution."""


class LimitedCodeSandboxException(Exception):
    """Base exception for limited code sandbox errors."""


class LimitedCodeSandboxEmptyCodeException(LimitedCodeSandboxException):
    """Raised when code is empty or invalid."""


class LimitedCodeSandboxNotConfiguredException(LimitedCodeSandboxException):
    """Raised when the sandbox is disabled or missing required config."""


class LimitedCodeSandboxNotInitializedException(LimitedCodeSandboxException):
    """Raised when the sandbox or execution context is not initialized."""


class LimitedCodeSandboxTimeoutException(LimitedCodeSandboxException):
    """Raised when sandbox startup or execution times out."""


class LimitedCodeSandboxHTTPException(LimitedCodeSandboxException):
    """Raised when a sandbox HTTP request fails."""


class LimitedCodeSandboxAPIException(LimitedCodeSandboxException):
    """Raised when the sandbox API returns an invalid or failed response."""


class LimitedCodeSandboxExecutionException(LimitedCodeSandboxException):
    """Raised during code or command execution."""
