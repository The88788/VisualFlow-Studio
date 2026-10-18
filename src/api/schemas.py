"""Request and response schemas for the internal parser service API."""

from datetime import datetime
from typing import Any, Optional, TypeVar, Generic

from pydantic import BaseModel, Field, model_validator
from pydantic.alias_generators import to_camel


# ==================== API Response Wrapper ====================

T = TypeVar("T")


class CamelModel(BaseModel):
    """Base model with camelCase serialization."""

    class Config:
        alias_generator = to_camel
        populate_by_name = True


class ApiResponse(BaseModel, Generic[T]):
    """Unified API response wrapper."""

    success: bool = True
    data: Optional[T] = None
    message: Optional[str] = None
    error: Optional[dict[str, Any]] = None

    class Config:
        populate_by_name = True


# ==================== Request Models ====================

class MaskCoordinates(BaseModel):
    """Coordinates for generating an insertion mask."""
    x: int
    y: int
    width: int
    height: int

class CreateVideoRequest(BaseModel):
    """Request to create a video."""

    prompt: str = Field(default="", description="Text prompt for video generation")
    model: str = Field(
        default="veo3_fast",
        description="Model to use (veo3_fast, veo3_pro, veo2_fast, sora-2, etc.)",
    )
    orientation: Optional[str] = Field(
        default=None,
        description="Video orientation: landscape (16:9) or portrait (9:16)",
    )
    seed: Optional[int] = Field(
        default=None,
        description="Random seed for reproducible generation",
    )
    # Image inputs
    image: Optional[str] = Field(
        default=None,
        description="Base64 encoded start image",
    )
    end_image: Optional[str] = Field(
        default=None,
        alias="endImage",
        description="Base64 encoded end image for dual mode",
    )
    reference_images: Optional[list[str]] = Field(
        default=None,
        alias="referenceImages",
        description="Base64 encoded reference images for R2V mode",
    )
    continuation_video_id: Optional[str] = Field(
        default=None,
        description="Video ID to extend",
    )
    insert_video_id: Optional[str] = Field(
        default=None,
        description="Video ID for object insertion",
    )
    remove_video_id: Optional[str] = Field(
        default=None,
        description="Video ID for object removal",
    )
    mask: Optional[str] = Field(
        default=None,
        description="Base64 encoded mask image for object insertion",
    )
    mask_coordinates: Optional[MaskCoordinates] = Field(
        default=None,
        alias="maskCoordinates",
        description="Coordinates (x, y, width, height) to generate a mask for object insertion",
    )
    reshoot_video_id: Optional[str] = Field(
        default=None,
        description="Video ID for camera motion reshoot",
    )
    reshoot_motion: Optional[str] = Field(
        default="forward",
        description="Camera motion: 'forward', 'backward', 'orbit_left', 'orbit_right', 'orbit_up', 'orbit_down', 'dolly_in_zoom_out', 'dolly_out_zoom_in'. Camera position: 'center', 'left', 'right', 'high', 'low', 'closer', 'further'.",
    )
    skip_continuation_image: bool = Field(
        default=False,
        alias="skipContinuationImage",
        description="If true, skip last frame extraction for continuation",
    )
    resolution: Optional[str] = Field(
        default=None,
        description="Target resolution: '720p' (default, no upscale), '1080p', or '4k'. Auto-upscales after generation.",
    )
    duration: float = Field(
        default=8.0,
        description="Requested video duration in seconds (4, 6, 8, or 10)",
    )
    provider: Optional[str] = Field(
        default=None,
        description="Provider hint (accepted but ignored)",
    )
    use_browser_captcha: bool = Field(
        default=False,
        alias="useBrowserCaptcha",
        description="Use browser-based captcha solver instead of Capsolver (experimental)",
    )

    class Config:
        populate_by_name = True

    def get_aspect_ratio(self) -> str:
        """Get aspect ratio from orientation field."""
        if self.orientation:
            return "9:16" if self.orientation in ("portrait", "9:16") else "16:9"
        return "16:9"

    def get_start_image(self) -> Optional[str]:
        """Get start image from image field."""
        return self.image

    @model_validator(mode="after")
    def validate_duration_by_model(self):
        """Validate duration selection strictly based on the model chosen."""
        model_lower = (self.model or "").lower().strip()
        is_omni = model_lower in ("omni_flash", "omni-flash")
        
        allowed_durations = (4.0, 6.0, 8.0, 10.0) if is_omni else (4.0, 6.0, 8.0)
        
        if self.duration not in allowed_durations:
            allowed_str = ", ".join(str(int(d)) for d in allowed_durations)
            raise ValueError(
                f"Duration for model '{self.model}' must be one of {allowed_str} seconds."
            )
        return self


class CreateImageRequest(BaseModel):
    """Text-to-image request for the flow.google.com provider."""

    prompt: str = Field(min_length=1, max_length=8000)
    model: str = Field(default="nano2")
    aspect: str = Field(default="1:1")
    count: int = Field(default=1, ge=1, le=4)

    @model_validator(mode="after")
    def validate_gflow_options(self):
        if self.model not in ("nano2", "nano-pro", "image4", "imagen4"):
            raise ValueError("Unsupported image model")
        if self.aspect not in ("9:16", "16:9", "1:1", "4:3", "3:4"):
            raise ValueError("Unsupported image aspect ratio")
        self.prompt = self.prompt.strip()
        if not self.prompt:
            raise ValueError("Prompt is required")
        return self


class TrimVideoRequest(BaseModel):
    """Request to trim a video by cutting from start and/or end."""

    start_offset_sec: Optional[float] = Field(
        default=None,
        ge=0,
        alias="startOffsetSec",
        description="Seconds to cut from the BEGINNING of the video",
    )
    end_offset_sec: Optional[float] = Field(
        default=None,
        ge=0,
        alias="endOffsetSec",
        description="Seconds to cut from the END of the video",
    )

    class Config:
        populate_by_name = True

    @model_validator(mode="after")
    def validate_at_least_one_offset(self):
        """Ensure at least one offset is provided."""
        if self.start_offset_sec is None and self.end_offset_sec is None:
            raise ValueError(
                "At least one of startOffsetSec or endOffsetSec is required"
            )
        return self


class MergeVideosRequest(BaseModel):
    """Request to merge videos."""

    video_ids: list[str] = Field(
        ...,
        min_length=2,
        alias="videoIds",
        description="List of video IDs to merge (2-10)",
    )
    end_offset_sec: Optional[float] = Field(
        default=None,
        alias="endOffsetSec",
        description="Global end offset for each clip",
    )
    per_video_end_offset_sec: Optional[Any] = Field(
        default=None,
        alias="perVideoEndOffsetSec",
        description="Per-video end offsets",
    )

    class Config:
        populate_by_name = True


class UpsampleVideoRequest(BaseModel):
    """Request to upsample a video to 1080p or 4k."""

    seed: Optional[int] = Field(
        default=None,
        description="Random seed for upsampling",
    )
    resolution: Optional[str] = Field(
        default="1080p",
        description="Target resolution (1080p or 4k)",
    )


# ==================== Response Models ====================

class VideoMetadata(CamelModel):
    """Video metadata."""

    account: Optional[str] = None
    has_image: bool = False
    project_id: Optional[str] = None


class VideoResponse(CamelModel):
    """Unified video response."""

    video_id: str
    prompt: str
    detected_language: Optional[str] = None
    model: str
    orientation: str = "landscape"
    seed: Optional[int] = None
    status: str
    progress: int = 0
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    processing_time: Optional[float] = None
    download_url: Optional[str] = None
    upsample_id: Optional[str] = Field(default=None, alias="upsampleId")
    file_size: Optional[str] = None  # Formatted: "15.2 MB"
    duration: Optional[str] = None  # Formatted: "4.2s"
    metadata: Optional[VideoMetadata] = None
    type: str = "normal"
    original_video_id: Optional[str] = None
    original_video_ids: Optional[list[str]] = None
    original_videos_count: Optional[int] = None
    is_continuation: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None



class CreateVideoResponse(CamelModel):
    """Response for video creation request."""

    video_id: str
    status: str
    message: str = "Video queued for generation"


class VideoListResponse(BaseModel):
    """Response for video list request."""

    videos: list[VideoResponse]
    total: int
    limit: int
    offset: int
    has_more: bool = Field(alias="hasMore", default=False)

    class Config:
        populate_by_name = True


class InstanceStatus(CamelModel):
    """Status of an account instance."""

    id: int
    email: str
    status: str
    is_active: bool
    project_id: Optional[str] = None
    credits: Optional[int] = None
    total_videos: int = 0
    success_count: int = 0
    error_count: int = 0
    today_count: int = 0


class ServiceStatusResponse(CamelModel):
    """Service status response."""

    service: str = "veoapi-parser"
    status: str = "running"
    instances: list[InstanceStatus] = []
    stats: dict[str, Any] = {}


class ModelsResponse(BaseModel):
    """Models list response."""

    all_models: list[str] = Field(alias="allModels")
    image_supported_models: list[str] = Field(alias="imageSupportedModels")
    messages: list[str] = []

    class Config:
        populate_by_name = True


# ==================== Account Schemas ====================

class AccountRequest(BaseModel):
    """Request to add an account."""

    email: str
    st: Optional[str] = None
    refresh_token: Optional[str] = None
    remark: Optional[str] = None
    proxy: Optional[str] = None

    @model_validator(mode="after")
    def check_auth_method(self) -> "AccountRequest":
        if not self.st and not self.refresh_token:
            raise ValueError("Either st or refresh_token must be provided")
        return self


class AddFullAccountRequest(BaseModel):
    """Request to add account to both DB and accounts.json."""

    name: str
    email: str
    password: str
    otp_secret: str = ""
    proxy: str = ""
    residential_proxy: Optional[str] = None  # Residential proxy for auth/generate/upscale


class AccountResponse(CamelModel):
    """Account response."""

    id: int
    email: str
    is_active: bool
    status: str = "active"
    project_id: Optional[str] = None
    credits: Optional[int] = None
    remark: Optional[str] = None
    created_at: datetime
    last_used_at: Optional[datetime] = None
    use_count: int = 0
    total_videos: int = 0
    success_count: int = 0
    error_count: int = 0
    consecutive_errors: int = 0
    today_count: int = 0


class AccountListResponse(BaseModel):
    """Account list response."""

    accounts: list[AccountResponse]
    total: int
