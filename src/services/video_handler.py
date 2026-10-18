"""Video generation handler - orchestrates the video generation process."""

import asyncio
import base64
import json
import os
import random
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..core.config import IMAGES_DIR, TMP_DIR, config as app_config
from ..core.database import db
from ..core.logger import logger
from ..core.models import VideoMode, VideoStatus
from .load_balancer import load_balancer
from .storage import storage
from .token_manager import TokenManager
from .watermark_service import watermark_service
from .browser_captcha import get_browser_captcha
from .proxy_pool import get_proxy_pool
from .veo_client import (
    VeoClient,
    VeoClientError,
    RELAXED_TO_FAST_FALLBACK,
    resolve_model,
    supports_feature,
)

# FFmpeg subprocess timeout (5 minutes)
_FFMPEG_TIMEOUT = 300


class _DynamicSemaphore:
    """Semaphore whose limit adjusts based on active account count.

    limit = max(1, active_accounts * per_account_limit)
    Checked on every acquire — no restart needed when accounts change.
    """

    def __init__(self, per_account: int = 2, hard_max: int = 30):
        self._per_account = per_account
        self._hard_max = hard_max
        self._active = 0
        self._lock = asyncio.Lock()
        self._waiters: asyncio.Queue = asyncio.Queue()

    async def _get_limit(self) -> int:
        active_accounts = await db.get_active_accounts()
        limit = max(1, len(active_accounts) * self._per_account)
        return min(limit, self._hard_max)

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                limit = await self._get_limit()
                if self._active < limit:
                    self._active += 1
                    return
            # Over limit — wait for a release signal
            waiter = asyncio.get_event_loop().create_future()
            self._waiters.put_nowait(waiter)
            await waiter

    def release(self) -> None:
        self._active -= 1
        # Wake one waiter if any
        try:
            waiter = self._waiters.get_nowait()
            if not waiter.done():
                waiter.set_result(None)
        except asyncio.QueueEmpty:
            pass

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        self.release()

    @property
    def active_count(self) -> int:
        return self._active


class VideoHandler:
    """Orchestrates video generation process."""

    def __init__(self, token_manager: TokenManager, max_concurrent_videos: int = 10):
        self.token_manager = token_manager
        # Dynamic semaphore: one worker slot per active account.
        #
        # Why ``per_account=1`` (was 2):
        # With ``per_account=2`` and 2 active accounts we'd spawn 4 workers.
        # The browser provider's ``_per_account_limit`` is 1, so 2 of those
        # workers immediately hit a saturated reservation and idle inside
        # ``_wait_for_browser_account`` for up to 5 minutes — while still
        # holding their semaphore slot. New pending videos can't get a
        # worker until those idle ones release, producing the exact
        # "1 generating, 3 stuck pending" symptom on a 2-account setup.
        # One worker per account matches actual capacity downstream
        # (browser path: 1 tab per Chrome, HTTP path: per-account rate).
        self._semaphore = _DynamicSemaphore(per_account=1, hard_max=max_concurrent_videos)
        # Busy-account tracking for the HTTP (veo) path. Browser provider
        # already does its own per-account slot reservation; this set keeps
        # the HTTP path from picking the same account in two _process_video
        # calls running concurrently.
        self._busy_accounts: set[int] = set()
        self._busy_lock = asyncio.Lock()
        # Track uploaded media per video for cleanup after completion
        # video_id -> list of {"st": str, "media_ids": list[str], "account_email": str}
        self._pending_media_cleanup: dict[str, list[dict]] = {}

        # --- Relaxed model circuit breaker ---
        # video_id -> generation start timestamp
        self._gen_start_times: dict[str, float] = {}
        # Consecutive slow relaxed generations (>3 min)
        self._relaxed_slow_streak: int = 0
        # If set, relaxed models disabled until this timestamp
        self._relaxed_disabled_until: float = 0
        # Count of probe videos after cooldown ends
        self._relaxed_probe_count: int = 0
        # Manual override: force relaxed off regardless of circuit breaker state
        self._relaxed_force_disabled: bool = False
        # Constants
        self._RELAXED_SLOW_THRESHOLD = 180  # 3 minutes
        self._RELAXED_STREAK_LIMIT = 3      # 3 slow videos -> disable
        self._RELAXED_COOLDOWN = 1800        # 30 minutes cooldown
        self._RELAXED_PROBE_LIMIT = 3        # 3 probe videos after cooldown

        # Auto-retries for internal Google errors
        self._auto_retries: dict[str, int] = {}

        # Per-account last generate_video submit timestamp for inter-request delay.
        # Enforces a minimum gap between successive API calls on the same account
        # to reduce abuse-signal intensity.
        self._account_last_gen: dict[int, float] = {}

        # Per-video excluded account IDs across invisible retries (e.g. when
        # polling returns PUBLIC_ERROR_UNUSUAL_ACTIVITY we re-queue the video
        # but want to skip the account that just got flagged).
        self._excluded_accounts: dict[str, set[int]] = {}
        # Cap on poll-driven invisible retries per video (separate from
        # _process_video's MAX_GLOBAL_RETRIES which only applies to the
        # generation-submit phase).
        self._unusual_retries: dict[str, int] = {}
        self._UNUSUAL_RETRY_LIMIT = 5

        # --- MAX_RETRIES_EXCEEDED reauth trigger ---
        self._max_retries_exceeded_count: int = 0
        self._reauth_triggered_at: float = 0
        # After this many consecutive MAX_RETRIES_EXCEEDED, mark all accounts needs_reauth
        self._REAUTH_TRIGGER_THRESHOLD = 3
        # Cooldown between reauth triggers (1 hour)
        self._REAUTH_TRIGGER_COOLDOWN = 3600

    @property
    def veo_client(self) -> VeoClient:
        """Get VeoClient from token manager."""
        return self.token_manager.veo_client

    async def _cleanup_uploaded_media(self, video_id: str) -> None:
        """Delete all uploaded media associated with a video from Google accounts."""
        entries = self._pending_media_cleanup.pop(video_id, [])
        if not entries:
            return

        total = 0
        for entry in entries:
            try:
                account_client = self.token_manager.veo_client
                # Try to get account-specific client by email
                account = await db.get_account_by_email(entry["account_email"])
                if account:
                    account_client = self.token_manager.get_veo_client_for_account(account)
                    st = account.st  # Use fresh ST from DB
                else:
                    st = entry["st"]  # Fallback to saved ST

                await account_client.delete_media(st, entry["media_ids"])
                total += len(entry["media_ids"])
            except Exception as e:
                logger.debug(f"Media cleanup for {entry['account_email']}: {e}")

        if total > 0:
            logger.info(f"Cleaned up {total} uploaded media for video {video_id}")

    async def _get_proxy_pool(self) -> list[str]:
        """Collect all unique proxies from all accounts."""
        accounts = await db.get_all_accounts()
        proxies = set()
        for acc in accounts:
            if acc.proxy:
                proxies.add(acc.proxy)
        return list(proxies)

    async def _try_proxy_rotation(
        self, account, account_client, video, video_id,
        prompt_for_api, video_aspect,
        start_image_uri, end_image_uri,
        reference_image_uris, uploaded_media_ids,
    ) -> bool:
        """On RATE_LIMITED: try up to 3 random proxies from pool.

        If generation succeeds with a new proxy, assign it to the account and return True.
        """
        all_proxies = await self._get_proxy_pool()
        # Exclude current account's proxy
        candidates = [p for p in all_proxies if p != account.proxy]
        if not candidates:
            logger.warning(f"Proxy rotation for {account.email}: no alternative proxies in pool")
            return False

        random.shuffle(candidates)
        attempts = min(3, len(candidates))

        for i in range(attempts):
            new_proxy = candidates[i]
            logger.info(
                f"Proxy rotation for {account.email}: attempt {i+1}/{attempts} "
                f"with proxy {new_proxy[:30]}..."
            )
            try:
                # Create temp client with new proxy
                temp_client = VeoClient(
                    capsolver=self.token_manager.capsolver,
                    proxy=new_proxy,
                )
                temp_client.browser_captcha = get_browser_captcha()

                # Re-read native IDs for extend/insert/remove/reshoot
                native_extend_id = None
                native_insert_id = None
                native_remove_id = None
                native_reshoot_id = None
                reshoot_motion = None
                if video.continuation_video_id:
                    src_vid = await db.get_video(video.continuation_video_id)
                    if src_vid:
                        if video.mode == VideoMode.EXTEND.value:
                            native_extend_id = src_vid.video_url
                        elif video.mode == VideoMode.INSERT.value:
                            native_insert_id = src_vid.task_id
                        elif video.mode == VideoMode.REMOVE.value:
                            native_remove_id = src_vid.task_id
                        elif video.mode == VideoMode.RESHOOT.value:
                            native_reshoot_id = src_vid.video_url
                            reshoot_motion = video.prompt

                insertion_mask_base64 = None
                if video.mask_path:
                    try:
                        mask_bytes = await self._load_image(video.mask_path)
                        insertion_mask_base64 = base64.b64encode(mask_bytes).decode('utf-8')
                    except Exception:
                        pass

                result = await temp_client.generate_video(
                    at=account.at,
                    st=account.st,
                    prompt=prompt_for_api,
                    model_key=video.google_model_key,
                    aspect_ratio=video.aspect_ratio,
                    start_image_uri=start_image_uri,
                    end_image_uri=end_image_uri,
                    reference_image_uris=reference_image_uris,
                    continuation_video_id=video.continuation_video_id,
                    seed=video.seed,
                    user_paygate_tier=account.user_paygate_tier or "PAYGATE_TIER_TWO",
                    native_extend_media_id=native_extend_id,
                    native_insert_media_id=native_insert_id,
                    native_remove_media_id=native_remove_id,
                    native_reshoot_media_id=native_reshoot_id,
                    reshoot_motion=reshoot_motion,
                    insertion_mask_base64=insertion_mask_base64,
                    account_email=account.email,
                )

                # SUCCESS — assign new proxy to account
                await db.update_account(account.id, proxy=new_proxy)
                # Reset cached VeoClient for old proxy
                await self.token_manager.reset_client_for_proxy(account.proxy)
                logger.info(
                    f"Proxy rotation SUCCESS for {account.email}: "
                    f"new proxy {new_proxy[:30]}... assigned"
                )

                # Track generation
                self._gen_start_times[video_id] = time.time()
                await db.update_video(
                    video_id,
                    task_id=result.task_id,
                    scene_id=result.scene_id,
                    seed=result.seed,
                    progress_message="Generating...",
                )
                if result.project_id and result.project_id != account.project_id:
                    await db.update_account(account.id, project_id=result.project_id)

                if uploaded_media_ids:
                    if video_id not in self._pending_media_cleanup:
                        self._pending_media_cleanup[video_id] = []
                    self._pending_media_cleanup[video_id].append({
                        "st": account.st,
                        "media_ids": list(uploaded_media_ids),
                        "account_email": account.email,
                    })

                await temp_client.close()
                return True

            except VeoClientError as e:
                logger.warning(
                    f"Proxy rotation attempt {i+1}/{attempts} failed for {account.email}: {e}"
                )
                try:
                    await temp_client.close()
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"Proxy rotation unexpected error: {e}")
                try:
                    await temp_client.close()
                except Exception:
                    pass

        logger.warning(f"Proxy rotation exhausted for {account.email}: all {attempts} proxies failed")
        return False

    async def _auto_upscale(self, video_id: str, video, gen_result, account) -> None:
        """Auto-upscale a completed video to the requested resolution."""
        resolution = video.requested_resolution

        try:
            # Refresh account token
            if not await self.token_manager.refresh_account_token(account.id):
                raise Exception("Failed to refresh account token for upscale")

            account = await db.get_account(account.id)
            if not account or not account.is_active:
                # Fallback to any available account
                account = await load_balancer.get_account()
                if not account:
                    raise Exception("No available accounts for upscale")
                if not await self.token_manager.refresh_account_token(account.id):
                    raise Exception("Failed to refresh fallback account token")
                account = await db.get_account(account.id)

            account_client = self.token_manager.get_veo_client_for_account(account)

            # Always use browser captcha (fallback to capsolver handled in veo_client)
            account_client.browser_captcha = get_browser_captcha()

            # Ensure project_id
            if not account.project_id:
                project_id = await account_client.create_project(account.st)
                if project_id:
                    await db.update_account(account.id, project_id=project_id)
                    account = await db.get_account(account.id)

            video_media_id = gen_result.video_url
            if not video_media_id:
                raise Exception("No mediaGenerationId from base generation")

            aspect_ratio_map = {
                "16:9": "VIDEO_ASPECT_RATIO_LANDSCAPE",
                "9:16": "VIDEO_ASPECT_RATIO_PORTRAIT",
            }
            aspect_ratio = aspect_ratio_map.get(
                video.aspect_ratio if isinstance(video.aspect_ratio, str) else video.aspect_ratio.value,
                "VIDEO_ASPECT_RATIO_LANDSCAPE",
            )

            logger.info(f"Auto-upscale {video_id}: {resolution} on {account.email}")

            MAX_UPSCALE_CAPTCHA_RETRIES = 7
            upscale_result = None
            for attempt in range(MAX_UPSCALE_CAPTCHA_RETRIES):
                try:
                    # Upscale POST: use residential if available (small payload),
                    # otherwise rotate datacenter pool (must NOT pick a
                    # residential entry — heavy traffic would burn that budget).
                    _upscale_proxy = None if account.residential_proxy else await get_proxy_pool().get_next_datacenter()
                    upscale_result = await account_client.upscale_video(
                        at=account.at,
                        project_id=account.project_id,
                        video_media_id=video_media_id,
                        aspect_ratio=aspect_ratio,
                        resolution=resolution,
                        user_paygate_tier=account.user_paygate_tier or "PAYGATE_TIER_TWO",
                        account_email=account.email,
                        request_proxy=_upscale_proxy,
                    )
                    break
                except VeoClientError as e:
                    if e.error_code in ("CAPTCHA_FAILED", "FORBIDDEN") and attempt < MAX_UPSCALE_CAPTCHA_RETRIES - 1:
                        logger.warning(
                            f"Auto-upscale {video_id}: captcha/forbidden, attempt {attempt + 1}/{MAX_UPSCALE_CAPTCHA_RETRIES}"
                        )
                        await asyncio.sleep(3)
                        continue
                    raise

            # Update the video with upscale task_id for polling
            await db.update_video(
                video_id,
                task_id=upscale_result.task_id,
                scene_id=upscale_result.scene_id if hasattr(upscale_result, 'scene_id') else None,
                google_model_key=f"veo_3_1_upsampler_{resolution}",
                video_type="upscale",
                progress_message=f"Upscaling to {resolution}...",
            )

            logger.info(f"Auto-upscale started for {video_id}, task={upscale_result.task_id}")

        except Exception as e:
            logger.error(f"Auto-upscale failed for {video_id}: {e}")
            # Fall back: mark as completed with base resolution
            await db.update_video(
                video_id,
                status=VideoStatus.COMPLETED.value,
                download_url=gen_result.download_url,
                completed_at=datetime.utcnow(),
                progress_message=None,
                error_message=f"Upscale to {resolution} failed: {e}",
            )
            asyncio.create_task(self._extract_file_metadata(video_id))

    def _determine_mode(
        self,
        start_image: Optional[str],
        end_image: Optional[str],
        reference_images: Optional[list[str]],
    ) -> VideoMode:
        """Determine generation mode based on inputs."""
        if reference_images:
            return VideoMode.R2V
        if start_image and end_image:
            return VideoMode.I2V_DUAL
        if start_image:
            return VideoMode.I2V
        return VideoMode.T2V

    async def _download_from_uri(self, uri: str) -> bytes:
        """Download image or video from http/https URL or AWS S3 URI."""
        if uri.startswith(("http://", "https://")):
            import httpx
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                response = await client.get(uri)
                response.raise_for_status()
                return response.content
        elif uri.startswith("s3://"):
            import aioboto3
            parts = uri[5:].split("/", 1)
            if len(parts) != 2:
                raise ValueError(f"Invalid S3 URI format: {uri}")
            bucket, key = parts
            
            # Use same credentials as S3 storage if available
            session = aioboto3.Session(
                aws_access_key_id=app_config.s3_access_key or None,
                aws_secret_access_key=app_config.s3_secret_key or None,
                region_name=app_config.s3_region or "us-east-1",
            )
            endpoint_url = app_config.s3_endpoint_url or None
            async with session.client("s3", endpoint_url=endpoint_url) as s3_client:
                response = await s3_client.get_object(Bucket=bucket, Key=key)
                async with response["Body"] as stream:
                    return await stream.read()
        else:
            raise ValueError(f"Unsupported URI scheme: {uri}")

    async def _save_image(
        self,
        video_id: str,
        image_data: str,
        filename: str,
    ) -> str:
        """Save base64, S3 URI, or HTTP/HTTPS image/video to storage."""
        if image_data.startswith(("s3://", "http://", "https://")):
            try:
                image_bytes = await self._download_from_uri(image_data)
            except Exception as e:
                raise ValueError(f"Failed to download input from URI '{image_data}': {e}")

            content_type = "image/jpeg"
            # Auto-detect if it is a video from URI extension or magic bytes
            is_vid = image_data.lower().split("?")[0].endswith((".mp4", ".mov", ".avi", ".webm"))
            if not is_vid and len(image_bytes) > 4:
                # Fallback: check magic bytes for MP4 (ftyp)
                if b"ftyp" in image_bytes[:30]:
                    is_vid = True
            
            if is_vid:
                content_type = "video/mp4"
                if filename.endswith(".jpg"):
                    filename = filename[:-4] + ".mp4"
        else:
            content_type = "image/jpeg"
            if "," in image_data:
                header = image_data.split(",", 1)[0]
                if "video" in header.lower():
                    content_type = "video/mp4"
                    if filename.endswith(".jpg"):
                        filename = filename[:-4] + ".mp4"
                image_data = image_data.split(",", 1)[1]

            try:
                image_bytes = base64.b64decode(image_data)
            except Exception as e:
                raise ValueError(f"Invalid base64 data: {e}")

        key = f"images/{video_id}/{filename}"
        await storage.upload(key, image_bytes, content_type=content_type)
        return key

    async def _load_image(self, image_path: str) -> bytes:
        """Load image from storage."""
        return await storage.download(image_path)

    async def _extract_last_frame(
        self,
        video_url: str,
        video_id: str,
    ) -> Optional[str]:
        """Extract last frame from video for continuation."""
        import subprocess
        import httpx

        try:
            temp_video = TMP_DIR / f"{video_id}_source.mp4"
            output_frame = TMP_DIR / f"{video_id}_continuation_frame.jpg"

            logger.debug(f"Downloading video for frame extraction: {video_url}")
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("GET", video_url, follow_redirects=True) as response:
                    if response.status_code != 200:
                        logger.error(f"Failed to download video: HTTP {response.status_code}")
                        return None

                    with open(temp_video, "wb") as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)

            probe_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(temp_video)
            ]
            result = await asyncio.to_thread(
                subprocess.run, probe_cmd, capture_output=True, text=True,
                timeout=_FFMPEG_TIMEOUT,
            )

            if result.returncode != 0:
                logger.error(f"ffprobe failed: {result.stderr}")
                temp_video.unlink(missing_ok=True)
                return None

            duration = float(result.stdout.strip())

            extract_time = max(0, duration - 0.1)
            extract_cmd = [
                "ffmpeg", "-y",
                "-ss", str(extract_time),
                "-i", str(temp_video),
                "-vframes", "1",
                "-q:v", "2",
                str(output_frame)
            ]

            result = await asyncio.to_thread(
                subprocess.run, extract_cmd, capture_output=True, text=True,
                timeout=_FFMPEG_TIMEOUT,
            )

            temp_video.unlink(missing_ok=True)

            if result.returncode != 0:
                logger.error(f"ffmpeg frame extraction failed: {result.stderr}")
                return None

            if not output_frame.exists():
                logger.error("Frame extraction produced no output")
                return None

            key = f"images/{video_id}/continuation_frame.jpg"
            await storage.upload_file(key, output_frame, content_type="image/jpeg")
            output_frame.unlink(missing_ok=True)
            logger.info(f"Extracted last frame for continuation: {key}")
            return key

        except Exception as e:
            logger.error(f"Failed to extract last frame: {e}")
            return None

    async def _extract_file_metadata(self, video_id: str) -> None:
        """Extract file_size and duration, then upload video to S3 storage."""
        import subprocess
        import httpx

        video = await db.get_video(video_id)
        if not video or not video.download_url:
            return

        temp_path = TMP_DIR / f"{video_id}_meta.mp4"

        try:
            # Download video from Google to temp
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("GET", video.download_url, follow_redirects=True) as response:
                    if response.status_code != 200:
                        return

                    with open(temp_path, "wb") as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)

            # ── Watermark removal (runs in thread pool, does not block event loop) ──
            # Only processes the small bottom-right region; most CPU time is FFmpeg
            # encoding which uses ultrafast preset to keep load minimal.
            clean_path = TMP_DIR / f"{video_id}_clean.mp4"
            try:
                # Disabling watermark service temporarily because of false positives
                # wm_removed = await watermark_service.process_if_needed(temp_path, clean_path)
                wm_removed = False
            except Exception as _wm_exc:
                wm_removed = False
                logger.warning(f"Watermark service error for {video_id}: {_wm_exc}")

            if wm_removed:
                temp_path.unlink(missing_ok=True)
                temp_path = clean_path
            else:
                clean_path.unlink(missing_ok=True)
            # ─────────────────────────────────────────────────────────────────────

            file_size = temp_path.stat().st_size

            # Get duration via ffprobe
            probe_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(temp_path)
            ]
            result = await asyncio.to_thread(
                subprocess.run, probe_cmd, capture_output=True, text=True,
                timeout=_FFMPEG_TIMEOUT,
            )

            duration = None
            if result.returncode == 0:
                duration = float(result.stdout.strip())

            # Upload to S3 storage so download endpoint serves from S3
            video_key = f"videos/{video_id}.mp4"
            await storage.upload_file(video_key, temp_path, content_type="video/mp4")
            logger.info(f"Video {video_id} uploaded to storage: {video_key}")

            temp_path.unlink(missing_ok=True)

            # Update DB: replace Google URL with storage key
            update_data = {
                "file_size": file_size,
                "download_url": f"{video_id}.mp4",
            }
            if duration is not None:
                update_data["duration"] = duration

            await db.update_video(video_id, **update_data)
            logger.info(f"Video {video_id} metadata: size={file_size}, duration={duration}")

        except Exception as e:
            logger.error(f"Failed to extract file metadata for {video_id}: {e}")
            temp_path.unlink(missing_ok=True)

    async def _concatenate_extend_video(
        self,
        video_id: str,
        video,
        gen_result,
        account,
    ) -> None:
        """
        After Extend generation completes, call Google's native concatenation API
        to merge original + extension into a single video.

        The concatenation result comes as base64 encodedVideo in the response,
        which we decode and save as the final video file.
        """
        try:
            source_video = await db.get_video(video.continuation_video_id)
            if not source_video or not source_video.video_url:
                logger.warning(
                    f"Concat {video_id}: source video {video.continuation_video_id} "
                    f"not found or has no mediaGenerationId, falling back to extension-only"
                )
                await self._finalize_completed_video(video_id, gen_result)
                return

            new_media_id = gen_result.video_url
            if not new_media_id:
                logger.warning(
                    f"Concat {video_id}: extension has no mediaGenerationId, "
                    f"falling back to extension-only"
                )
                await self._finalize_completed_video(video_id, gen_result)
                return

            original_media_id = source_video.video_url

            # Build inputVideos — original first, then extension (skip first 1s of extension
            # to avoid duplicate frame at the junction, matching web UI behavior)
            input_videos = [
                {
                    "mediaGenerationId": original_media_id,
                    "lengthNanos": 8000,
                    "startTimeOffset": "0s",
                    "endTimeOffset": "8s",
                },
                {
                    "mediaGenerationId": new_media_id,
                    "lengthNanos": 8000,
                    "startTimeOffset": "1s",
                    "endTimeOffset": "8s",
                },
            ]

            await db.update_video(
                video_id,
                progress=90,
                progress_message="Concatenating with original...",
            )

            account_client = self.token_manager.get_veo_client_for_account(account)

            logger.info(
                f"Concat {video_id}: merging {original_media_id[:20]}... + {new_media_id[:20]}..."
            )

            # Start concatenation
            operation_name = await account_client.run_concatenation(
                at=account.at,
                input_videos=input_videos,
            )

            # Poll until complete — returns raw video bytes (decoded from base64)
            video_bytes = await account_client.poll_concatenation(
                at=account.at,
                operation_name=operation_name,
                timeout=300,
                poll_interval=5,
            )

            if not video_bytes:
                logger.warning(
                    f"Concat {video_id}: concatenation failed or returned no data, "
                    f"falling back to extension-only"
                )
                await self._finalize_completed_video(video_id, gen_result)
                return

            # Save concatenated video to temp file
            temp_path = TMP_DIR / f"{video_id}_concat.mp4"
            with open(temp_path, "wb") as f:
                f.write(video_bytes)

            file_size = temp_path.stat().st_size

            # Get duration via ffprobe
            import subprocess
            probe_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(temp_path)
            ]
            result = await asyncio.to_thread(
                subprocess.run, probe_cmd, capture_output=True, text=True,
                timeout=_FFMPEG_TIMEOUT,
            )
            duration = None
            if result.returncode == 0:
                try:
                    duration = float(result.stdout.strip())
                except ValueError:
                    pass

            # Upload to S3
            video_key = f"videos/{video_id}.mp4"
            await storage.upload_file(video_key, temp_path, content_type="video/mp4")
            temp_path.unlink(missing_ok=True)

            logger.info(
                f"Concat {video_id}: uploaded concatenated video "
                f"({file_size} bytes, {duration}s)"
            )

            # Update DB with final result
            update_data = {
                "status": VideoStatus.COMPLETED.value,
                "download_url": f"{video_id}.mp4",
                "video_url": gen_result.video_url,
                "cost": gen_result.cost,
                "completed_at": datetime.utcnow(),
                "progress": 100,
                "progress_message": None,
                "file_size": file_size,
            }
            if duration is not None:
                update_data["duration"] = duration

            await db.update_video(video_id, **update_data)
            asyncio.create_task(self._cleanup_uploaded_media(video_id))

        except Exception as e:
            logger.error(f"Concat {video_id}: error during concatenation: {e}")
            # Fallback: complete with just the extension segment
            await self._finalize_completed_video(video_id, gen_result)

    async def _finalize_completed_video(
        self,
        video_id: str,
        gen_result,
    ) -> None:
        """Finalize a completed video (non-concat path or fallback)."""
        await db.update_video(
            video_id,
            status=VideoStatus.COMPLETED.value,
            download_url=gen_result.download_url,
            video_url=gen_result.video_url,
            cost=gen_result.cost,
            completed_at=datetime.utcnow(),
            progress=100,
            progress_message=None,
        )
        asyncio.create_task(self._extract_file_metadata(video_id))
        asyncio.create_task(self._cleanup_uploaded_media(video_id))

    async def create_video(
        self,
        prompt: str,
        model: str = "veo31_fast",
        aspect_ratio: str = "16:9",
        start_image: Optional[str] = None,
        end_image: Optional[str] = None,
        reference_images: Optional[list[str]] = None,
        continuation_video_id: Optional[str] = None,
        insert_video_id: Optional[str] = None,
        remove_video_id: Optional[str] = None,
        reshoot_video_id: Optional[str] = None,
        reshoot_motion: str = "forward",
        mask: Optional[str] = None,
        mask_coordinates: Optional["MaskCoordinates"] = None,
        skip_continuation_image: bool = False,
        seed: Optional[int] = None,
        detected_language: Optional[str] = None,
        requested_resolution: Optional[str] = None,
        use_browser_captcha: bool = False,
        duration: Optional[float] = None,
    ) -> str:
        """Create a new video generation request."""
        video_id = str(uuid.uuid4())

        # Determine mode
        mode = self._determine_mode(start_image, end_image, reference_images)

        # Resolve Google model key
        google_model_key = resolve_model(model, mode.value, aspect_ratio)
        if not google_model_key:
            raise ValueError(
                f"Unsupported model/mode/aspect combination: "
                f"{model}/{mode.value}/{aspect_ratio}"
            )

        # Pre-apply the relaxed → fast downgrade when relaxed is globally
        # disabled or the cooldown is active, so the saved google_model_key
        # reflects the model that will actually run (dashboard otherwise
        # shows "Relaxed" for requests that ultimately use the fast model).
        _fast_key = RELAXED_TO_FAST_FALLBACK.get(google_model_key)
        if _fast_key:
            now = time.time()
            if self._relaxed_force_disabled or self._relaxed_disabled_until > now:
                google_model_key = _fast_key

        # Validate features
        if end_image and not supports_feature(model, "dual_mode"):
            raise ValueError(f"Model {model} does not support dual mode")

        if reference_images and not supports_feature(model, "reference_images"):
            raise ValueError(
                f"Model {model} does not support reference images"
            )

        # Handle video continuation
        continuation_frame_path = None
        native_extend_media_id = None
        
        if continuation_video_id and not skip_continuation_image and not start_image:
            source_video = await db.get_video(continuation_video_id)
            if source_video and source_video.status == VideoStatus.COMPLETED.value:
                aspect_ratio = source_video.aspect_ratio
                # Prioritize native extension using mediaGenerationId (saved as video_url)
                if source_video.video_url:
                    native_extend_media_id = source_video.video_url
                    mode = VideoMode.EXTEND
                    google_model_key = resolve_model(model, mode.value, aspect_ratio)
                    if not google_model_key:
                        logger.warning(
                            f"Model {model} doesn't support EXTEND for continuation, falling back to I2V"
                        )
                        mode = VideoMode.T2V
                        native_extend_media_id = None
                        
                # Fallback to legacy last-frame extraction if native extend is not supported or missing ID
                if not native_extend_media_id and source_video.download_url:
                    logger.info(f"Extracting last frame for continuation from {continuation_video_id}")
                    continuation_frame_path = await self._extract_last_frame(
                        source_video.download_url,
                        video_id
                    )
                    if continuation_frame_path:
                        mode = VideoMode.I2V
                        google_model_key = resolve_model(model, mode.value, aspect_ratio)
                        if not google_model_key:
                            logger.warning(
                                f"Model {model} doesn't support I2V for continuation, falling back to T2V"
                            )
                            mode = VideoMode.T2V
                            continuation_frame_path = None
                    else:
                        logger.warning("Failed to extract last frame, continuing without")
            else:
                logger.warning(f"Source video {continuation_video_id} not found or not completed")

        elif insert_video_id and not start_image:
            source_video = await db.get_video(insert_video_id)
            if source_video and source_video.status == VideoStatus.COMPLETED.value:
                aspect_ratio = source_video.aspect_ratio
                if source_video.video_url:
                    native_insert_media_id = source_video.video_url
                    mode = VideoMode.INSERT
                    google_model_key = resolve_model(model, mode.value, aspect_ratio)
                    if not google_model_key:
                        raise ValueError(f"Model {model} doesn't support INSERT mode")
                    # Store insert ID as continuation ID in DB to avoid schema changes
                    continuation_video_id = insert_video_id
                else:
                    raise ValueError(f"Source video {insert_video_id} has no Google mediaGenerationId required for insertion.")
            else:
                raise ValueError("Source video not ready for insertion")

        elif remove_video_id and not start_image:
            source_video = await db.get_video(remove_video_id)
            if source_video and source_video.status == VideoStatus.COMPLETED.value:
                aspect_ratio = source_video.aspect_ratio
                if source_video.video_url:
                    native_remove_media_id = source_video.video_url
                    mode = VideoMode.REMOVE
                    google_model_key = resolve_model(model, mode.value, aspect_ratio)
                    if not google_model_key:
                        raise ValueError(f"Model {model} doesn't support REMOVE mode")
                    # Store remove ID as continuation ID in DB to avoid schema changes
                    continuation_video_id = remove_video_id
                else:
                    raise ValueError(f"Source video {remove_video_id} has no Google mediaGenerationId required for removal.")
            else:
                raise ValueError("Source video not ready for removal")

        elif reshoot_video_id and not start_image:
            source_video = await db.get_video(reshoot_video_id)
            if source_video and source_video.status == VideoStatus.COMPLETED.value:
                aspect_ratio = source_video.aspect_ratio
                if source_video.video_url:
                    native_reshoot_media_id = source_video.video_url
                    mode = VideoMode.RESHOOT
                    google_model_key = resolve_model(model, mode.value, aspect_ratio)
                    if not google_model_key:
                        raise ValueError(f"Model {model} doesn't support RESHOOT mode")
                    # Store reshoot ID as continuation ID in DB, and store motion in prompt
                    continuation_video_id = reshoot_video_id
                    prompt = reshoot_motion
                else:
                    raise ValueError(f"Source video {reshoot_video_id} has no Google mediaGenerationId required for reshoot.")
            else:
                raise ValueError("Source video not ready for reshoot")

        # Save images to disk
        start_image_path = None
        end_image_path = None
        reference_images_paths = None

        if continuation_frame_path:
            start_image_path = continuation_frame_path
        elif start_image:
            start_image_path = await self._save_image(
                video_id, start_image, "start.jpg"
            )

        if end_image:
            end_image_path = await self._save_image(
                video_id, end_image, "end.jpg"
            )

        if reference_images:
            reference_images_paths = []
            for i, ref_img in enumerate(reference_images):
                path = await self._save_image(
                    video_id, ref_img, f"ref_{i}.jpg"
                )
                reference_images_paths.append(path)

        mask_path = None
        mask_coordinates_json = None
        if mask:
            mask_path = await self._save_image(video_id, mask, "mask.png")
        elif mask_coordinates:
            mask_coordinates_json = mask_coordinates.model_dump_json()

        # Create database record
        await db.create_video(
            video_id=video_id,
            prompt=prompt,
            detected_language=detected_language,
            model=model,
            google_model_key=google_model_key,
            mode=mode.value,
            aspect_ratio=aspect_ratio,
            start_image_path=start_image_path,
            end_image_path=end_image_path,
            reference_images_paths=reference_images_paths,
            continuation_video_id=continuation_video_id,
            seed=seed,
            requested_resolution=requested_resolution,
            mask_path=mask_path,
            mask_coordinates=mask_coordinates_json,
            duration=duration,
            requested_duration=duration,
        )

        logger.info(
            f"Video created: {video_id} (model={model}, mode={mode.value}, seed={seed}, resolution={requested_resolution or 'default'})"
        )

        # Start generation in background with concurrency limit
        asyncio.create_task(self._process_video_limited(video_id))

        return video_id

    async def _process_video_limited(self, video_id: str) -> None:
        """Process video with concurrency limit."""
        async with self._semaphore:
            while True:
                # Always grab the oldest pending video from the database
                oldest_list = await db.get_videos(limit=1, status="pending", sort_asc=True)
                if not oldest_list:
                    logger.info("No pending videos found in DB. Worker slot exiting.")
                    break

                target_video_id = oldest_list[0].video_id
                logger.info(
                    f"Worker acquired semaphore slot (originally spawned for {video_id}). "
                    f"Redirecting to oldest pending video {target_video_id} "
                    f"(created_at={oldest_list[0].created_at})"
                )

                # Pre-flight check: ensure the video is still pending
                target_video = await db.get_video(target_video_id)
                if not target_video or target_video.status != VideoStatus.PENDING.value:
                    # Stale or already claimed, immediately try next oldest
                    continue

                # Process this video
                await self._process_video(target_video_id)

                # Check if it was successfully claimed (status is no longer pending)
                video_after = await db.get_video(target_video_id)
                if not video_after or video_after.status != VideoStatus.PENDING.value:
                    # Successfully claimed/started or went to terminal status
                    break
                else:
                    logger.info(
                        f"Video {target_video_id} remains PENDING after attempt. "
                        f"Worker slot retrying next oldest..."
                    )
                    await asyncio.sleep(0.5)

    async def _process_video(self, video_id: str) -> None:
        """Process video generation with retry logic and account rotation."""
        video = await db.get_video(video_id)
        if not video:
            logger.error(f"Video {video_id} not found")
            return

        # Bail out immediately if the watchdog (fail_stale_pending_videos) or
        # an explicit cancel already moved this row out of PENDING. Without
        # this check the worker still does ~minutes of prep work (semaphore
        # wait, account selection, browser tab acquisition) before
        # try_claim_video_for_generation finally returns False — to the user
        # this looks like the failed/rate-limited video is "still
        # generating" because a worker is still spinning on it and holding
        # a concurrency slot.
        if video.status != VideoStatus.PENDING.value:
            logger.info(
                f"Video {video_id} is not PENDING (status={video.status}) — "
                f"skipping _process_video (watchdog or cancel already owns it)"
            )
            return

        # ---- Provider routing ------------------------------------------------
        # When provider_mode=browser, route every browser-supported mode (T2V,
        # I2V, I2V_DUAL, R2V, EXTEND, UPSCALE) through the browser2api (Flow
        # UI) path. Modes the browser client doesn't support (INSERT, REMOVE,
        # RESHOOT) fall through to the HTTP (veo) path below. Browser-path
        # failures stay failed — there is no HTTP fallback by design.
        if app_config.provider_mode == "browser":
            from .providers.browser_provider import (
                BROWSER_SUPPORTED_MODES,
                get_browser_provider,
            )
            if video.mode in BROWSER_SUPPORTED_MODES:
                logger.info(
                    f"Routing video {video_id} ({video.mode}) via BrowserProvider "
                    f"(provider_mode=browser)"
                )
                await get_browser_provider().process_video(video_id)
                return
        if app_config.provider_mode == "gflow":
            from .providers.gflow_provider import get_gflow_provider

            logger.info(
                "Routing video %s (%s) via GFlowProvider "
                "(provider_mode=gflow)",
                video_id,
                video.mode,
            )
            await get_gflow_provider().process_video(video_id)
            return
        # ---------------------------------------------------------------------

        MAX_GLOBAL_RETRIES = 10
        MAX_ACCOUNT_RETRIES = 15

        global_attempt = 0
        account = None
        # Carry over any accounts we already excluded for this video on
        # previous invisible retries (e.g. UNUSUAL_ACTIVITY rotation).
        tried_account_ids: set[int] = set(self._excluded_accounts.get(video_id, set()))
        # Whether we've already moved the video PENDING→GENERATING in this
        # _process_video call. Same reasoning as browser_provider —
        # subsequent retries shouldn't re-claim PENDING.
        claimed_in_this_call = False
        # Account currently reserved for this worker (HTTP path).
        # Released in the ``finally`` below regardless of outcome.
        _reserved_busy_id: Optional[int] = None

        # Set initial progress message
        await db.update_video(video_id, progress_message="Starting generation...")

        # Register a task-done callback that releases whatever account we
        # last reserved for THIS worker. We stash the reserved id on the
        # current asyncio.Task so a sync callback (which can't await) can
        # see and clear it after the coroutine finishes — success, failure
        # or cancel.
        _cur_task = asyncio.current_task()
        if _cur_task is not None:
            _cur_task._veoapi_busy_acc_id = None  # type: ignore[attr-defined]

            def _release_busy_on_done(t: asyncio.Task, _self=self) -> None:
                acc_id = getattr(t, "_veoapi_busy_acc_id", None)
                if acc_id is None:
                    return
                # Lock-free discard is safe (set.discard is atomic in CPython
                # and we only ever clear our own id here).
                _self._busy_accounts.discard(acc_id)

            _cur_task.add_done_callback(_release_busy_on_done)

        while global_attempt < MAX_GLOBAL_RETRIES:
            try:
                # 1. Select Account
                account = None
                if video.continuation_video_id:
                    src_vid = await db.get_video(video.continuation_video_id)
                    if src_vid and src_vid.account_id:
                        account = await db.get_account(src_vid.account_id)
                        if account and not account.is_active:
                            logger.warning(f"Source account {account.email} inactive, attempting anyway for continuation...")

                if not account:
                    # Build exclude set: per-call tried IDs ∪ accounts
                    # already reserved by OTHER concurrent _process_video
                    # workers. This is what prevents two workers from
                    # racing to the same account when both observe the
                    # same DB ``last_used_at`` ordering before either
                    # has bumped it. Without this, the second worker
                    # blocks inside the per-account semaphore in
                    # browser_pool / on the HTTP rate-limit, holds its
                    # outer worker slot, and starves new pending videos.
                    async with self._busy_lock:
                        other_busy = self._busy_accounts - ({_reserved_busy_id} if _reserved_busy_id else set())
                    combined_exclude = tried_account_ids | other_busy
                    # Try shift pool first, then expand to full pool if shift is exhausted
                    account = await load_balancer.get_account(
                        model_key=video.google_model_key,
                        exclude_ids=combined_exclude,
                    )
                    if not account and combined_exclude:
                        logger.info(
                            f"Shift pool exhausted for video {video_id}, expanding to full account pool"
                        )
                        account = await load_balancer.get_account(
                            model_key=video.google_model_key,
                            exclude_ids=combined_exclude,
                            ignore_shift=True,
                        )

                if not account:
                    logger.warning(f"No accounts available for video {video_id}, attempt {global_attempt}")
                    await db.update_video(video_id, progress_message="Waiting for available account...")
                    await asyncio.sleep(5)
                    global_attempt += 1
                    continue

                # Reserve this account so concurrent workers see it as
                # busy. If we rotated from a previous account in this
                # same _process_video call, release the old reservation
                # first. The done-callback registered above also clears
                # whatever the last reservation was on task exit.
                async with self._busy_lock:
                    if _reserved_busy_id is not None and _reserved_busy_id != account.id:
                        self._busy_accounts.discard(_reserved_busy_id)
                    self._busy_accounts.add(account.id)
                    _reserved_busy_id = account.id
                if _cur_task is not None:
                    _cur_task._veoapi_busy_acc_id = account.id  # type: ignore[attr-defined]

                # 2. Try Generation Loop (Per Account)
                _rl_retry_count = 0  # rate-limit retries before proxy rotation
                for account_attempt in range(MAX_ACCOUNT_RETRIES):
                    try:
                        if not await self.token_manager.refresh_account_token(account.id):
                            logger.warning(f"Token refresh failed for {account.email}, switching account")
                            break

                        # Atomic claim only on the FIRST attempt of this
                        # _process_video call. Subsequent rotations within
                        # the same call are GENERATING-already → just
                        # retarget the row. Re-claiming would spuriously
                        # think the video was killed externally.
                        if not claimed_in_this_call:
                            claimed = await db.try_claim_video_for_generation(
                                video_id,
                                account.id,
                                progress_message="Generating...",
                            )
                            if not claimed:
                                cur = await db.get_video(video_id)
                                cur_status = cur.status if cur else "<deleted>"
                                logger.info(
                                    f"Video {video_id} no longer PENDING "
                                    f"(status={cur_status}) — aborting before HTTP generation"
                                )
                                return
                            claimed_in_this_call = True
                        else:
                            await db.update_video(
                                video_id,
                                account_id=account.id,
                                progress_message="Generating...",
                            )

                        account = await db.get_account(account.id)

                        prompt_for_api = video.prompt

                        # Map aspect_ratio to Google format for image upload
                        video_aspect = (
                            "VIDEO_ASPECT_RATIO_PORTRAIT"
                            if video.aspect_ratio == "9:16"
                            else "VIDEO_ASPECT_RATIO_LANDSCAPE"
                        )

                        # Get account-specific VeoClient (with account's proxy)
                        account_client = self.token_manager.get_veo_client_for_account(account)

                        # Use browser captcha if requested for this video
                        # Always use browser captcha (fallback to capsolver in veo_client)
                        account_client.browser_captcha = get_browser_captcha()
                        account_client.proxy_pool = self.token_manager.get_active_proxy_pool()

                        # Proxy for generate_video POST:
                        # - residential accounts: use residential session (request_proxy=None)
                        # - others: rotate datacenter pool per request (residential
                        #   pool entries are reserved for explicit per-account
                        #   assignment, never used for ad-hoc rotation here).
                        if account.residential_proxy:
                            request_proxy = None
                            logger.debug(f"Using residential proxy for generation: {account.email}")
                        else:
                            request_proxy = await get_proxy_pool().get_next_datacenter()
                            if request_proxy:
                                logger.debug(f"Using pool datacenter proxy for generation: {request_proxy}")

                        # --- Circuit breaker: swap relaxed -> fast if disabled ---
                        current_model_key = video.google_model_key
                        fallback_key = RELAXED_TO_FAST_FALLBACK.get(current_model_key)
                        if fallback_key:
                            now = time.time()
                            if self._relaxed_force_disabled or account.relaxed_disabled:
                                # Manual override: globally or per-account
                                reason = "globally" if self._relaxed_force_disabled else f"for {account.email}"
                                logger.info(f"Relaxed model disabled {reason}, using fast: {fallback_key}")
                                await db.update_video(video_id, google_model_key=fallback_key)
                                video = await db.get_video(video_id)
                            elif self._relaxed_disabled_until > now:
                                # Relaxed is disabled, use fast
                                logger.info(
                                    f"Relaxed model disabled (cooldown {int(self._relaxed_disabled_until - now)}s left), using fast: {fallback_key}"
                                )
                                await db.update_video(video_id, google_model_key=fallback_key)
                                video = await db.get_video(video_id)
                            elif self._relaxed_disabled_until > 0:
                                # Cooldown just expired, entering probe mode
                                if self._relaxed_probe_count == 0:
                                    logger.info("Relaxed model cooldown expired, starting probe phase")
                                self._relaxed_probe_count += 1

                        # Upload images (pass aspect_ratio so Google gets correct format)
                        start_image_uri = None
                        end_image_uri = None
                        reference_image_uris = None
                        uploaded_media_ids = []  # Track for cleanup

                        # Always use the account's session proxy for uploads so that
                        # Google sees a consistent IP across the entire request lifecycle
                        # (auth → upload → generate → poll). Mixing residential auth with
                        # datacenter uploads was a detectable anomaly that triggered blocks.
                        _upload_proxy = None

                        if video.start_image_path:
                            image_bytes = await self._load_image(video.start_image_path)
                            start_image_uri = await account_client.upload_image(
                                account.at, image_bytes, video_aspect,
                                request_proxy=_upload_proxy,
                            )
                            uploaded_media_ids.append(start_image_uri)

                        if video.end_image_path:
                            image_bytes = await self._load_image(video.end_image_path)
                            end_image_uri = await account_client.upload_image(
                                account.at, image_bytes, video_aspect,
                                request_proxy=_upload_proxy,
                            )
                            uploaded_media_ids.append(end_image_uri)

                        if video.reference_images_paths:
                            reference_image_uris = []
                            paths = json.loads(video.reference_images_paths)
                            for path in paths:
                                image_bytes = await self._load_image(path)
                                uri = await account_client.upload_image(
                                    account.at, image_bytes, video_aspect,
                                    request_proxy=_upload_proxy,
                                )
                                reference_image_uris.append(uri)
                                uploaded_media_ids.append(uri)

                        # Start Generation
                        native_extend_id = None
                        native_insert_id = None
                        native_remove_id = None
                        native_reshoot_id = None
                        reshoot_motion = None
                        if video.continuation_video_id:
                            if video.mode == VideoMode.EXTEND.value:
                                src_vid = await db.get_video(video.continuation_video_id)
                                if src_vid:
                                    native_extend_id = src_vid.video_url
                            elif video.mode == VideoMode.INSERT.value:
                                src_vid = await db.get_video(video.continuation_video_id)
                                if src_vid:
                                    native_insert_id = src_vid.task_id
                            elif video.mode == VideoMode.REMOVE.value:
                                src_vid = await db.get_video(video.continuation_video_id)
                                if src_vid:
                                    native_remove_id = src_vid.task_id
                            elif video.mode == VideoMode.RESHOOT.value:
                                src_vid = await db.get_video(video.continuation_video_id)
                                if src_vid:
                                    native_reshoot_id = src_vid.video_url
                                    reshoot_motion = video.prompt
                                
                        insertion_mask_base64 = None
                        if video.mask_path:
                            try:
                                mask_bytes = await self._load_image(video.mask_path)
                                insertion_mask_base64 = base64.b64encode(mask_bytes).decode('utf-8')
                            except Exception as e:
                                logger.error(f"Failed to load mask image: {e}")
                        elif video.mask_coordinates and src_vid and src_vid.download_url:
                            # Generate mask matching source video resolution
                            try:
                                import subprocess
                                import numpy as np
                                import cv2
                                
                                # Download source video just to check dimensions
                                temp_source_path = TMP_DIR / f"{video_id}_mask_src.mp4"
                                src_video_bytes = await storage.download(f"videos/{src_vid.download_url}")
                                with open(temp_source_path, "wb") as f:
                                    f.write(src_video_bytes)
                                
                                # Get width and height via ffprobe
                                probe_cmd = [
                                    "ffprobe", "-v", "error",
                                    "-select_streams", "v:0",
                                    "-show_entries", "stream=width,height",
                                    "-of", "csv=s=x:p=0",
                                    str(temp_source_path)
                                ]
                                result = await asyncio.to_thread(
                                    subprocess.run, probe_cmd, capture_output=True, text=True,
                                    timeout=_FFMPEG_TIMEOUT,
                                )
                                temp_source_path.unlink(missing_ok=True)
                                
                                if result.returncode == 0:
                                    w_str, h_str = result.stdout.strip().split('x')
                                    w, h = int(w_str), int(h_str)
                                    
                                    # Create 3-channel (RGB) mask (Google expects 3 channels)
                                    mask_img = np.zeros((h, w, 3), dtype=np.uint8)
                                    coords = json.loads(video.mask_coordinates)
                                    cx, cy = coords["x"], coords["y"]
                                    c_w, c_h = coords["width"], coords["height"]
                                    cv2.rectangle(mask_img, (cx, cy), (cx + c_w, cy + c_h), (255, 255, 255), -1)
                                    
                                    _, buffer = cv2.imencode('.png', mask_img)
                                    b64_mask = base64.b64encode(buffer).decode('utf-8')
                                    
                                    # Save to file
                                    video.mask_path = await self._save_image(video_id, b64_mask, "mask.png")
                                    await db.update_video(video_id, mask_path=video.mask_path)
                                    
                                    insertion_mask_base64 = b64_mask
                                else:
                                    logger.error(f"Failed to probe source video {src_vid.video_id}: {result.stderr}")
                            except Exception as e:
                                logger.error(f"Failed to generate mask in _process_video: {e}")

                        # Anti-abuse inter-request delay: enforce a minimum gap
                        # between successive generate_video calls on the same
                        # account. Caps rapid-fire traffic which is a strong
                        # bot-detection signal for Google's abuse heuristics.
                        _now = time.time()
                        _last = self._account_last_gen.get(account.id, 0.0)
                        _gap = _now - _last
                        _min_gap = 2.0
                        if _gap < _min_gap:
                            await asyncio.sleep(_min_gap - _gap + random.uniform(0.5, 3.0))
                        self._account_last_gen[account.id] = time.time()

                        result = await account_client.generate_video(
                            at=account.at,
                            st=account.st,
                            project_id=account.project_id or None,
                            prompt=prompt_for_api,
                            model_key=video.google_model_key,
                            aspect_ratio=video.aspect_ratio,
                            start_image_uri=start_image_uri,
                            end_image_uri=end_image_uri,
                            reference_image_uris=reference_image_uris,
                            continuation_video_id=video.continuation_video_id,
                            seed=video.seed,
                            user_paygate_tier=account.user_paygate_tier or "PAYGATE_TIER_TWO",
                            native_extend_media_id=native_extend_id,
                            native_insert_media_id=native_insert_id,
                            native_remove_media_id=native_remove_id,
                            native_reshoot_media_id=native_reshoot_id,
                            reshoot_motion=reshoot_motion,
                            insertion_mask_base64=insertion_mask_base64,
                            account_email=account.email,
                            request_proxy=request_proxy,
                        )

                        # SUCCESS!
                        # Track generation start time for circuit breaker
                        self._gen_start_times[video_id] = time.time()

                        await db.update_video(
                            video_id,
                            task_id=result.task_id,
                            scene_id=result.scene_id,
                            seed=result.seed,
                            progress_message="Generating...",
                        )

                        # Save project_id to account for future use (upscaling, etc.)
                        if result.project_id and result.project_id != account.project_id:
                            await db.update_account(account.id, project_id=result.project_id)

                        logger.info(f"Video {video_id} started successfully on {account.email}")

                        # Save uploaded media for cleanup after video completes
                        if uploaded_media_ids:
                            if video_id not in self._pending_media_cleanup:
                                self._pending_media_cleanup[video_id] = []
                            self._pending_media_cleanup[video_id].append({
                                "st": account.st,
                                "media_ids": list(uploaded_media_ids),
                                "account_email": account.email,
                            })

                        return

                    except VeoClientError as e:
                        # Save uploaded media for cleanup even on error
                        if uploaded_media_ids:
                            if video_id not in self._pending_media_cleanup:
                                self._pending_media_cleanup[video_id] = []
                            self._pending_media_cleanup[video_id].append({
                                "st": account.st,
                                "media_ids": list(uploaded_media_ids),
                                "account_email": account.email,
                            })

                        logger.warning(
                            f"Attempt {account_attempt+1}/{MAX_ACCOUNT_RETRIES} on {account.email} failed: {e}"
                        )

                        if e.error_code == "INVALID_ARGUMENT":
                            # Try fallback from relaxed to fast model first
                            fallback_key = RELAXED_TO_FAST_FALLBACK.get(video.google_model_key)
                            if fallback_key and video.google_model_key != fallback_key:
                                logger.info(
                                    f"INVALID_ARGUMENT on relaxed model, falling back to fast: {fallback_key}"
                                )
                                await db.update_video(video_id, google_model_key=fallback_key)
                                video = await db.get_video(video_id)
                                continue  # Retry on same account with fast model

                            # If it still fails even on the fast model (or there's no fallback),
                            # it's definitely a bad user request (prompt, aspect ratio, image format).
                            # Do not bounce between accounts causing false bans, just fail it immediately.
                            raise e

                        if e.error_code == "INVALID_PROMPT":
                             raise e

                        # Throttle = account overloaded, track separately
                        if e.error_code == "PUBLIC_ERROR_USER_REQUESTS_THROTTLED":
                            # Try fallback from relaxed to fast model before switching account
                            fallback_key = RELAXED_TO_FAST_FALLBACK.get(video.google_model_key)
                            if fallback_key and video.google_model_key != fallback_key:
                                logger.info(
                                    f"Relaxed model throttled, falling back to fast: {fallback_key}"
                                )
                                await db.update_video(video_id, google_model_key=fallback_key)
                                video = await db.get_video(video_id)
                                continue  # Retry on same account with fast model
                            await self.token_manager.record_throttle(account.id)
                            break  # Try another account

                        # UNUSUAL_ACTIVITY = Google's anti-abuse flagging this
                        # account, not the prompt. Try another account; if the
                        # same account hits this 5 times in a row send it to
                        # rest for 30 minutes.
                        if e.error_code == "PUBLIC_ERROR_UNUSUAL_ACTIVITY":
                            logger.warning(
                                f"UNUSUAL_ACTIVITY on {account.email} — switching account"
                            )
                            await self.token_manager.record_unusual_activity(account.id)
                            break  # Try another account

                        # Other PUBLIC_ERROR_* = content/policy rejection from Google.
                        # Not an account issue — fail immediately, don't retry.
                        if e.error_code and e.error_code.startswith("PUBLIC_ERROR"):
                            raise e

                        if e.error_code == "RATE_LIMITED":
                            # Retry up to 10 times on same proxy before rotating
                            if _rl_retry_count < 10:
                                _rl_retry_count += 1
                                logger.warning(
                                    f"RATE_LIMITED on {account.email} — retry {_rl_retry_count}/10 with fresh captcha"
                                )
                                await asyncio.sleep(1)
                                continue
                            # All retries exhausted — mark account rate-limited
                            await self.token_manager.record_error(account.id, e.error_code)
                            break

                        if e.error_code in ("UNAUTHORIZED", "TOKEN_EXCHANGE_ERROR"):
                            await self.token_manager.record_error(account.id, e.error_code)
                            break

                        if e.error_code in ("FORBIDDEN", "CAPTCHA_FAILED"):
                            # veo_client already exhausted 5 browser + 3 capsolver retries internally,
                            # no point retrying on same account — switch to another
                            await db.update_account_used(account.id)
                            break

                        if e.error_code in ("TIMEOUT", "GENERATION_ERROR", "IMAGE_UPLOAD_ERROR"):
                             # Bump last_used_at so load balancer deprioritizes this account
                             # for concurrent requests while we're retrying on it
                             await db.update_account_used(account.id)
                             await asyncio.sleep(2 * (account_attempt + 1))
                             continue

                        break

                if account:
                    tried_account_ids.add(account.id)
                global_attempt += 1
                await asyncio.sleep(2)

            except VeoClientError as e:
                logger.error(f"Fatal validation error for video {video_id}: {e}")
                await db.update_video(
                    video_id,
                    status=VideoStatus.FAILED.value,
                    error_code=e.error_code or "VALIDATION_ERROR",
                    error_message=str(e),
                    progress_message=None,
                )
                await self._cleanup_uploaded_media(video_id)
                return

            except Exception as e:
                logger.exception(f"Unexpected error in retry loop for {video_id}: {e}")
                global_attempt += 1
                await asyncio.sleep(5)

        # Failed all retries
        logger.error(f"Video {video_id} failed after {MAX_GLOBAL_RETRIES} global attempts")
        await db.update_video(
            video_id,
            status=VideoStatus.FAILED.value,
            error_code="MAX_RETRIES_EXCEEDED",
            error_message="Failed to generate video after multiple account attempts",
            progress_message=None,
        )
        await self._cleanup_uploaded_media(video_id)

        # Track consecutive MAX_RETRIES_EXCEEDED and trigger gradual reauth if threshold hit
        self._max_retries_exceeded_count += 1
        now = time.time()
        if (
            self._max_retries_exceeded_count >= self._REAUTH_TRIGGER_THRESHOLD
            and now - self._reauth_triggered_at > self._REAUTH_TRIGGER_COOLDOWN
        ):
            self._max_retries_exceeded_count = 0
            self._reauth_triggered_at = now
            logger.warning(
                f"MAX_RETRIES_EXCEEDED hit {self._REAUTH_TRIGGER_THRESHOLD} times — "
                f"marking all active accounts as needs_reauth for gradual reauth"
            )
            asyncio.create_task(self._trigger_gradual_reauth())

    async def _trigger_gradual_reauth(self) -> None:
        """Mark all active accounts as needs_reauth so immediate_reauth_monitor picks them up."""
        try:
            accounts = await db.get_active_accounts()
            for acc in accounts:
                await db.update_account(acc.id, status="needs_reauth")
                logger.info(f"Marked {acc.email} as needs_reauth (MAX_RETRIES_EXCEEDED trigger)")
                await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error in _trigger_gradual_reauth: {e}")

    async def poll_video_status(self, video_id: str) -> None:
        """Poll and update video status."""
        video = await db.get_video(video_id)
        if not video or not video.task_id or not video.account_id:
            return

        account = await db.get_account(video.account_id)
        if not account or not account.at:
            return

        # Use account-specific VeoClient (with account's proxy)
        account_client = self.token_manager.get_veo_client_for_account(account)

        try:
            # Residential accounts poll via their session proxy (consistent IP).
            # Non-residential accounts rotate through the datacenter pool to
            # distribute load. On network errors, rotate up to 3 times then
            # fall back to direct connection.
            result = None
            _poll_use_proxy = not bool(account.residential_proxy)
            _poll_max_attempts = 4  # 3 proxy rotations + 1 direct fallback
            for _poll_attempt in range(_poll_max_attempts):
                _is_last = _poll_attempt == _poll_max_attempts - 1
                if _poll_use_proxy and not _is_last:
                    _poll_proxy = await get_proxy_pool().get_next_datacenter()
                else:
                    _poll_proxy = None  # direct on last attempt
                try:
                    result = await account_client.get_video_status(
                        account.at, video.task_id, request_proxy=_poll_proxy
                    )
                    break
                except VeoClientError as _pe:
                    _is_network = any(k in str(_pe) for k in ("curl:", "Connection reset", "Failed to perform", "PROXY", "Recv failure"))
                    if _is_network and not _is_last:
                        _proxy_hint = f"{_poll_proxy[:30]}..." if _poll_proxy else "direct"
                        logger.warning(
                            f"Poll {video_id}: network error via proxy {_proxy_hint}, "
                            f"rotating (attempt {_poll_attempt + 1}/{_poll_max_attempts}): {_pe}"
                        )
                        continue
                    raise  # non-network error or last attempt — propagate
            if result is None:
                return

            update_data = {
                "progress": result.progress,
            }

            # Update progress message based on progress
            is_upscaling = video.video_type == "upscale"
            if is_upscaling:
                update_data["progress_message"] = f"Upscaling to {video.requested_resolution or '1080p'}..."
            elif result.progress < 80:
                update_data["progress_message"] = "Generating..."
            else:
                update_data["progress_message"] = "Finalizing..."

            if result.status == "completed":
                await self.token_manager.record_success(account.id)
                self._max_retries_exceeded_count = 0  # Reset on success

                # --- Circuit breaker: track relaxed model generation time ---
                start_time = self._gen_start_times.pop(video_id, None)
                if start_time and video.google_model_key:
                    gen_duration = time.time() - start_time
                    is_relaxed = video.google_model_key in RELAXED_TO_FAST_FALLBACK
                    if is_relaxed:
                        if gen_duration > self._RELAXED_SLOW_THRESHOLD:
                            self._relaxed_slow_streak += 1
                            logger.warning(
                                f"Relaxed model slow: {gen_duration:.0f}s (streak {self._relaxed_slow_streak}/{self._RELAXED_STREAK_LIMIT})"
                            )
                            if self._relaxed_slow_streak >= self._RELAXED_STREAK_LIMIT:
                                self._relaxed_disabled_until = time.time() + self._RELAXED_COOLDOWN
                                self._relaxed_slow_streak = 0
                                self._relaxed_probe_count = 0
                                logger.warning(
                                    f"Relaxed model disabled for {self._RELAXED_COOLDOWN}s due to slow generations"
                                )
                        else:
                            # Fast generation on relaxed — reset streak
                            if self._relaxed_slow_streak > 0:
                                logger.info(
                                    f"Relaxed model fast again ({gen_duration:.0f}s), resetting slow streak"
                                )
                            self._relaxed_slow_streak = 0
                            # If probing, reset probe state on success
                            if self._relaxed_probe_count > 0:
                                self._relaxed_disabled_until = 0
                                self._relaxed_probe_count = 0
                                logger.info("Relaxed model probe succeeded, re-enabled permanently")

                # Check if auto-upscale is requested
                if video.requested_resolution and video.video_type != "upscale":
                    logger.info(
                        f"Video {video_id} base generation done, starting auto-upscale to {video.requested_resolution}"
                    )
                    # Mark as upscale IMMEDIATELY to prevent re-triggering on next poll
                    await db.update_video(
                        video_id,
                        video_url=result.video_url,
                        cost=result.cost,
                        task_id=None,
                        video_type="upscale",
                        progress=80,
                        progress_message=f"Upscaling to {video.requested_resolution}...",
                    )
                    # Cleanup uploaded media from generation phase
                    asyncio.create_task(self._cleanup_uploaded_media(video_id))
                    # Start upscale in background
                    asyncio.create_task(
                        self._auto_upscale(video_id, video, result, account)
                    )
                    return

                # --- Extend mode: concatenate original + extension via Google API ---
                is_extend = (
                    video.mode in (VideoMode.EXTEND.value, "extend")
                    and video.continuation_video_id
                    and result.video_url  # need mediaGenerationId for concat
                )
                if is_extend:
                    logger.info(
                        f"Video {video_id} extend generation done, starting concatenation"
                    )
                    # Save the extension's video_url immediately so we don't lose it
                    await db.update_video(
                        video_id,
                        video_url=result.video_url,
                        cost=result.cost,
                        task_id=None,  # clear task_id to prevent re-polling
                        progress=90,
                        progress_message="Concatenating with original...",
                    )
                    asyncio.create_task(
                        self._concatenate_extend_video(
                            video_id, video, result, account
                        )
                    )
                    return

                # --- Normal completion (T2V, I2V, R2V, Insert, Reshoot, etc.) ---
                logger.info(f"Video {video_id} completed")
                self._auto_retries.pop(video_id, None)
                self._unusual_retries.pop(video_id, None)
                self._excluded_accounts.pop(video_id, None)
                await self._finalize_completed_video(video_id, result)
                return

            elif result.status == "failed":
                # --- Circuit breaker cleanup ---
                self._gen_start_times.pop(video_id, None)

                # --- Auto Retry for internal Google Errors ---
                is_lmroot_error = False
                err_code_str = str(result.error_code) if result.error_code else ""
                err_msg_str = str(result.error_message) if result.error_message else ""
                
                if err_code_str in ("13", "14", "INTERNAL", "UNAVAILABLE"):
                    is_lmroot_error = True
                elif "lmroot" in err_msg_str.lower() or "invalid response from gemini model" in err_msg_str.lower():
                    is_lmroot_error = True

                if is_lmroot_error:
                    retries = self._auto_retries.get(video_id, 0)
                    if retries < 5:
                        self._auto_retries[video_id] = retries + 1
                        logger.warning(
                            f"Video {video_id} failed with internal Google error "
                            f"(attempt {retries + 1}/5). Auto-retrying invisibly..."
                        )
                        await db.update_video(
                            video_id,
                            status=VideoStatus.PENDING.value,
                            progress_message=f"Google Internal Error. Auto-retrying ({retries+1}/5)...",
                            task_id=None,
                            error_code=None,
                            error_message=None,
                        )
                        # Remove from pending media cleanup so files aren't deleted before retry
                        self._pending_media_cleanup.pop(video_id, None)
                        # Restart processing
                        asyncio.create_task(self._process_video_limited(video_id))
                        return

                # If out of retries, change error to INVALID_ARGUMENT as requested
                if is_lmroot_error:
                    result.error_code = "INVALID_ARGUMENT"
                    result.error_message = "Google API internal parsing failure. Please tweak request/coordinates and try again."

                update_data.update(
                    {
                        "status": VideoStatus.FAILED.value,
                        "error_code": result.error_code,
                        "error_message": result.error_message,
                        "progress_message": None,
                    }
                )
                # Throttle errors — track for auto-disable
                if result.error_code == "PUBLIC_ERROR_USER_REQUESTS_THROTTLED":
                    await self.token_manager.record_throttle(account.id)
                # Anti-abuse flagging — switch to a different account on
                # invisible retry instead of failing the video. Google flags
                # the account, not the prompt; another account usually works.
                elif result.error_code == "PUBLIC_ERROR_UNUSUAL_ACTIVITY":
                    await self.token_manager.record_unusual_activity(account.id)
                    retries = self._unusual_retries.get(video_id, 0)
                    if retries < self._UNUSUAL_RETRY_LIMIT:
                        self._unusual_retries[video_id] = retries + 1
                        excluded = self._excluded_accounts.setdefault(video_id, set())
                        excluded.add(account.id)
                        logger.warning(
                            f"Video {video_id} hit UNUSUAL_ACTIVITY on {account.email} "
                            f"during polling — invisible retry "
                            f"{retries + 1}/{self._UNUSUAL_RETRY_LIMIT} on a different account "
                            f"(excluded so far: {sorted(excluded)})"
                        )
                        await db.update_video(
                            video_id,
                            status=VideoStatus.PENDING.value,
                            progress=0,
                            progress_message=f"Retrying on another account ({retries + 1}/{self._UNUSUAL_RETRY_LIMIT})...",
                            task_id=None,
                            error_code=None,
                            error_message=None,
                        )
                        # Don't delete uploaded media — the next attempt may
                        # need them (start/end/reference images).
                        self._pending_media_cleanup.pop(video_id, None)
                        asyncio.create_task(self._process_video_limited(video_id))
                        return
                    # Exhausted invisible retries — but we still must NOT
                    # surface UNUSUAL_ACTIVITY to the user. Re-queue PENDING
                    # with a long cool-down so flagged accounts get a chance
                    # to age out, and clear the excluded set so every account
                    # is eligible again on the next try.
                    logger.warning(
                        f"Video {video_id} exhausted {self._UNUSUAL_RETRY_LIMIT} "
                        f"UNUSUAL_ACTIVITY retries — re-queueing with cool-down "
                        f"(masking from user)"
                    )
                    self._unusual_retries.pop(video_id, None)
                    self._excluded_accounts.pop(video_id, None)
                    await db.update_video(
                        video_id,
                        status=VideoStatus.PENDING.value,
                        progress=0,
                        progress_message="Waiting for accounts to cool down...",
                        task_id=None,
                        account_id=None,
                        error_code=None,
                        error_message=None,
                    )
                    self._pending_media_cleanup.pop(video_id, None)

                    async def _retry_after_unusual_cooldown():
                        try:
                            # Under the 5-min pending SLA so this retry has
                            # a chance to actually run.
                            await asyncio.sleep(60.0)
                            cur = await db.get_video(video_id)
                            if cur and cur.status == VideoStatus.PENDING.value:
                                await self._process_video_limited(video_id)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # pragma: no cover
                            logger.error(
                                f"UNUSUAL_ACTIVITY cool-down retry failed "
                                f"for {video_id}: {exc}"
                            )

                    asyncio.create_task(_retry_after_unusual_cooldown())
                    return

                # PUBLIC_ERROR_* are content/policy errors from Google,
                # not account/service errors — don't penalise the account.
                is_public_error = (
                    result.error_code
                    and result.error_code.startswith("PUBLIC_ERROR")
                )
                if not is_public_error:
                    await self.token_manager.record_error(
                        account.id, result.error_code
                    )
                logger.warning(
                    f"Video {video_id} failed: {result.error_message}"
                )
                # Cleanup uploaded media on failure
                asyncio.create_task(self._cleanup_uploaded_media(video_id))

            await db.update_video(video_id, **update_data)

        except VeoClientError as e:
            logger.error(f"Failed to poll video {video_id}: {e}")

            if e.error_code == "UNAUTHORIZED":
                await self.token_manager.refresh_account_token(
                    account.id, force=True
                )

    async def retry_video(self, video_id: str) -> str:
        """Retry a failed video generation."""
        original = await db.get_video(video_id)
        if not original:
            raise ValueError(f"Video {video_id} not found")

        if original.status != VideoStatus.FAILED.value:
            raise ValueError("Can only retry failed videos")

        new_video_id = str(uuid.uuid4())

        new_start_path = None
        new_end_path = None
        new_ref_paths = None

        if original.start_image_path:
            try:
                data = await storage.download(original.start_image_path)
                new_key = f"images/{new_video_id}/start.jpg"
                await storage.upload(new_key, data, content_type="image/jpeg")
                new_start_path = new_key
            except FileNotFoundError:
                logger.warning(f"Start image not found for retry: {original.start_image_path}")

        if original.end_image_path:
            try:
                data = await storage.download(original.end_image_path)
                new_key = f"images/{new_video_id}/end.jpg"
                await storage.upload(new_key, data, content_type="image/jpeg")
                new_end_path = new_key
            except FileNotFoundError:
                logger.warning(f"End image not found for retry: {original.end_image_path}")

        if original.reference_images_paths:
            new_ref_paths = []
            paths = json.loads(original.reference_images_paths)
            for i, path in enumerate(paths):
                try:
                    data = await storage.download(path)
                    new_key = f"images/{new_video_id}/ref_{i}.jpg"
                    await storage.upload(new_key, data, content_type="image/jpeg")
                    new_ref_paths.append(new_key)
                except FileNotFoundError:
                    logger.warning(f"Reference image not found for retry: {path}")

        await db.create_video(
            video_id=new_video_id,
            prompt=original.prompt,
            detected_language=original.detected_language,
            model=original.model,
            google_model_key=original.google_model_key,
            mode=original.mode,
            aspect_ratio=original.aspect_ratio,
            start_image_path=new_start_path,
            end_image_path=new_end_path,
            reference_images_paths=new_ref_paths,
            continuation_video_id=original.continuation_video_id,
            seed=original.seed,
        )

        logger.info(f"Retrying video {video_id} as {new_video_id}")

        asyncio.create_task(self._process_video_limited(new_video_id))

        return new_video_id

    async def cleanup_video_images(self, video_id: str) -> None:
        """Remove images associated with a video."""
        deleted = await storage.delete_prefix(f"images/{video_id}")
        if deleted:
            logger.debug(f"Cleaned up {deleted} images for video {video_id}")

    async def upscale_video(
        self,
        video_id: str,
        resolution: str = "1080p",
    ) -> str:
        """Upscale a completed video to higher resolution."""
        video = await db.get_video(video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found")

        if video.status != VideoStatus.COMPLETED.value:
            raise ValueError("Can only upscale completed videos")

        if not video.video_url:
            raise ValueError("Original video has no mediaGenerationId (video_url). Cannot upscale.")

        resolution = resolution.lower()
        if resolution not in ("4k", "1080p"):
            raise ValueError(f"Invalid resolution: {resolution}. Use '4k' or '1080p'")

        new_video_id = str(uuid.uuid4())

        await db.create_video(
            video_id=new_video_id,
            prompt=f"[UPSCALE {resolution}] {video.prompt}",
            detected_language=video.detected_language,
            model=video.model,
            google_model_key=f"veo_3_1_upsampler_{resolution}",
            mode="upscale",
            aspect_ratio=video.aspect_ratio,
            continuation_video_id=video_id,
            video_type="upscale",
            original_video_id=video_id,
            # Set requested_resolution so BrowserProvider._run_standalone_upscale
            # can pick the right target without parsing google_model_key.
            requested_resolution=resolution,
        )

        logger.info(
            f"Upscale video created: {new_video_id} (source={video_id}, res={resolution})"
        )

        asyncio.create_task(self._process_upscale(new_video_id, video, resolution))

        return new_video_id

    async def _process_upscale(
        self,
        video_id: str,
        original_video,
        resolution: str,
    ) -> None:
        """Process video upscaling in background.

        If the source video was generated via the browser provider, route
        the upscale through BrowserProvider too — its ``mediaGenerationId``
        is a Flow-internal URL, not an HTTP-addressable media id, so the
        HTTP upscale path fails with NOT_FOUND.
        """
        # Branch: browser-generated source → browser upscale.
        if getattr(original_video, "provider", "veo") == "browser":
            try:
                from .providers.browser_provider import get_browser_provider
                logger.info(
                    f"Routing upscale {video_id} via BrowserProvider "
                    f"(source {original_video.video_id} was browser-generated)"
                )
                await get_browser_provider().process_video(video_id)
            except Exception as e:
                # Reuse the provider's classifier so the client sees the
                # same codes (GENERATION_FAILED / RATE_LIMITED / ...) it
                # already handles — not a novel ``browser_upscale_failed``.
                from .providers.browser_provider import _classify_unknown_error
                code, msg = _classify_unknown_error(e)
                logger.exception(
                    f"[upscale] BrowserProvider route failed for {video_id}: "
                    f"{e} (mapped to {code})"
                )
                await db.update_video(
                    video_id,
                    status=VideoStatus.FAILED.value,
                    error_code=code,
                    error_message=msg,
                )
            return

        account = None

        try:
            # Use the SAME account that generated the original video.
            # The mediaGenerationId is tied to the account/project that created it.
            if original_video.account_id:
                account = await db.get_account(original_video.account_id)

            # Fallback to load balancer if original account is unavailable
            if not account or not account.is_active:
                account = await load_balancer.get_account()

            if not account:
                await db.update_video(
                    video_id,
                    status=VideoStatus.FAILED.value,
                    error_code="NO_ACCOUNTS",
                    error_message="No available accounts",
                )
                return

            if not await self.token_manager.refresh_account_token(account.id):
                await db.update_video(
                    video_id,
                    status=VideoStatus.FAILED.value,
                    error_code="TOKEN_REFRESH_FAILED",
                    error_message="Failed to refresh account token",
                )
                return

            account = await db.get_account(account.id)

            # Get account-specific VeoClient (with account's proxy)
            account_client = self.token_manager.get_veo_client_for_account(account)

            # Always use browser captcha (fallback to capsolver in veo_client)
            account_client.browser_captcha = get_browser_captcha()

            # Ensure account has a project_id (needed for reCAPTCHA)
            if not account.project_id:
                project_id = await account_client.create_project(account.st)
                if project_id:
                    await db.update_account(account.id, project_id=project_id)
                    account = await db.get_account(account.id)

            # Atomic claim — same race guard as base generation.
            claimed = await db.try_claim_video_for_generation(
                video_id,
                account.id,
                progress_message="Starting upscale...",
            )
            if not claimed:
                cur = await db.get_video(video_id)
                cur_status = cur.status if cur else "<deleted>"
                logger.info(
                    f"Upscale {video_id} no longer PENDING "
                    f"(status={cur_status}) — aborting"
                )
                return

            # video_url stores the mediaGenerationId from Google's response
            video_media_id = original_video.video_url
            if not video_media_id:
                raise ValueError("Original video has no mediaGenerationId")

            aspect_ratio_map = {
                "16:9": "VIDEO_ASPECT_RATIO_LANDSCAPE",
                "9:16": "VIDEO_ASPECT_RATIO_PORTRAIT",
            }
            aspect_ratio = aspect_ratio_map.get(
                original_video.aspect_ratio,
                "VIDEO_ASPECT_RATIO_LANDSCAPE"
            )

            logger.info(
                f"Upscale {video_id}: account={account.email}, "
                f"project_id={account.project_id}, media_id={video_media_id[:30]}..."
            )

            MAX_UPSCALE_CAPTCHA_RETRIES = 7
            result = None
            for attempt in range(MAX_UPSCALE_CAPTCHA_RETRIES):
                try:
                    _upscale_proxy = None if account.residential_proxy else await get_proxy_pool().get_next_datacenter()
                    result = await account_client.upscale_video(
                        at=account.at,
                        project_id=account.project_id,
                        video_media_id=video_media_id,
                        aspect_ratio=aspect_ratio,
                        resolution=resolution,
                        user_paygate_tier=account.user_paygate_tier or "PAYGATE_TIER_TWO",
                        account_email=account.email,
                        request_proxy=_upscale_proxy,
                    )
                    break
                except VeoClientError as e:
                    if e.error_code in ("CAPTCHA_FAILED", "FORBIDDEN") and attempt < MAX_UPSCALE_CAPTCHA_RETRIES - 1:
                        logger.warning(
                            f"Upscale {video_id}: captcha/forbidden, attempt {attempt + 1}/{MAX_UPSCALE_CAPTCHA_RETRIES}"
                        )
                        await asyncio.sleep(3)
                        continue
                    raise

            await db.update_video(
                video_id,
                task_id=result.task_id,
                scene_id=result.scene_id,
                progress_message="Upscaling...",
            )

            logger.info(
                f"Video {video_id} upscaling started: task_id={result.task_id}"
            )

        except VeoClientError as e:
            logger.error(f"Video {video_id} upscaling failed: {e}")

            await db.update_video(
                video_id,
                status=VideoStatus.FAILED.value,
                error_code=e.error_code,
                error_message=str(e),
                progress_message=None,
            )

            is_public_error = (
                e.error_code and e.error_code.startswith("PUBLIC_ERROR")
            )
            if account and not is_public_error:
                await self.token_manager.record_error(
                    account.id, e.error_code
                )

        except Exception as e:
            logger.exception(f"Video {video_id} upscale error: {e}")

            await db.update_video(
                video_id,
                status=VideoStatus.FAILED.value,
                error_code="UPSCALE_ERROR",
                error_message=str(e),
                progress_message=None,
            )
