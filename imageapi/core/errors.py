"""Error codes and exceptions for ImageAPI."""

from enum import Enum
from typing import Any, Optional


class ErrorCode(str, Enum):
    VALIDATION_PROMPT_REQUIRED = "VALIDATION_PROMPT_REQUIRED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    SERVICE_NOT_INITIALIZED = "SERVICE_NOT_INITIALIZED"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    GENERATION_FAILED = "GENERATION_FAILED"
    GENERATION_FILTERED = "GENERATION_FILTERED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    NO_ACCOUNTS_AVAILABLE = "NO_ACCOUNTS_AVAILABLE"
    QUEUE_FULL = "QUEUE_FULL"
    SERVICE_TEMPORARILY_UNAVAILABLE = "SERVICE_TEMPORARILY_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


GOOGLE_ERROR_MAPPING = {
    "UNAUTHORIZED": ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
    "FORBIDDEN": ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
    "RATE_LIMITED": ErrorCode.RATE_LIMIT_EXCEEDED,
    "TOKEN_EXPIRED": ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
    "CAPTCHA_FAILED": ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
    "FILTERED": ErrorCode.GENERATION_FILTERED,
    "GENERATION_FAILED": ErrorCode.GENERATION_FAILED,
}


class ApiError(Exception):
    """Structured API error."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status_code: int = 400,
        details: Optional[str] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        error_data: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
        }
        if self.details:
            error_data["details"] = self.details
        return {"success": False, "error": error_data}


def map_google_error(error_code: Optional[str], message: str) -> ApiError:
    """Map ImageClientError to ApiError."""
    mapped = GOOGLE_ERROR_MAPPING.get(error_code or "", ErrorCode.GENERATION_FAILED)

    if mapped == ErrorCode.RATE_LIMIT_EXCEEDED:
        status = 429
    elif mapped == ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE:
        status = 503
    elif mapped == ErrorCode.GENERATION_FILTERED:
        status = 451
    else:
        status = 500

    return ApiError(code=mapped, message=message, status_code=status)
