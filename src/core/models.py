"""Pydantic models for VeoAPI."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class VideoStatus(str, Enum):
    PENDING = "pending"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VideoType(str, Enum):
    NORMAL = "normal"
    TRIMMED = "trimmed"
    MERGED = "merged"
    UPSCALE = "upscale"


class VideoMode(str, Enum):
    T2V = "t2v"
    I2V = "i2v"
    I2V_DUAL = "i2v_dual"
    R2V = "r2v"
    EXTEND = "extend"
    INSERT = "insert"
    REMOVE = "remove"
    RESHOOT = "reshoot"
    UPSCALE = "upscale"


class AspectRatio(str, Enum):
    LANDSCAPE = "16:9"
    PORTRAIT = "9:16"


class UserModel(str, Enum):
    VEO31_FAST = "veo31_fast"
    VEO31_PRO = "veo31_pro"
    VEO3_FAST = "veo3_fast"  # alias
    VEO3_PRO = "veo3_pro"  # alias
    VEO3_QUALITY = "veo3_quality"  # alias
    VEO2_FAST = "veo2_fast"  # alias
    SORA_2 = "sora_2"  # alias
    SORA_2_DASH = "sora-2"  # alias
    OMNI_FLASH = "omni_flash"
    OMNI_FLASH_DASH = "omni-flash"


# Database models
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
    created_at: datetime
    last_used_at: Optional[datetime] = None
    use_count: int = 0
    proxy: Optional[str] = None  # Per-account datacenter proxy URL (heavy traffic)
    residential_proxy: Optional[str] = None  # Per-account residential proxy (auth/generate/upscale)
    relaxed_disabled: bool = False  # Per-account relaxed model disable
    force_relaxed: bool = False  # Hard pin to lower-priority model (browser mode)
    # Browser provider (browser2api fork)
    browser_ready: bool = False  # True if account has a warm browser profile for flow UI
    browser_last_check: Optional[datetime] = None  # last successful browser session
    # Persisted cooldown deadline — survives service restarts so throttled/
    # resting/rate_limited accounts don't get re-enabled prematurely.
    cooldown_until: Optional[datetime] = None
    # Google OAuth refresh_token (cockpit-tools antigravity client).
    # When set, this account refreshes AT via OAuth instead of ST exchange.
    refresh_token: Optional[str] = None


class VideoDB(BaseModel):
    id: int
    video_id: str
    account_id: Optional[int] = None
    task_id: Optional[str] = None
    scene_id: Optional[str] = None
    prompt: str
    translated_prompt: Optional[str] = None
    detected_language: Optional[str] = None
    model: str
    google_model_key: str
    mode: VideoMode
    aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE
    start_image_path: Optional[str] = None
    end_image_path: Optional[str] = None
    reference_images_paths: Optional[str] = None  # JSON array of paths
    continuation_video_id: Optional[str] = None
    seed: Optional[int] = None
    status: VideoStatus = VideoStatus.PENDING
    progress: int = 0
    progress_message: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    download_url: Optional[str] = None
    video_url: Optional[str] = None
    cost: Optional[float] = None
    video_type: str = "normal"
    requested_resolution: Optional[str] = None
    original_video_id: Optional[str] = None
    original_video_ids: Optional[str] = None  # JSON for merged
    mask_path: Optional[str] = None  # path to saved insertion mask
    mask_coordinates: Optional[str] = None  # JSON encoding of MaskCoordinates
    file_size: Optional[int] = None  # bytes
    duration: Optional[float] = None  # seconds
    requested_duration: Optional[float] = None  # seconds
    # Browser provider (browser2api fork)
    provider: str = "veo"  # "veo" (http) | "browser" (flow UI)
    project_url: Optional[str] = None  # labs.google/fx/tools/flow/project/<uuid>
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    # When the video transitioned PENDING→GENERATING; used by
    # fail_stale_generating_videos to enforce the generating SLA. Stays put
    # across the whole generation (poll updates bump updated_at, not this).
    generating_since: Optional[datetime] = None


class AccountStatsDB(BaseModel):
    account_id: int
    total_videos: int = 0
    success_count: int = 0
    error_count: int = 0
    consecutive_errors: int = 0
    today_count: int = 0
    today_date: str


class ProxyConfigDB(BaseModel):
    id: int = 1
    enabled: bool = False
    url: Optional[str] = None


class ProxyStateDB(BaseModel):
    """Per-proxy rotation state. ``proxy_pool.txt`` stays the source list;
    this row tracks usage/cooldown so the rotator can pick rested proxies
    and bench burnt ones. ``status``: 'active' | 'benched' | 'cooldown'."""
    proxy_url: str
    label: Optional[str] = None
    status: str = "benched"
    last_assigned_at: Optional[datetime] = None
    last_generation_at: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None
    flagged_count: int = 0
    flagged_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class GenerationConfigDB(BaseModel):
    id: int = 1
    video_timeout: int = 1800
    poll_interval: int = 5
