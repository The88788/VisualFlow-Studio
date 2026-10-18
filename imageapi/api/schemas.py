"""Request/response schemas for ImageAPI (matching geminiskiyyy API)."""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ModelEnum(str, Enum):
    FAST = "fast"
    THINKING = "thinking"


class AspectRatioEnum(str, Enum):
    AUTO = "auto"
    SQUARE = "1:1"
    LANDSCAPE = "16:9"
    PORTRAIT = "9:16"
    PHOTO = "4:3"
    PHOTO_PORTRAIT = "3:4"
    WIDE = "3:2"
    WIDE_PORTRAIT = "2:3"
    CINEMATIC = "21:9"
    SOCIAL = "4:5"
    SOCIAL_WIDE = "5:4"


class TaskStatusEnum(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    FILTERED = "filtered"


# ---- Requests ----

class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000, description="Image prompt")
    model: ModelEnum = Field(default=ModelEnum.FAST, description="Model: fast or thinking")
    references: list[str] = Field(default_factory=list, description="Base64 reference images (max 10)")
    aspect_ratio: Optional[AspectRatioEnum] = Field(default=None, description="Aspect ratio")
    resolution: Optional[str] = Field(default=None, description="Output resolution: '4k' for 4K, otherwise 2K (default)")


# ---- Responses ----

class GenerateResponse(BaseModel):
    task_id: str
    status: TaskStatusEnum
    message: str


class ImageInfo(BaseModel):
    filename: str
    base64: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: TaskStatusEnum
    account_name: Optional[str] = None
    images: Optional[list[ImageInfo]] = None  # base64, only when S3 is disabled
    image_urls: Optional[list[str]] = None    # S3 URLs, when S3 is enabled
    processing_time: Optional[float] = None
    error: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None


class AccountInfo(BaseModel):
    id: int
    email: str
    status: str
    is_active: bool
    credits: Optional[int] = None
    tier: Optional[str] = None
    proxy: Optional[str] = None
    last_used_at: Optional[str] = None
    use_count: int = 0
    total_tasks: int = 0
    success_count: int = 0
    error_count: int = 0
    consecutive_errors: int = 0


class AccountsResponse(BaseModel):
    total: int
    active: int
    inactive: int
    accounts: list[AccountInfo]


class HealthResponse(BaseModel):
    status: str  # healthy, degraded, unhealthy
    accounts_active: int
    accounts_total: int
    queue_size: int


class StatsResponse(BaseModel):
    total_tasks: int
    pending: int
    processing: int
    success: int
    failed: int
    filtered: int
    accounts_active: int
    accounts_total: int
    queue_size: int
    uptime_seconds: float


class MemoryResponse(BaseModel):
    rss_mb: float
    vms_mb: float
    task_count: int


class ErrorResponse(BaseModel):
    success: bool = False
    error: dict[str, Any]
