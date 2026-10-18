"""Pydantic models for ImageAPI."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    FILTERED = "filtered"


class AccountStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    NEEDS_REAUTH = "needs_reauth"
    DISABLED = "disabled"
    COOLDOWN = "cooldown"


class AccountDB(BaseModel):
    id: int
    email: str
    st: str
    at: Optional[str] = None
    at_expires: Optional[datetime] = None
    project_id: Optional[str] = None
    credits: Optional[int] = None
    user_paygate_tier: Optional[str] = None
    is_active: bool = True
    status: str = "active"
    remark: Optional[str] = None
    proxy: Optional[str] = None
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    use_count: int = 0


class AccountStatsDB(BaseModel):
    id: int
    account_id: int
    total_tasks: int = 0
    success_count: int = 0
    error_count: int = 0
    consecutive_errors: int = 0
    today_count: int = 0
    today_date: Optional[str] = None


class TaskDB(BaseModel):
    id: int
    task_id: str
    account_id: Optional[int] = None
    prompt: str
    model: str = "fast"
    aspect_ratio: Optional[str] = None
    seed: Optional[int] = None
    resolution: Optional[str] = None
    status: str = "pending"
    images_count: int = 0
    result_dir: Optional[str] = None
    image_urls: Optional[str] = None  # JSON-encoded list of S3 URLs
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    processing_time: Optional[float] = None
