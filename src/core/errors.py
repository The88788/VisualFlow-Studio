"""Error codes and custom exceptions for VeoAPI."""

from enum import Enum
from typing import Optional


class ErrorCode(str, Enum):
    """API error codes matching internal service specification."""

    # Validation errors
    VALIDATION_PROMPT_REQUIRED = "VALIDATION_PROMPT_REQUIRED"
    VALIDATION_VIDEO_IDS_REQUIRED = "VALIDATION_VIDEO_IDS_REQUIRED"
    VALIDATION_TOO_MANY_VIDEOS = "VALIDATION_TOO_MANY_VIDEOS"
    VALIDATION_VIDEO_NOT_READY = "VALIDATION_VIDEO_NOT_READY"
    VALIDATION_IMAGE_NOT_READY = "VALIDATION_IMAGE_NOT_READY"
    VALIDATION_INVALID_PARAMS = "VALIDATION_INVALID_PARAMS"

    # Service errors
    SERVICE_NOT_INITIALIZED = "SERVICE_NOT_INITIALIZED"
    SERVICE_TEMPORARILY_UNAVAILABLE = "SERVICE_TEMPORARILY_UNAVAILABLE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    MAX_CONCURRENT_REQUESTS = "MAX_CONCURRENT_REQUESTS"

    # Resource errors
    VIDEO_NOT_FOUND = "VIDEO_NOT_FOUND"
    VIDEO_FILE_NOT_FOUND = "VIDEO_FILE_NOT_FOUND"
    IMAGE_NOT_FOUND = "IMAGE_NOT_FOUND"
    IMAGE_FILE_NOT_FOUND = "IMAGE_FILE_NOT_FOUND"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"

    # Generation errors
    GENERATION_FAILED = "GENERATION_FAILED"
    VIDEO_TRIM_FAILED = "VIDEO_TRIM_FAILED"
    VIDEO_MERGE_FAILED = "VIDEO_MERGE_FAILED"

    # Rate limiting
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"

    # Internal errors
    INTERNAL_ERROR = "INTERNAL_ERROR"


# Mapping from VeoClient internal codes to API error codes
VEO_ERROR_MAPPING = {
    "UNAUTHORIZED": ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
    "FORBIDDEN": ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
    "RATE_LIMITED": ErrorCode.RATE_LIMIT_EXCEEDED,
    # Content / safety rejections — synthesized when Flow's DOM shows the
    # generic "Oops, something went wrong" failure card without a specific
    # sniffer hit. Maps to a 400-level validation error so the client
    # knows the prompt itself is the issue (not infra), and retrying with
    # the same input won't help.
    "PUBLIC_ERROR_SAFETY_FILTER": ErrorCode.VALIDATION_INVALID_PARAMS,
    "CAPTCHA_FAILED": ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
    "TOKEN_EXCHANGE_ERROR": ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
    "TOKEN_EXPIRED": ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
    "UPLOAD_ERROR": ErrorCode.GENERATION_FAILED,
    "GENERATION_ERROR": ErrorCode.GENERATION_FAILED,
    "GENERATION_FAILED": ErrorCode.GENERATION_FAILED,
    "INVALID_ARGUMENT": ErrorCode.VALIDATION_INVALID_PARAMS,
    "MAX_RETRIES_EXCEEDED": ErrorCode.GENERATION_FAILED,
    "NO_TASK_ID": ErrorCode.GENERATION_FAILED,
    "IMAGE_UPLOAD_INIT_FAILED": ErrorCode.GENERATION_FAILED,
    "IMAGE_UPLOAD_FAILED": ErrorCode.GENERATION_FAILED,
    "UPSCALING_ERROR": ErrorCode.GENERATION_FAILED,
    "TIMEOUT": ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
    "NO_ACCOUNTS": ErrorCode.SERVICE_NOT_INITIALIZED,
    "SERVICE_RESTARTED": ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
}


class ApiError(Exception):
    """Custom API error with structured response."""

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

    def to_dict(self) -> dict:
        """Convert to API response format."""
        error_data = {
            "code": self.code.value,
            "message": self.message,
        }
        if self.details:
            error_data["details"] = self.details

        return {
            "success": False,
            "error": error_data,
        }


def map_veo_error(error_code: Optional[str], message: str) -> ApiError:
    """Map VeoClientError to ApiError."""
    mapped_code = VEO_ERROR_MAPPING.get(error_code, ErrorCode.GENERATION_FAILED)

    # Determine status code based on error type
    if mapped_code == ErrorCode.RATE_LIMIT_EXCEEDED:
        status = 429
    elif mapped_code == ErrorCode.VIDEO_NOT_FOUND:
        status = 404
    elif mapped_code in (
        ErrorCode.SERVICE_NOT_INITIALIZED,
        ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
    ):
        status = 503
    elif mapped_code == ErrorCode.MAX_CONCURRENT_REQUESTS:
        status = 429
    elif mapped_code in (
        ErrorCode.VALIDATION_PROMPT_REQUIRED,
        ErrorCode.VALIDATION_INVALID_PARAMS,
        ErrorCode.VALIDATION_VIDEO_NOT_READY,
        ErrorCode.VALIDATION_TOO_MANY_VIDEOS,
        ErrorCode.VALIDATION_VIDEO_IDS_REQUIRED,
    ):
        status = 400
    else:
        status = 500

    return ApiError(code=mapped_code, message=message, status_code=status)
