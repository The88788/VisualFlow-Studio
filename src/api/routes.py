"""API routes for the internal parser service."""

import asyncio
import json
import mimetypes
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, UploadFile, Form, File
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from ..core.config import config, TMP_DIR, CONFIG_PATH, BASE_DIR
from ..core.database import db
from ..core.errors import ApiError, ErrorCode
from ..core.logger import logger
from ..core.models import VideoDB, VideoStatus
from ..services.ffmpeg_service import ffmpeg_service, FFmpegError
from ..services.storage import storage
from ..services.video_handler import VideoHandler
from ..services.token_manager import TokenManager
from ..services.veo_client import normalize_proxy_url
from ..services.proxy_pool import _parse_line as _parse_proxy_line
from .schemas import (
    AccountListResponse,
    AccountRequest,
    AccountResponse,
    AddFullAccountRequest,
    ApiResponse,
    CreateImageRequest,
    CreateVideoRequest,
    CreateVideoResponse,
    InstanceStatus,
    MergeVideosRequest,
    ModelsResponse,
    ServiceStatusResponse,
    TrimVideoRequest,
    UpsampleVideoRequest,
    VideoListResponse,
    VideoMetadata,
    VideoResponse,
)

router = APIRouter(prefix="/api", tags=["api"])

# Service instances set during startup
_video_handler: Optional[VideoHandler] = None
_token_manager: Optional[TokenManager] = None

# Max image size in bytes (30MB per TZ spec)
MAX_IMAGE_SIZE_BYTES = 30 * 1024 * 1024

# Service startup time for uptime calculation
_startup_time: Optional[datetime] = None

# Auth process tracking: account_id -> {process, output, running, exit_code}
_auth_processes: dict[int, dict] = {}
_auth_inflight: set[int] = set()
_auth_inflight_lock = asyncio.Lock()

# Path to accounts.json used by setup_puppeteer.js
_ACCOUNTS_JSON_PATH = CONFIG_PATH.parent / "accounts.json"


def set_startup_time() -> None:
    """Set service startup time."""
    global _startup_time
    _startup_time = datetime.utcnow()


def _read_accounts_json() -> list[dict]:
    """Read accounts.json file."""
    if not _ACCOUNTS_JSON_PATH.exists():
        return []
    with open(_ACCOUNTS_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_accounts_json(accounts: list[dict]) -> None:
    """Write accounts.json file."""
    _ACCOUNTS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_ACCOUNTS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)


async def _wipe_browser_profile_for_account(account_id: int, email: str) -> None:
    """Evict any live Chrome session and clear ONLY Google auth cookies
    for this account, preserving the rest of the on-disk profile.

    History (and why we changed strategy)
    -------------------------------------
    Earlier this function did ``shutil.rmtree(profile_dir)`` before re-auth.
    That worked, but for Google's risk model it is actively harmful:
    wiping the dir nukes IndexedDB, Local Storage, Service Worker cache,
    Cache, Code Cache, History, Sessions — i.e. the entire device-history
    that Google uses to recognise "this is the same machine the user has
    been on for weeks". Re-login from a freshly-empty profile under the
    same Google account looks far more like automation ("known account,
    zero device memory") than a normal token refresh, and we observed
    this correlating with ``PUBLIC_ERROR_UNUSUAL_ACTIVITY`` on accounts
    that had been clean before.

    New behaviour
    -------------
    Open ``<profile>/Default/Cookies`` as SQLite, ``DELETE`` rows whose
    ``host_key`` belongs to Google domains. The session-token is gone,
    so OAuth re-fires; everything else (IndexedDB / SW / Cache / fonts
    / Code Cache / History) stays put. The new session-token then lands
    in the *same* profile that already has months of device history.

    If the SQLite step fails (locked file, corrupted DB, missing
    schema), fall back to the old full-wipe so re-auth still proceeds.

    The profile path is resolved exactly the way ``BrowserSessionPool``
    resolves it.
    """
    import shutil
    import sqlite3

    # 1) Close Chrome if the pool has a live session — Chrome holds an
    #    exclusive lock on Cookies (SQLite WAL), so we cannot touch the
    #    file while it is running.
    try:
        from ..services.providers.browser_pool import get_pool
        await get_pool().evict(account_id, timeout=10.0)
        logger.info("Browser auth: evicted live pool session for %s", email)
    except Exception as e:
        logger.debug("Browser auth: pool evict skipped for %s: %s", email, e)

    # 2) Resolve the profile path the same way the pool does.
    profile_root_cfg = config.browser_profile_root
    profile_root = Path(profile_root_cfg) if profile_root_cfg else Path("/opt/veoapi_browser_profiles")
    suffix = config.browser_profile_suffix or "_puppeteer"
    profile_dir = profile_root / f"{account_id}{suffix}"

    if not profile_dir.exists():
        logger.debug("Browser auth: no existing profile dir at %s for %s", profile_dir, email)
        return

    cookies_db = profile_dir / "Default" / "Cookies"
    if not cookies_db.exists():
        # Profile dir exists but has no Cookies DB yet (e.g. interrupted
        # first launch). Nothing to clear — let browser_login.py proceed
        # against the partially-initialised profile, Chrome will repair.
        logger.debug("Browser auth: no Cookies DB at %s — nothing to clear", cookies_db)
        return

    # Google-family hosts whose cookies we drop. Anything else (analytics,
    # CDN, user-installed extensions) stays. ``host_key`` in Chrome's
    # Cookies table is the cookie's Domain attribute and may or may not
    # have a leading dot, so we match both shapes.
    _google_hosts = (
        ".google.com", "google.com",
        ".accounts.google.com", "accounts.google.com",
        ".labs.google", "labs.google",
        ".aisandbox-pa.googleapis.com", "aisandbox-pa.googleapis.com",
        ".googleusercontent.com", "googleusercontent.com",
        ".gstatic.com", "gstatic.com",
        ".youtube.com", "youtube.com",
        ".myaccount.google.com", "myaccount.google.com",
    )

    def _clear_google_cookies_sync() -> int:
        # Chrome can leave a WAL/journal even after the process exits;
        # open with a short busy timeout and retry once.
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                conn = sqlite3.connect(str(cookies_db), timeout=5.0, isolation_level=None)
                try:
                    cur = conn.cursor()
                    placeholders = ",".join("?" * len(_google_hosts))
                    cur.execute(
                        f"DELETE FROM cookies WHERE host_key IN ({placeholders})",
                        _google_hosts,
                    )
                    deleted = cur.rowcount
                    conn.commit()
                    return deleted
                finally:
                    conn.close()
            except sqlite3.OperationalError as e:
                last_err = e
                if attempt == 0 and "lock" in str(e).lower():
                    time.sleep(1.0)
                    continue
                raise
        assert last_err is not None  # for type checkers
        raise last_err

    try:
        deleted = await asyncio.to_thread(_clear_google_cookies_sync)
        logger.info(
            "Browser auth: cleared %d Google cookies for %s — profile preserved "
            "(IndexedDB / LocalStorage / SW / Cache / History intact)",
            deleted, email,
        )
        return
    except Exception as e:
        logger.warning(
            "Browser auth: targeted cookie clear failed for %s at %s: %s — "
            "falling back to full rmtree",
            email, cookies_db, e,
        )

    # ---- Fallback: full wipe (legacy behaviour) -----------------------
    # Only reached if the SQLite path errored out. Better to lose device
    # history than to leave the account stuck unable to re-auth.
    try:
        await asyncio.to_thread(shutil.rmtree, profile_dir, ignore_errors=False)
        logger.warning(
            "Browser auth: FALLBACK wiped entire profile dir %s for %s",
            profile_dir, email,
        )
    except Exception as e:
        logger.warning(
            "Browser auth: rmtree(%s) failed for %s: %s — retrying with ignore_errors",
            profile_dir, email, e,
        )
        try:
            await asyncio.to_thread(shutil.rmtree, profile_dir, ignore_errors=True)
        except Exception as e2:  # pragma: no cover
            logger.error(
                "Browser auth: could not wipe profile dir %s for %s: %s",
                profile_dir, email, e2,
            )


async def _run_browser_auth_subprocess(account_id: int, email: str) -> bool:
    """Run scripts/browser_login.py for one account (browser provider mode).

    Streams stdout into _auth_processes[account_id] so the dashboard /auth-status
    endpoint can show live output.  Returns True on exit-code 0.

    Always wipes the existing browser profile dir first — re-auth must
    start clean, otherwise a stale session-token cookie can let
    browser_login.py reuse the broken session and silently fail.
    """
    import sys

    login_script = BASE_DIR / "scripts" / "browser_login.py"
    if not login_script.exists():
        logger.error("Browser auth: scripts/browser_login.py not found at %s", login_script)
        return False

    # Wipe profile + evict live Chrome BEFORE invoking browser_login.py.
    await _wipe_browser_profile_for_account(account_id, email)

    python_exec = sys.executable
    cmd = [
        "xvfb-run", "--auto-servernum",
        "--server-args=-screen 0 1280x800x24",
        python_exec, str(login_script), "--id", str(account_id), "--all",
    ]

    logger.info("Browser auth: starting for %s: %s", email, " ".join(cmd))

    _auth_processes[account_id] = {
        "running": True,
        "exit_code": None,
        "output": "",
        "email": email,
    }

    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_file_path = logs_dir / f"reauth_{email.replace('@', '_').replace('.', '_')}.log"

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(BASE_DIR),
        )

        output_lines = []
        with open(log_file_path, "w", encoding="utf-8") as log_file:
            log_file.write(f"--- Browser login for {email} at {datetime.utcnow().isoformat()} ---\n")
            async for line in process.stdout:
                decoded = line.decode("utf-8", errors="replace")
                output_lines.append(decoded)
                log_file.write(decoded)
                log_file.flush()
                if len(output_lines) > 200:
                    output_lines = output_lines[-200:]
                _auth_processes[account_id]["output"] = "".join(output_lines)

        await process.wait()
        _auth_processes[account_id]["exit_code"] = process.returncode
        _auth_processes[account_id]["running"] = False

        logger.info(
            "Browser auth: %s finished, exit_code=%d. Log → %s",
            email, process.returncode, log_file_path,
        )
        return process.returncode == 0

    except Exception as e:
        logger.error("Browser auth: %s error: %s", email, e)
        _auth_processes[account_id]["running"] = False
        _auth_processes[account_id]["exit_code"] = -1
        _auth_processes[account_id]["output"] += f"\nError: {e}"
        return False


async def _try_begin_account_auth(account_id: int, email: str) -> bool:
    """Return True only for the first concurrent auth attempt per account."""
    async with _auth_inflight_lock:
        if account_id in _auth_inflight:
            logger.info(
                "Auth already in progress for %s (account_id=%s) — skipping duplicate trigger",
                email,
                account_id,
            )
            return False
        _auth_inflight.add(account_id)
        return True


async def _finish_account_auth(account_id: int) -> None:
    """Release per-account auth guard."""
    async with _auth_inflight_lock:
        _auth_inflight.discard(account_id)


async def _browser_auth_post_success(account_id: int, email: str) -> bool:
    """Post-login steps after browser_login.py exits 0.

    browser_login.py already wrote st + browser_ready=1 to the DB.
    We need to: evict old pool session, refresh the AT (exchange ST→AT),
    and mark account active.
    """
    try:
        from ..services.providers.browser_pool import get_pool
        await get_pool().evict(account_id, timeout=5.0)
        logger.info("Browser auth post-success: evicted old pool session for %s", email)
    except Exception as e:
        logger.warning("Browser auth post-success: pool evict failed for %s: %s", email, e)

    try:
        tm = get_token_manager()
        refreshed = await tm.refresh_account_token(account_id, force=True)
        if refreshed:
            await db.update_account(account_id, is_active=True, status="active")
            logger.info("Browser auth post-success: AT refreshed, %s marked active", email)
            return True
        else:
            logger.warning(
                "Browser auth post-success: AT refresh failed for %s — marking check_required",
                email,
            )
            await db.update_account(account_id, status="check_required", is_active=False)
            return False
    except Exception as e:
        logger.error("Browser auth post-success: error for %s: %s", email, e)
        return False


async def run_single_account_auth(account_id: int) -> bool:
    """
    Run full auth cycle for a single account (blocking until complete).
    In browser mode: runs scripts/browser_login.py (Python/Playwright).
    In veo mode: cleans browser profile, removes old token, runs puppeteer,
    syncs, refreshes, enables.
    Returns True if auth succeeded.
    """
    import shutil

    account = await db.get_account(account_id)
    if not account:
        logger.error(f"Scheduled auth: account {account_id} not found")
        return False

    if not await _try_begin_account_auth(account_id, account.email):
        return False

    try:
        # ---- Browser provider path -----------------------------------------------
        if config.provider_mode == "browser":
            ok = await _run_browser_auth_subprocess(account_id, account.email)
            if ok:
                return await _browser_auth_post_success(account_id, account.email)
            # Non-zero exit — browser_login.py already logs the reason
            await db.update_account(account_id, status="check_required", is_active=False)
            return False
        # --------------------------------------------------------------------------

        script_path = BASE_DIR / "setup_puppeteer.js"
        if not script_path.exists():
            logger.error("Scheduled auth: setup_puppeteer.js not found")
            return False

        email = account.email
        logger.info(f"Scheduled auth: starting for {email}")

        # Clean browser profile
        try:
            accounts_json = _read_accounts_json()
            acc_entry = next((a for a in accounts_json if a.get("email") == email), None)
            if acc_entry and acc_entry.get("profile_path"):
                import shutil
                for suffix in ["", "_puppeteer"]:
                    profile_dir = BASE_DIR / f"{acc_entry['profile_path']}{suffix}"
                    if profile_dir.exists():
                        shutil.rmtree(profile_dir)
                        logger.info(f"Scheduled auth: deleted browser profile: {profile_dir}")
        except Exception as e:
            logger.warning(f"Scheduled auth: profile cleanup failed for {email}: {e}")

        # Remove old token from auth_tokens.json
        auth_tokens_path = CONFIG_PATH.parent / "auth_tokens.json"
        try:
            if auth_tokens_path.exists():
                with open(auth_tokens_path, "r", encoding="utf-8") as f:
                    tokens = json.load(f)
                if email in tokens:
                    del tokens[email]
                with open(auth_tokens_path, "w", encoding="utf-8") as f:
                    json.dump(tokens, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Scheduled auth: token cleanup failed for {email}: {e}")

        # Track in _auth_processes
        _auth_processes[account_id] = {
            "running": True,
            "exit_code": None,
            "output": "",
            "email": email,
        }

        # Run puppeteer
        cmd = [
            "xvfb-run", "--auto-servernum",
            "--server-args=-screen 0 1280x800x24",
            "node", str(script_path), email,
        ]

        try:
            logs_dir = BASE_DIR / "logs"
            logs_dir.mkdir(exist_ok=True)
            log_file_path = logs_dir / f"reauth_{email.replace('@', '_').replace('.', '_')}.log"

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(BASE_DIR),
            )

            output_lines = []
            with open(log_file_path, "w", encoding="utf-8") as log_file:
                log_file.write(f"--- Starting reauth for {email} at {datetime.utcnow().isoformat()} ---\n")
                async for line in process.stdout:
                    decoded = line.decode("utf-8", errors="replace")
                    output_lines.append(decoded)
                    log_file.write(decoded)
                    log_file.flush()
                    if len(output_lines) > 200:
                        output_lines = output_lines[-200:]
                    _auth_processes[account_id]["output"] = "".join(output_lines)

            await process.wait()
            _auth_processes[account_id]["exit_code"] = process.returncode
            _auth_processes[account_id]["running"] = False

            logger.info(f"Scheduled auth: {email} finished, exit_code={process.returncode}. Log saved to {log_file_path}")

            if process.returncode == 0:
                tm = get_token_manager()
                await tm.sync_tokens_from_file(str(auth_tokens_path))

                updated_acc = await db.get_account(account_id)
                if not updated_acc or updated_acc.st == "pending_auth":
                    await db.update_account(
                        account_id, status="check_required", is_active=False,
                        remark=(updated_acc.remark or "") + " | Scheduled auth: no token",
                    )
                    logger.warning(f"Scheduled auth: {email} — no token appeared")
                    return False

                refreshed = await tm.refresh_account_token(account_id, force=True)
                if refreshed:
                    await db.update_account(account_id, is_active=True, status="active")
                    logger.info(f"Scheduled auth: {email} — refreshed & enabled")
                    return True
                else:
                    await db.update_account(
                        account_id, status="check_required", is_active=False,
                        remark=(updated_acc.remark or "") + " | Scheduled auth: refresh failed",
                    )
                    logger.warning(f"Scheduled auth: {email} — refresh failed")
                    return False
            elif process.returncode == 11:
                await db.update_account(account_id, status="banned", is_active=False, remark="Account disabled by Google Workspace policy")
                logger.error(f"Scheduled auth: {email} — account BANNED by Google (exit=11)")
                return False
            else:
                await db.update_account(account_id, status="check_required", is_active=False)
                logger.warning(f"Scheduled auth: {email} — puppeteer failed (exit={process.returncode})")
                return False

        except Exception as e:
            logger.error(f"Scheduled auth: {email} — error: {e}")
            _auth_processes[account_id]["running"] = False
            _auth_processes[account_id]["exit_code"] = -1
            _auth_processes[account_id]["output"] += f"\nError: {e}"
            return False
    finally:
        await _finish_account_auth(account_id)


async def reauth_all_accounts() -> dict:
    """Re-authorize all accounts sequentially. Returns summary stats."""
    accounts = await db.get_all_accounts()
    if not accounts:
        logger.info("Scheduled reauth: no accounts to process")
        return {"total": 0, "success": 0, "failed": 0}

    # Don't reauth accounts that are deliberately in cooldown — that would
    # release them early. Re-auth resumes after the cooldown timer expires.
    _COOLDOWN_STATUSES = {"throttled", "resting", "rate_limited"}
    accounts = [a for a in accounts if a.status not in _COOLDOWN_STATUSES]

    total = len(accounts)
    success = 0
    failed = 0

    logger.info(f"Scheduled reauth: starting for {total} accounts")

    for acc in accounts:
        try:
            result = await run_single_account_auth(acc.id)
            if result:
                success += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f"Scheduled reauth: error for {acc.email}: {e}")
            failed += 1

        # Pause between accounts to avoid rate limits
        await asyncio.sleep(30)

    logger.info(f"Scheduled reauth complete: {success}/{total} succeeded, {failed} failed")
    return {"total": total, "success": success, "failed": failed}


def _format_uptime(seconds: float) -> str:
    """Format uptime as 'Xd Yh Zm'."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    
    return " ".join(parts)


def set_video_handler(handler: VideoHandler) -> None:
    """Set the video handler instance."""
    global _video_handler
    _video_handler = handler


def set_token_manager(manager: TokenManager) -> None:
    """Set the token manager instance."""
    global _token_manager
    _token_manager = manager


def get_video_handler() -> VideoHandler:
    """Get the video handler instance."""
    if _video_handler is None:
        raise ApiError(
            code=ErrorCode.SERVICE_NOT_INITIALIZED,
            message="Service not initialized yet",
            status_code=503,
        )
    return _video_handler


def get_token_manager() -> TokenManager:
    """Get the token manager instance."""
    if _token_manager is None:
        raise ApiError(
            code=ErrorCode.SERVICE_NOT_INITIALIZED,
            message="Service not initialized yet",
            status_code=503,
        )
    return _token_manager


def validate_base64_image(image_data: str, field_name: str) -> None:
    """Validate base64 image or video data size."""
    if not image_data:
        return

    # Skip validation if it is an S3 URI or HTTP/HTTPS URL
    if image_data.startswith(("s3://", "http://", "https://")):
        return

    is_video = False
    if "," in image_data:
        header = image_data.split(",", 1)[0]
        if "video" in header.lower():
            is_video = True
        image_data = image_data.split(",", 1)[1]

    estimated_size = len(image_data) * 3 // 4

    if is_video:
        max_size = 1024 * 1024 * 1024  # 1.0 GB
        if estimated_size > max_size:
            raise ApiError(
                code=ErrorCode.VALIDATION_INVALID_PARAMS,
                message=f"{field_name} is too large. Maximum size is 1.0 GB.",
                status_code=400,
            )
    else:
        if estimated_size > MAX_IMAGE_SIZE_BYTES:
            raise ApiError(
                code=ErrorCode.VALIDATION_INVALID_PARAMS,
                message=f"{field_name} is too large. Maximum size is 30MB.",
                status_code=400,
            )


def build_video_response(video: VideoDB) -> VideoResponse:
    """Build a VideoResponse from a VideoDB record."""
    # Map aspect_ratio to orientation
    orientation = "portrait" if video.aspect_ratio == "9:16" else "landscape"

    # Calculate processingTime
    processing_time = None
    if video.completed_at and video.created_at:
        processing_time = (video.completed_at - video.created_at).total_seconds()

    # Calculate estimatedCompletion
    estimated_completion = None
    if video.status in (VideoStatus.PENDING.value, VideoStatus.GENERATING.value, "pending", "generating"):
        # Rough estimate: 120 seconds from creation
        from datetime import timedelta
        est = video.created_at + timedelta(seconds=120)
        if est > datetime.utcnow():
            estimated_completion = est.isoformat() + "Z"

    # Build download URL
    download_url = None
    if video.download_url:
        # For trimmed/merged videos with local files, or if S3 is disabled
        if video.video_type in ("trimmed", "merged") or not config.s3_enabled:
            download_url = f"/api/videos/{video.video_id}/download"
        else:
            # Construct public S3 URL
            base_url = config.s3_endpoint_url.rstrip('/') if config.s3_endpoint_url else f"https://{config.s3_bucket}.s3.amazonaws.com"
            download_url = f"{base_url}/{config.s3_bucket}/videos/{video.download_url}"

    # Build metadata
    account_email = None
    project_id = None
    if video.account_id:
        # We'll populate this asynchronously in the route if needed
        pass

    has_image = bool(video.start_image_path)

    metadata = VideoMetadata(
        account=account_email,
        has_image=has_image,
        project_id=project_id,
    )

    # Parse original_video_ids from JSON
    original_video_ids = None
    original_videos_count = None
    if video.original_video_ids:
        try:
            original_video_ids = json.loads(video.original_video_ids)
            if isinstance(original_video_ids, list):
                original_videos_count = len(original_video_ids)
        except (json.JSONDecodeError, TypeError):
            pass

    # Format file_size as human-readable string
    file_size_str = None
    if video.file_size:
        size_bytes = video.file_size
        if size_bytes >= 1024 * 1024:
            file_size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
        elif size_bytes >= 1024:
            file_size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            file_size_str = f"{size_bytes} B"

    # Format duration as "Xs"
    duration_str = None
    if video.duration:
        duration_str = f"{video.duration:.1f}s"

    return VideoResponse(
        video_id=video.video_id,
        prompt=video.prompt,
        detected_language=video.detected_language,
        model=video.model,
        orientation=orientation,
        seed=video.seed,
        status=video.status.value if hasattr(video.status, "value") else video.status,
        progress=video.progress,
        created_at=video.created_at,
        completed_at=video.completed_at,
        processing_time=processing_time,
        download_url=download_url,
        file_size=file_size_str,
        duration=duration_str,
        metadata=metadata,
        type=video.video_type or "normal",
        original_video_id=video.original_video_id,
        original_video_ids=original_video_ids,
        original_videos_count=original_videos_count,
        is_continuation=bool(video.continuation_video_id),
        error_code=video.error_code,
        error_message=video.error_message,
    )


def _serialize_datetime(value):
    """Return a stable JSON timestamp for SQLite datetime/string values."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def build_image_response(task: dict) -> dict:
    """Build the public representation of an image generation task."""
    try:
        result_files = json.loads(task.get("result_files") or "[]")
    except (json.JSONDecodeError, TypeError):
        result_files = []
    try:
        metadata = json.loads(task.get("result_metadata") or "[]")
    except (json.JSONDecodeError, TypeError):
        metadata = []

    images = []
    seen_storage_keys: set[str] = set()
    seen_media_names: set[str] = set()
    seen_hashes: set[str] = set()
    if isinstance(result_files, list):
        for index, raw_storage_key in enumerate(result_files):
            item_metadata = (
                metadata[index]
                if isinstance(metadata, list)
                and index < len(metadata)
                and isinstance(metadata[index], dict)
                else {}
            )
            storage_key = str(raw_storage_key)
            media_name = str(item_metadata.get("media_name") or "").strip()
            content_hash = str(item_metadata.get("sha256") or "").strip()
            if (
                storage_key in seen_storage_keys
                or (media_name and media_name in seen_media_names)
                or (content_hash and content_hash in seen_hashes)
            ):
                continue
            seen_storage_keys.add(storage_key)
            if media_name:
                seen_media_names.add(media_name)
            if content_hash:
                seen_hashes.add(content_hash)
            upscale_data = item_metadata.get("upscales", {})
            public_upscales = {}
            if isinstance(upscale_data, dict):
                for scale, state in upscale_data.items():
                    if not isinstance(state, dict):
                        continue
                    public_state = {
                        key: state.get(key)
                        for key in ("status", "error", "file_size")
                        if state.get(key) is not None
                    }
                    if state.get("status") == "completed":
                        public_state["url"] = (
                            f"/api/images/{task['image_id']}/download/{index}/{scale}"
                        )
                    public_upscales[str(scale)] = public_state
            public_metadata = dict(item_metadata)
            public_metadata.pop("upscales", None)
            images.append(
                {
                    "index": index,
                    "url": f"/api/images/{task['image_id']}/download/{index}",
                    "metadata": public_metadata,
                    "upscales": public_upscales,
                }
            )

    return {
        "image_id": task["image_id"],
        "prompt": task.get("prompt"),
        "model": task.get("model"),
        "aspect": task.get("aspect_ratio"),
        "count": (
            len(images)
            if task.get("status") == "completed"
            else task.get("requested_count", 1)
        ),
        "status": task.get("status", "pending"),
        "progress": task.get("progress", 0),
        "error_code": task.get("error_code"),
        "error_message": task.get("error_message"),
        "parent_image_id": task.get("parent_image_id"),
        "parent_result_index": task.get("parent_result_index"),
        "revision": int(task.get("revision") or 1),
        "images": images,
        "created_at": _serialize_datetime(task.get("created_at")),
        "completed_at": _serialize_datetime(task.get("completed_at")),
    }


async def _enrich_video_response(resp: VideoResponse, video: VideoDB) -> VideoResponse:
    """Enrich video response with account info (async)."""
    if video.account_id:
        account = await db.get_account(video.account_id)
        if account and resp.metadata:
            resp.metadata.account = account.email
            resp.metadata.project_id = account.project_id
    return resp


# ==================== Service Endpoints ====================

@router.get("/status")
async def get_service_status():
    """Get service status and account information."""
    accounts = await db.get_all_accounts()
    all_stats = await db.get_all_account_stats()
    video_counts = await db.get_video_counts()

    instances = []
    for acc in accounts:
        stats = all_stats.get(acc.id)
        instances.append(InstanceStatus(
            id=acc.id,
            email=acc.email,
            status=acc.status,
            is_active=acc.is_active,
            project_id=acc.project_id,
            credits=acc.credits,
            total_videos=stats.total_videos if stats else 0,
            success_count=stats.success_count if stats else 0,
            error_count=stats.error_count if stats else 0,
            today_count=stats.today_count if stats else 0,
        ))

    # Calculate uptime
    uptime_str = "0m"
    if _startup_time:
        uptime_seconds = (datetime.utcnow() - _startup_time).total_seconds()
        uptime_str = _format_uptime(uptime_seconds)

    # Count active requests (generating videos)
    active_requests = video_counts.get("generating", 0)

    return ApiResponse(
        success=True,
        data=ServiceStatusResponse(
            service="running",
            status="running",
            instances=instances,
            stats={
                "totalVideos": video_counts.get("total", 0),
                "activeRequests": active_requests,
                "uptime": uptime_str,
            },
        ),
    )


@router.get("/dashboard/data")
async def get_dashboard_data(image_limit: int = 10):
    """Get all dashboard data in a single request."""
    import os

    image_limit = max(10, min(image_limit, 1000))

    accounts = await db.get_all_accounts()
    all_stats = await db.get_all_account_stats()
    video_counts = await db.get_video_counts()
    image_counts = await db.get_image_counts()

    provider_ready = True
    if config.provider_mode == "gflow":
        profile_dir = Path(config.gflow_home) / f"profile_{config.gflow_profile}"
        provider_ready = (
            bool(config.gflow_profile)
            and (profile_dir / ".gflow_browser_strategy").is_file()
            and (profile_dir / "Default" / "Cookies").is_file()
        )

    # Uptime
    uptime_str = "0m"
    uptime_seconds = 0
    if _startup_time:
        uptime_seconds = (datetime.utcnow() - _startup_time).total_seconds()
        uptime_str = _format_uptime(uptime_seconds)

    # Memory (psutil is optional)
    memory_mb = 0
    try:
        import psutil
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        memory_mb = round(mem_info.rss / 1024 / 1024, 1)
    except ImportError:
        pass

    # Accounts with stats
    accounts_data = []
    active_count = 0
    for acc in accounts:
        stats = all_stats.get(acc.id)
        is_active = acc.is_active and acc.status in ("active", "relaxed_only")
        if is_active:
            active_count += 1

        total = (stats.total_videos if stats else 0)
        success = (stats.success_count if stats else 0)
        errors = (stats.error_count if stats else 0)
        success_rate = round((success / total * 100), 1) if total > 0 else 0

        # Check if auth process is running for this account
        auth_proc = _auth_processes.get(acc.id)
        auth_running = auth_proc["running"] if auth_proc else False

        accounts_data.append({
            "id": acc.id,
            "email": acc.email,
            "status": acc.status,
            "is_active": acc.is_active,
            "proxy": acc.proxy,
            "credits": acc.credits,
            "remark": acc.remark,
            "use_count": acc.use_count,
            "last_used_at": acc.last_used_at.isoformat() if acc.last_used_at else None,
            "created_at": acc.created_at.isoformat() if acc.created_at else None,
            "total_videos": total,
            "success_count": success,
            "error_count": errors,
            "consecutive_errors": stats.consecutive_errors if stats else 0,
            "today_count": stats.today_count if stats else 0,
            "success_rate": success_rate,
            "auth_running": auth_running,
            "relaxed_disabled": acc.relaxed_disabled,
        })

    # Recent videos
    recent_videos = await db.get_videos(limit=25)
    videos_data = []
    for v in recent_videos:
        acc = None
        if v.account_id:
            for a in accounts:
                if a.id == v.account_id:
                    acc = a
                    break

        videos_data.append({
            "video_id": v.video_id,
            "prompt": v.prompt[:100] if v.prompt else "",
            "model": v.model,
            "mode": v.mode if isinstance(v.mode, str) else v.mode.value,
            "status": v.status if isinstance(v.status, str) else v.status.value,
            "progress": v.progress,
            "progress_message": v.progress_message,
            "video_type": v.video_type,
            "google_model_key": v.google_model_key,
            "error_code": v.error_code,
            "error_message": v.error_message,
            "account_email": acc.email if acc else None,
            "provider": v.provider,
            "orientation": "portrait" if v.aspect_ratio == "9:16" else "landscape",
            "duration": v.duration,
            "file_size": v.file_size,
            "resolution": v.requested_resolution or (
                "720p" if v.provider == "gflow" and v.download_url else None
            ),
            "download_url": (
                f"/api/videos/{v.video_id}/download"
                if v.download_url and v.status == VideoStatus.COMPLETED.value
                else None
            ),
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "completed_at": v.completed_at.isoformat() if v.completed_at else None,
        })

    recent_images = [
        build_image_response(task)
        for task in await db.get_image_tasks(limit=image_limit)
    ]

    # Overall stats
    total_videos = video_counts.get("total", 0)
    completed = video_counts.get("completed", 0)
    failed = video_counts.get("failed", 0)
    generating = video_counts.get("generating", 0)
    pending = video_counts.get("pending", 0)
    overall_success_rate = round((completed / total_videos * 100), 1) if total_videos > 0 else 0

    return {
        "provider_mode": config.provider_mode,
        "provider_ready": provider_ready,
        "generation_capabilities": {
            "mode": "image-and-video" if config.provider_mode == "gflow" else "legacy",
            "fixed_duration": 8 if config.provider_mode == "gflow" else None,
            "max_concurrent": config.max_concurrent_videos,
        },
        "image_counts": image_counts,
        "video_counts": {
            "total": total_videos,
            "completed": completed,
            "failed": failed,
            "generating": generating,
            "pending": pending,
        },
        "success_rate": overall_success_rate,
        "accounts": accounts_data,
        "active_accounts": active_count,
        "total_accounts": len(accounts),
        "recent_videos": videos_data,
        "recent_images": recent_images,
        "recent_images_limit": image_limit,
        "recent_images_has_more": image_counts.get("total", 0) > len(recent_images),
        "memory_mb": memory_mb,
        "uptime": uptime_str,
        "uptime_seconds": uptime_seconds,
    }


@router.post("/reinitialize")
async def reinitialize():
    """Reinitialize service (refresh all tokens)."""
    tm = get_token_manager()

    try:
        await tm.refresh_all_expiring(force=True)
        return ApiResponse(
            success=True,
            message="Reinitialization started",
        )
    except Exception as e:
        logger.error(f"Reinitialize error: {e}")
        raise ApiError(
            code=ErrorCode.INTERNAL_ERROR,
            message="Failed to reinitialize",
            status_code=500,
        )


@router.get("/generation/relaxed-status")
async def get_relaxed_status():
    """Get current relaxed model circuit breaker state."""
    handler = get_video_handler()
    import time
    now = time.time()
    return ApiResponse(
        success=True,
        data={
            "force_disabled": handler._relaxed_force_disabled,
            "circuit_breaker_active": handler._relaxed_disabled_until > now,
            "circuit_breaker_seconds_left": max(0, int(handler._relaxed_disabled_until - now)),
            "slow_streak": handler._relaxed_slow_streak,
        },
    )


@router.post("/generation/relaxed-mode")
async def set_relaxed_mode(enabled: bool):
    """Manually enable or disable relaxed model mode.

    When disabled, all relaxed-model requests are switched to fast models immediately.
    """
    handler = get_video_handler()
    handler._relaxed_force_disabled = not enabled
    if enabled:
        # Also clear auto circuit breaker so it doesn't override the manual re-enable
        handler._relaxed_disabled_until = 0
        handler._relaxed_slow_streak = 0
        handler._relaxed_probe_count = 0
        logger.info("Relaxed models manually re-enabled")
    else:
        logger.info("Relaxed models manually disabled")
    return ApiResponse(
        success=True,
        message=f"Relaxed models {'enabled' if enabled else 'disabled'}",
    )


@router.get("/models")
async def get_models():
    """Get available models and capabilities."""
    if config.provider_mode == "gflow":
        return ApiResponse(
            success=True,
            data=ModelsResponse(
                all_models=[
                    "nano2", "nano-pro", "image4", "imagen4",
                    "veo31_fast", "veo31_pro", "omni_flash",
                ],
                image_supported_models=["nano2", "nano-pro", "image4", "imagen4"],
                messages=[
                    "The flow.google.com provider supports text-to-image and text-to-video.",
                    "Veo uses the account's fixed 8-second duration.",
                ],
            ),
        )

    return ApiResponse(
        success=True,
        data=ModelsResponse(
            all_models=[
                "veo3_fast",
                "veo3_pro",
                "veo2_fast",
                "sora-2",
                "omni_flash",
            ],
            image_supported_models=[
                "veo3_fast",
                "veo3_pro",
                "sora-2",
                "omni_flash",
            ],
            messages=[
                "Image/video uploads are supported for models: veo3_fast, veo3_pro, sora-2, omni_flash",
                "Model is invalid. Available models: veo3_fast, veo3_pro, veo2_fast, sora-2, omni_flash",
            ],
        ),
    )


# ==================== Image Endpoints ====================

@router.post("/images", status_code=202)
async def create_image(request: Request):
    """Queue an image job, optionally with local reference images."""
    if config.provider_mode != "gflow":
        raise ApiError(
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message="Image generation requires provider_mode=gflow",
            status_code=503,
        )

    profile_dir = Path(config.gflow_home) / f"profile_{config.gflow_profile}"
    if not (
        (profile_dir / ".gflow_browser_strategy").is_file()
        and (profile_dir / "Default" / "Cookies").is_file()
    ):
        raise ApiError(
            code=ErrorCode.SERVICE_NOT_INITIALIZED,
            message="Google Flow profile is not authenticated",
            status_code=503,
        )

    uploads: list[UploadFile] = []
    source_image_id: Optional[str] = None
    source_index = 0
    try:
        if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            form = await request.form()
            req = CreateImageRequest(
                prompt=str(form.get("prompt") or ""),
                model=str(form.get("model") or "nano2"),
                aspect=str(form.get("aspect") or "1:1"),
                count=int(str(form.get("count") or "1")),
            )
            uploads = [
                item for item in form.getlist("references")
                if isinstance(item, (UploadFile, StarletteUploadFile))
            ]
            source_image_id = str(form.get("source_image_id") or "").strip() or None
            source_index = int(str(form.get("source_index") or "0"))
        else:
            req = CreateImageRequest.model_validate(await request.json())
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message="Проверьте промпт и настройки генерации",
            status_code=422,
            details=str(exc),
        ) from exc

    if len(uploads) + (1 if source_image_id else 0) > 10:
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message="Можно загрузить не более 10 референсов",
            status_code=400,
        )

    image_id = str(uuid.uuid4())
    reference_keys: list[str] = []
    parent_revision = 0
    allowed_types = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    try:
        if source_image_id:
            source_task = await db.get_image_task(source_image_id)
            if not source_task or source_task.get("status") != "completed":
                raise ApiError(
                    code=ErrorCode.VALIDATION_IMAGE_NOT_READY,
                    message="Исходное изображение ещё не готово",
                    status_code=409,
                )
            try:
                source_files = json.loads(source_task.get("result_files") or "[]")
            except (json.JSONDecodeError, TypeError):
                source_files = []
            if (
                not isinstance(source_files, list)
                or source_index < 0
                or source_index >= len(source_files)
            ):
                raise ApiError(
                    code=ErrorCode.IMAGE_FILE_NOT_FOUND,
                    message="Исходное изображение не найдено",
                    status_code=404,
                )
            source_key = str(source_files[source_index])
            if not await storage.exists(source_key):
                raise ApiError(
                    code=ErrorCode.IMAGE_FILE_NOT_FOUND,
                    message="Файл исходного изображения отсутствует",
                    status_code=404,
                )
            suffix = Path(source_key).suffix.lower()
            if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
                suffix = ".png"
            copied_key = f"references/{image_id}/source{suffix}"
            content_type = mimetypes.guess_type(source_key)[0] or "image/png"
            await storage.upload(
                copied_key,
                await storage.download(source_key),
                content_type=content_type,
            )
            reference_keys.append(copied_key)
            parent_revision = int(source_task.get("revision") or 1)

        for index, upload in enumerate(uploads):
            content_type = (upload.content_type or "").lower()
            if content_type not in allowed_types:
                raise ApiError(
                    code=ErrorCode.VALIDATION_INVALID_PARAMS,
                    message="Референсы должны быть PNG, JPEG или WebP",
                    status_code=400,
                )
            data = await upload.read(MAX_IMAGE_SIZE_BYTES + 1)
            if not data or len(data) > MAX_IMAGE_SIZE_BYTES:
                raise ApiError(
                    code=ErrorCode.VALIDATION_INVALID_PARAMS,
                    message="Размер одного референса не должен превышать 30 МБ",
                    status_code=400,
                )
            signatures_ok = {
                "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
                "image/jpeg": data.startswith(b"\xff\xd8\xff"),
                "image/webp": len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
            }
            if not signatures_ok[content_type]:
                raise ApiError(
                    code=ErrorCode.VALIDATION_INVALID_PARAMS,
                    message=f"Файл {upload.filename or index + 1} не является корректным изображением",
                    status_code=400,
                )
            key = f"references/{image_id}/{index + 1}{allowed_types[content_type]}"
            await storage.upload(key, data, content_type=content_type)
            reference_keys.append(key)

        await db.create_image_task(
            image_id=image_id,
            prompt=req.prompt,
            model=req.model,
            aspect_ratio=req.aspect,
            requested_count=req.count,
            reference_files=reference_keys,
            parent_image_id=source_image_id,
            parent_result_index=source_index if source_image_id else None,
            revision=parent_revision + 1 if source_image_id else 1,
        )
    except Exception:
        await storage.delete_prefix(f"references/{image_id}")
        raise

    from ..services.providers.gflow_image_provider import get_gflow_image_provider

    asyncio.create_task(get_gflow_image_provider().process(image_id))
    return ApiResponse(
        success=True,
        message="Image refinement queued" if source_image_id else "Image generation queued",
        data={
            "image_id": image_id,
            "status": "pending",
            "revision": parent_revision + 1 if source_image_id else 1,
        },
    )


@router.post("/images/{image_id}/retry", status_code=202)
async def retry_image(image_id: str):
    """Retry a failed image task with the same prompt, settings and references."""
    original = await db.get_image_task(image_id)
    if not original:
        raise ApiError(
            code=ErrorCode.IMAGE_NOT_FOUND,
            message="Задание изображения не найдено",
            status_code=404,
        )
    if original.get("status") != "failed":
        raise ApiError(
            code=ErrorCode.VALIDATION_IMAGE_NOT_READY,
            message="Повторить можно только задание с ошибкой",
            status_code=409,
        )

    try:
        source_keys = json.loads(original.get("reference_files") or "[]")
    except (json.JSONDecodeError, TypeError):
        source_keys = []
    if not isinstance(source_keys, list):
        source_keys = []

    new_image_id = str(uuid.uuid4())
    copied_keys: list[str] = []
    try:
        for index, source_key_value in enumerate(source_keys):
            source_key = str(source_key_value)
            if not await storage.exists(source_key):
                raise ApiError(
                    code=ErrorCode.IMAGE_FILE_NOT_FOUND,
                    message=(
                        "Референсы этого старого задания уже удалены. "
                        "Загрузите их заново; новые ошибки будут хранить файлы для повтора."
                    ),
                    status_code=409,
                )
            suffix = Path(source_key).suffix.lower()
            if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
                suffix = ".png"
            target_key = f"references/{new_image_id}/{index + 1}{suffix}"
            await storage.upload(
                target_key,
                await storage.download(source_key),
                content_type=mimetypes.guess_type(source_key)[0] or "image/png",
            )
            copied_keys.append(target_key)

        await db.create_image_task(
            image_id=new_image_id,
            prompt=str(original.get("prompt") or ""),
            model=str(original.get("model") or "nano2"),
            aspect_ratio=str(original.get("aspect_ratio") or "1:1"),
            requested_count=int(original.get("requested_count") or 1),
            reference_files=copied_keys,
            parent_image_id=original.get("parent_image_id"),
            parent_result_index=original.get("parent_result_index"),
            revision=int(original.get("revision") or 1),
        )
    except Exception:
        await storage.delete_prefix(f"references/{new_image_id}")
        raise

    from ..services.providers.gflow_image_provider import get_gflow_image_provider

    asyncio.create_task(get_gflow_image_provider().process(new_image_id))
    return ApiResponse(
        success=True,
        message="Image retry queued",
        data={
            "image_id": new_image_id,
            "status": "pending",
            "revision": int(original.get("revision") or 1),
        },
    )


@router.delete("/images/{image_id}")
async def delete_image(image_id: str):
    """Delete generated files, uploaded references and the dashboard record."""
    task = await db.get_image_task(image_id)
    if not task:
        raise ApiError(
            code=ErrorCode.IMAGE_NOT_FOUND,
            message="Image task not found",
            status_code=404,
        )
    if task.get("status") in ("pending", "generating"):
        raise ApiError(
            code=ErrorCode.VALIDATION_IMAGE_NOT_READY,
            message="Нельзя удалить изображение во время генерации",
            status_code=409,
        )
    await storage.delete_prefix(f"images/{image_id}")
    await storage.delete_prefix(f"references/{image_id}")
    await db.delete_image_task(image_id)
    return ApiResponse(success=True, message="Изображение удалено")


@router.get("/images")
async def list_images(limit: int = 50, offset: int = 0, status: Optional[str] = None):
    """List image generation tasks, newest first."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    tasks = await db.get_image_tasks(limit=limit, offset=offset, status=status)
    return ApiResponse(
        success=True,
        data={"items": [build_image_response(task) for task in tasks]},
    )


@router.get("/images/{image_id}")
async def get_image(image_id: str):
    """Get one image task and its downloadable results."""
    task = await db.get_image_task(image_id)
    if not task:
        raise ApiError(
            code=ErrorCode.IMAGE_NOT_FOUND,
            message="Image task not found",
            status_code=404,
        )
    return ApiResponse(success=True, data=build_image_response(task))


@router.get("/images/{image_id}/download/{index}")
async def download_image(image_id: str, index: int):
    """Render or download one generated PNG from server-side storage."""
    task = await db.get_image_task(image_id)
    if not task:
        raise ApiError(
            code=ErrorCode.IMAGE_NOT_FOUND,
            message="Image task not found",
            status_code=404,
        )
    if task.get("status") != "completed":
        raise ApiError(
            code=ErrorCode.VALIDATION_IMAGE_NOT_READY,
            message="Image generation is not complete",
            status_code=409,
        )
    try:
        result_files = json.loads(task.get("result_files") or "[]")
    except (json.JSONDecodeError, TypeError):
        result_files = []
    if not isinstance(result_files, list) or index < 0 or index >= len(result_files):
        raise ApiError(
            code=ErrorCode.IMAGE_FILE_NOT_FOUND,
            message="Generated image file not found",
            status_code=404,
        )
    storage_key = str(result_files[index])
    if not await storage.exists(storage_key):
        raise ApiError(
            code=ErrorCode.IMAGE_FILE_NOT_FOUND,
            message="Generated image file not found in storage",
            status_code=404,
        )
    return StreamingResponse(
        storage.stream(storage_key),
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="{image_id}-{index + 1}.png"'
        },
    )


@router.post("/images/{image_id}/upscale/{index}")
async def upscale_image(image_id: str, index: int, scale: str = "2k"):
    """Queue an on-demand native Flow upscale for one generated image."""
    if scale != "2k":
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message="Сейчас доступно повышение только до 2K",
            status_code=400,
        )
    task = await db.get_image_task(image_id)
    if not task:
        raise ApiError(code=ErrorCode.IMAGE_NOT_FOUND, message="Image task not found", status_code=404)
    if task.get("status") != "completed":
        raise ApiError(
            code=ErrorCode.VALIDATION_IMAGE_NOT_READY,
            message="Изображение ещё не готово",
            status_code=409,
        )
    try:
        metadata = json.loads(task.get("result_metadata") or "[]")
    except (json.JSONDecodeError, TypeError):
        metadata = []
    if not isinstance(metadata, list) or index < 0 or index >= len(metadata):
        raise ApiError(code=ErrorCode.IMAGE_FILE_NOT_FOUND, message="Изображение не найдено", status_code=404)
    if not isinstance(metadata[index], dict):
        raise ApiError(code=ErrorCode.IMAGE_FILE_NOT_FOUND, message="Изображение не найдено", status_code=404)
    state = metadata[index].get("upscales", {}).get(scale, {})
    if state.get("status") in ("pending", "generating"):
        return ApiResponse(success=True, message="Подготовка 2K уже выполняется")
    if state.get("status") == "completed" and state.get("storage_key"):
        return ApiResponse(success=True, message="Версия 2K уже готова")
    metadata[index].setdefault("upscales", {})[scale] = {"status": "pending"}
    await db.update_image_task(image_id, result_metadata=json.dumps(metadata))
    from ..services.providers.gflow_image_provider import get_gflow_image_provider

    asyncio.create_task(get_gflow_image_provider().upscale(image_id, index, scale))
    return ApiResponse(success=True, message="Подготовка версии 2K запущена")


@router.get("/images/{image_id}/download/{index}/{scale}")
async def download_upscaled_image(image_id: str, index: int, scale: str):
    """Download a prepared Flow-upscaled image variant."""
    task = await db.get_image_task(image_id)
    if not task:
        raise ApiError(code=ErrorCode.IMAGE_NOT_FOUND, message="Image task not found", status_code=404)
    try:
        metadata = json.loads(task.get("result_metadata") or "[]")
    except (json.JSONDecodeError, TypeError):
        metadata = []
    if not isinstance(metadata, list) or index < 0 or index >= len(metadata):
        raise ApiError(code=ErrorCode.IMAGE_FILE_NOT_FOUND, message="Изображение не найдено", status_code=404)
    if not isinstance(metadata[index], dict):
        raise ApiError(code=ErrorCode.IMAGE_FILE_NOT_FOUND, message="Изображение не найдено", status_code=404)
    state = metadata[index].get("upscales", {}).get(scale, {})
    storage_key = state.get("storage_key") if state.get("status") == "completed" else None
    if not storage_key or not await storage.exists(str(storage_key)):
        raise ApiError(
            code=ErrorCode.VALIDATION_IMAGE_NOT_READY,
            message="Версия 2K ещё не готова",
            status_code=409,
        )
    suffix = Path(str(storage_key)).suffix.lower() or ".png"
    return StreamingResponse(
        storage.stream(str(storage_key)),
        media_type=str(state.get("content_type") or "application/octet-stream"),
        headers={"Content-Disposition": f'inline; filename="{image_id}-{index + 1}-{scale}{suffix}"'},
    )


# ==================== Video Endpoints ====================

@router.post("/videos")
async def create_video(request: Request):
    """
    Create a new video generation request.

    Supports both application/json and multipart/form-data.
    """
    handler = get_video_handler()

    # Parse request body based on content type
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        import json
        mask_coords_raw = form.get("maskCoordinates")
        mask_coords = None
        if mask_coords_raw:
            try:
                # Need to import MaskCoordinates here or at top of file
                from .schemas import MaskCoordinates
                mask_coords = MaskCoordinates(**json.loads(mask_coords_raw))
            except Exception:
                pass

        req_data = CreateVideoRequest(
            prompt=form.get("prompt", ""),
            model=form.get("model", "veo31_fast"),
            orientation=form.get("orientation"),
            seed=int(form.get("seed")) if form.get("seed") else None,
            image=form.get("image"),
            end_image=form.get("endImage"),
            continuation_video_id=form.get("continuation_video_id"),
            insert_video_id=form.get("insert_video_id"),
            remove_video_id=form.get("remove_video_id"),
            mask=form.get("mask"),
            mask_coordinates=mask_coords,
            reshoot_video_id=form.get("reshoot_video_id"),
            reshoot_motion=form.get("reshoot_motion", "forward"),
            skip_continuation_image=form.get("skipContinuationImage", "false").lower() == "true",
            resolution=form.get("resolution"),
            provider=form.get("provider"),
            use_browser_captcha=form.get("useBrowserCaptcha", "false").lower() == "true",
        )
    else:
        body = await request.json()
        from pydantic import ValidationError
        try:
            req_data = CreateVideoRequest(**body)
        except ValidationError as e:
            raise ApiError(
                code=ErrorCode.VALIDATION_INVALID_PARAMS,
                message=f"Invalid request body: {e.errors()}",
                status_code=400,
            )

    if not req_data.prompt or not req_data.prompt.strip():
        if not req_data.reshoot_video_id:
            raise ApiError(
                code=ErrorCode.VALIDATION_PROMPT_REQUIRED,
                message="Prompt is required for this action.",
                status_code=400,
            )

    aspect_ratio = req_data.get_aspect_ratio()
    logger.info(
        "create_video: orientation=%r -> aspect_ratio=%r",
        req_data.orientation, aspect_ratio,
    )
    start_image = req_data.get_start_image()

    # Validate image sizes
    validate_base64_image(start_image, "image")
    validate_base64_image(req_data.end_image, "endImage")

    if req_data.reference_images:
        for i, ref_img in enumerate(req_data.reference_images):
            validate_base64_image(ref_img, f"referenceImages[{i}]")

    # Validate resolution
    requested_resolution = None
    if req_data.resolution:
        res = req_data.resolution.lower().strip()
        # Normalize: "720" -> "720p", "1080" -> "1080p"
        resolution_map = {
            "720": "720p", "720p": "720p",
            "1080": "1080p", "1080p": "1080p",
            "4k": "4k",
        }
        res = resolution_map.get(res)
        if not res:
            raise ApiError(
                code=ErrorCode.VALIDATION_INVALID_PARAMS,
                message="Invalid resolution. Use '720', '720p', '1080', '1080p', or '4k'.",
                status_code=400,
            )
        # 720p = default, no upscale needed
        if res in ("1080p", "4k"):
            requested_resolution = res

    # Validate video ID mutual exclusivity
    provided_ids = sum(x is not None for x in [req_data.continuation_video_id, req_data.insert_video_id, req_data.remove_video_id, req_data.reshoot_video_id])
    if provided_ids > 1:
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message="Cannot mix continuation_video_id, insert_video_id, remove_video_id, and reshoot_video_id",
            status_code=400,
        )

    vid_to_check = req_data.continuation_video_id or req_data.insert_video_id or req_data.remove_video_id or req_data.reshoot_video_id
    if vid_to_check:
        source_video = await db.get_video(vid_to_check)
        if not source_video:
            raise ApiError(
                code=ErrorCode.VIDEO_NOT_FOUND,
                message=f"Source video '{vid_to_check}' not found",
                status_code=400,
            )
        if source_video.status != VideoStatus.COMPLETED.value:
            raise ApiError(
                code=ErrorCode.VALIDATION_VIDEO_NOT_READY,
                message=f"Source video must be completed, current status: {source_video.status}",
                status_code=400,
            )

    # Pre-flight check: refuse new requests when there's no usable account
    # for this generation path. Continuation / extend / upscale / insert /
    # remove / reshoot are pinned to the source video's account, so we skip
    # the check for those — let the existing path handle "source account
    # unavailable" with its own messaging.
    if not vid_to_check:
        active_accounts = await db.get_active_accounts()
        if config.provider_mode == "browser":
            usable = [a for a in active_accounts if getattr(a, "browser_ready", False)]
            mode_hint = "browser-ready"
        elif config.provider_mode == "gflow":
            # The migrated Flow provider owns its authenticated Chrome profile
            # under GFLOW_CLI_HOME instead of the legacy accounts table.
            profile_dir = Path(config.gflow_home) / f"profile_{config.gflow_profile}"
            profile_ready = (
                bool(config.gflow_profile)
                and (profile_dir / ".gflow_browser_strategy").is_file()
                and (profile_dir / "Default" / "Cookies").is_file()
            )
            usable = [config.gflow_profile] if profile_ready else []
            mode_hint = "configured gflow profile"
        else:
            usable = [a for a in active_accounts if a.status != "free_tier"]
            mode_hint = "active"
        if not usable:
            raise ApiError(
                code=ErrorCode.SERVICE_UNAVAILABLE,
                message=f"No {mode_hint} accounts available — service unavailable",
                status_code=503,
            )

    try:
        video_id = await handler.create_video(
            prompt=req_data.prompt,
            model=req_data.model,
            aspect_ratio=aspect_ratio,
            start_image=start_image,
            end_image=req_data.end_image,
            reference_images=req_data.reference_images,
            continuation_video_id=req_data.continuation_video_id,
            insert_video_id=req_data.insert_video_id,
            remove_video_id=req_data.remove_video_id,
            reshoot_video_id=req_data.reshoot_video_id,
            reshoot_motion=req_data.reshoot_motion,
            mask=req_data.mask,
            mask_coordinates=req_data.mask_coordinates,
            skip_continuation_image=req_data.skip_continuation_image,
            seed=req_data.seed,
            requested_resolution=requested_resolution,
            use_browser_captcha=req_data.use_browser_captcha,
            duration=req_data.duration,
        )

        return ApiResponse(
            success=True,
            data=CreateVideoResponse(
                video_id=video_id,
                status="pending",
                message="Video queued for generation"
            ),
        )

    except ValueError as e:
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message=str(e),
            status_code=400,
        )
    except Exception as e:
        logger.exception(f"Create video error: {e}")
        raise ApiError(
            code=ErrorCode.INTERNAL_ERROR,
            message="Internal server error",
            status_code=500,
        )


@router.get("/videos")
async def list_videos(
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
):
    """List videos with pagination and optional status filter."""
    if limit < 1 or limit > 100:
        limit = 50
    if offset < 0:
        offset = 0

    videos = await db.get_videos(limit=limit + 1, offset=offset, status=status)

    has_more = len(videos) > limit
    if has_more:
        videos = videos[:limit]

    total = await db.count_videos(status=status)

    video_responses = []
    for v in videos:
        resp = build_video_response(v)
        resp = await _enrich_video_response(resp, v)
        video_responses.append(resp)

    return ApiResponse(
        success=True,
        data=VideoListResponse(
            videos=video_responses,
            total=total,
            limit=limit,
            offset=offset,
            has_more=has_more,
        ),
    )


@router.get("/videos/{video_id}")
async def get_video_detail(video_id: str):
    """Get detailed video information."""
    video = await db.get_video(video_id)

    if not video:
        raise ApiError(
            code=ErrorCode.VIDEO_NOT_FOUND,
            message="Video not found",
            status_code=404,
        )

    resp = build_video_response(video)
    resp = await _enrich_video_response(resp, video)

    return ApiResponse(
        success=True,
        data=resp,
    )


@router.get("/videos/{video_id}/download")
async def download_video(video_id: str):
    """Download a completed video."""
    video = await db.get_video(video_id)

    if not video:
        raise ApiError(
            code=ErrorCode.VIDEO_NOT_FOUND,
            message="Video not found",
            status_code=404,
        )

    if video.status != VideoStatus.COMPLETED.value:
        raise ApiError(
            code=ErrorCode.VALIDATION_VIDEO_NOT_READY,
            message="Video is not completed",
            status_code=400,
        )

    if not video.download_url:
        raise ApiError(
            code=ErrorCode.VIDEO_FILE_NOT_FOUND,
            message="No download URL available",
            status_code=404,
        )

    # Determine storage key
    video_key = f"videos/{video.download_url}"

    # If download_url is an S3 key (not http), stream from storage
    if not video.download_url.startswith("http"):
        if not await storage.exists(video_key):
            raise ApiError(
                code=ErrorCode.VIDEO_FILE_NOT_FOUND,
                message="Video file not found in storage",
                status_code=404,
            )

        return StreamingResponse(
            storage.stream(video_key),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f"attachment; filename={video_id}.mp4",
            },
        )

    # Fallback: proxy from Google URL (video not yet uploaded to S3)
    import httpx

    async def stream_video():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", video.download_url, follow_redirects=True) as response:
                if response.status_code != 200:
                    raise ApiError(
                        code=ErrorCode.VIDEO_FILE_NOT_FOUND,
                        message=f"Failed to fetch video from upstream: {response.status_code}",
                        status_code=404,
                    )
                async for chunk in response.aiter_bytes():
                    yield chunk

    return StreamingResponse(
        stream_video(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": f"attachment; filename={video_id}.mp4",
        },
    )


@router.post("/videos/{video_id}/upsample", status_code=202)
async def upsample_video(
    video_id: str,
    request: UpsampleVideoRequest = None,
):
    """Upsample a completed video."""
    video = await db.get_video(video_id)

    if not video:
        raise ApiError(
            code=ErrorCode.VIDEO_NOT_FOUND,
            message="Video not found",
            status_code=404,
        )

    if video.status != VideoStatus.COMPLETED.value:
        raise ApiError(
            code=ErrorCode.VALIDATION_VIDEO_NOT_READY,
            message="Can only upsample completed videos",
            status_code=400,
        )

    handler = get_video_handler()
    resolution = request.resolution.lower() if request and request.resolution else "1080p"

    try:
        upscale_video_id = await handler.upscale_video(
            video_id=video_id,
            resolution=resolution,
        )

        upscale_video = await db.get_video(upscale_video_id)
        if upscale_video:
            resp = build_video_response(upscale_video)
        else:
            resp = VideoResponse(
                video_id=upscale_video_id,
                prompt=video.prompt,
                model=video.model,
                status="generating",
                type="upscale",
                original_video_id=video_id,
            )
        
        # Populate upsampleId field per TZ
        resp.upsample_id = upscale_video_id

        return ApiResponse(
            success=True,
            data=resp,
            message="Upsampling started",
        )

    except ValueError as e:
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message=str(e),
            status_code=400,
        )
    except Exception as e:
        logger.exception(f"Upsample video error: {e}")
        raise ApiError(
            code=ErrorCode.INTERNAL_ERROR,
            message="Failed to start upsampling",
            status_code=500,
        )


@router.post("/videos/{video_id}/trim", status_code=201)
async def trim_video(
    video_id: str,
    request: TrimVideoRequest,
):
    """Trim a completed video. Creates a DB record with type=trimmed."""
    video = await db.get_video(video_id)

    if not video:
        raise ApiError(
            code=ErrorCode.VIDEO_NOT_FOUND,
            message="Video not found",
            status_code=404,
        )

    if video.status != VideoStatus.COMPLETED.value:
        raise ApiError(
            code=ErrorCode.VALIDATION_VIDEO_NOT_READY,
            message="Can only trim completed videos",
            status_code=400,
        )

    if not video.download_url:
        raise ApiError(
            code=ErrorCode.VIDEO_FILE_NOT_FOUND,
            message="No video URL available",
            status_code=400,
        )



    try:
        output_path = await ffmpeg_service.trim_video(
            input_url=video.download_url,
            start_offset_sec=request.start_offset_sec,
            end_offset_sec=request.end_offset_sec,
        )

        # Get file metadata
        file_size = output_path.stat().st_size if output_path.exists() else None
        duration_val = None
        try:
            duration_val = await ffmpeg_service.get_video_duration(str(output_path))
        except Exception:
            pass

        # Upload to storage
        video_key = f"videos/{output_path.name}"
        await storage.upload_file(video_key, output_path, content_type="video/mp4")
        output_path.unlink(missing_ok=True)

        # Create DB record for trimmed video (format per TZ: trim_{short_id}_{random})
        new_video_id = f"trim_{uuid.uuid4().hex[:8]}_{uuid.uuid4().hex[:11]}"
        await db.create_video(
            video_id=new_video_id,
            prompt=video.prompt,
            detected_language=video.detected_language,
            model=video.model,
            google_model_key=video.google_model_key,
            mode=video.mode.value if hasattr(video.mode, "value") else video.mode,
            aspect_ratio=video.aspect_ratio.value if hasattr(video.aspect_ratio, "value") else video.aspect_ratio,
            seed=video.seed,
            status=VideoStatus.COMPLETED.value,
            video_type="trimmed",
            original_video_id=video_id,
            download_url=output_path.name,
            file_size=file_size,
            duration=duration_val,
            completed_at=datetime.utcnow(),
        )

        new_video = await db.get_video(new_video_id)
        resp = build_video_response(new_video) if new_video else VideoResponse(
            video_id=new_video_id,
            prompt=video.prompt,
            model=video.model,
            status="completed",
            type="trimmed",
            original_video_id=video_id,
        )

        return ApiResponse(
            success=True,
            data=resp,
            message="Video trimmed successfully",
        )

    except FFmpegError as e:
        raise ApiError(
            code=ErrorCode.VIDEO_TRIM_FAILED,
            message=str(e),
            status_code=500,
        )


@router.post("/merge-videos", status_code=202)
async def merge_videos(request: MergeVideosRequest):
    """
    Merge multiple completed videos into one (async).
    
    Returns 202 Accepted immediately. Poll GET /api/videos/{videoId} 
    for status updates.
    """
    import asyncio
    
    if len(request.video_ids) > 10:
        raise ApiError(
            code=ErrorCode.VALIDATION_TOO_MANY_VIDEOS,
            message="Maximum 10 videos can be merged",
            status_code=400,
        )

    video_urls = []
    first_video = None

    for vid in request.video_ids:
        video = await db.get_video(vid)

        if not video:
            raise ApiError(
                code=ErrorCode.VIDEO_NOT_FOUND,
                message=f"Video {vid} not found",
                status_code=404,
            )

        if video.status != VideoStatus.COMPLETED.value:
            raise ApiError(
                code=ErrorCode.VALIDATION_VIDEO_NOT_READY,
                message=f"Video {vid} is not completed",
                status_code=400,
            )

        if not video.download_url:
            raise ApiError(
                code=ErrorCode.VIDEO_FILE_NOT_FOUND,
                message=f"Video {vid} has no download URL",
                status_code=400,
            )

        video_urls.append(video.download_url)
        if first_video is None:
            first_video = video

    # Parse per-video offsets
    per_video_offsets: list[float] | None = None
    if request.per_video_end_offset_sec is not None:
        pv = request.per_video_end_offset_sec
        if isinstance(pv, list):
            per_video_offsets = [float(x) if x else 0 for x in pv]
        elif isinstance(pv, dict):
            if all(vid in pv for vid in request.video_ids):
                per_video_offsets = [float(pv.get(vid, 0)) for vid in request.video_ids]
            else:
                per_video_offsets = [float(v) if v else 0 for v in pv.values()]

    # Generate video ID upfront
    new_video_id = f"merged_{uuid.uuid4().hex[:8]}"
    
    # Create DB record immediately with status=generating
    await db.create_video(
        video_id=new_video_id,
        prompt=f"Merged video from {len(request.video_ids)} videos",
        model="merged",
        google_model_key="merged",
        mode=first_video.mode.value if first_video and hasattr(first_video.mode, "value") else "t2v",
        aspect_ratio=first_video.aspect_ratio.value if first_video and hasattr(first_video.aspect_ratio, "value") else "16:9",
        status=VideoStatus.GENERATING.value,
        video_type="merged",
        original_video_ids=request.video_ids,
    )

    # Background task for actual merge
    async def do_merge():
        try:
            # Update progress: downloading
            await db.update_video(new_video_id, progress=10, progress_message="Скачивание видео...")
            
            output_path = await ffmpeg_service.merge_videos(
                video_urls=video_urls,
                end_offset_sec=request.end_offset_sec,
                per_video_end_offset_sec=per_video_offsets,
            )

            # Upload to storage
            video_key = f"videos/{output_path.name}"
            await storage.upload_file(video_key, output_path, content_type="video/mp4")
            output_path.unlink(missing_ok=True)

            # Update progress: processing
            await db.update_video(new_video_id, progress=80, progress_message="Объединение...")

            # Get file metadata
            file_size = output_path.stat().st_size if output_path.exists() else None
            duration_val = None
            try:
                duration_val = await ffmpeg_service.get_video_duration(str(output_path))
            except Exception:
                pass

            # Update to completed
            await db.update_video(
                new_video_id,
                status=VideoStatus.COMPLETED.value,
                download_url=output_path.name,
                file_size=file_size,
                duration=duration_val,
                completed_at=datetime.utcnow(),
                progress=100,
                progress_message=None,
            )
            logger.info(f"Merge completed: {new_video_id}")

        except FFmpegError as e:
            logger.error(f"Merge failed for {new_video_id}: {e}")
            await db.update_video(
                new_video_id,
                status=VideoStatus.FAILED.value,
                error_code="VIDEO_MERGE_FAILED",
                error_message=str(e),
                progress=0,
                progress_message=None,
            )
        except Exception as e:
            logger.exception(f"Unexpected error merging {new_video_id}: {e}")
            await db.update_video(
                new_video_id,
                status=VideoStatus.FAILED.value,
                error_code="INTERNAL_ERROR",
                error_message=str(e),
                progress=0,
                progress_message=None,
            )

    # Start background task
    asyncio.create_task(do_merge())

    # Return immediately with 202
    new_video = await db.get_video(new_video_id)
    resp = build_video_response(new_video) if new_video else VideoResponse(
        video_id=new_video_id,
        prompt=f"Merged video from {len(request.video_ids)} videos",
        model="merged",
        status="generating",
        type="merged",
        original_video_ids=request.video_ids,
        original_videos_count=len(request.video_ids),
        progress=0,
        progress_message="Начало склейки...",
    )

    return JSONResponse(
        status_code=202,
        content=ApiResponse(
            success=True,
            data=resp,
            message="Склейка видео поставлена в очередь",
        ).model_dump(mode='json', by_alias=True, exclude_none=True),
    )


# ==================== Account Endpoints ====================

@router.get("/accounts")
async def list_accounts():
    """List all accounts with statistics."""
    accounts = await db.get_all_accounts()
    all_stats = await db.get_all_account_stats()

    account_responses = []
    for acc in accounts:
        stats = all_stats.get(acc.id)
        account_responses.append(AccountResponse(
            id=acc.id,
            email=acc.email,
            is_active=acc.is_active,
            status=acc.status,
            project_id=acc.project_id,
            credits=acc.credits,
            remark=acc.remark,
            created_at=acc.created_at,
            last_used_at=acc.last_used_at,
            use_count=acc.use_count,
            total_videos=stats.total_videos if stats else 0,
            success_count=stats.success_count if stats else 0,
            error_count=stats.error_count if stats else 0,
            consecutive_errors=stats.consecutive_errors if stats else 0,
            today_count=stats.today_count if stats else 0,
        ))

    return ApiResponse(
        success=True,
        data=AccountListResponse(
            accounts=account_responses,
            total=len(account_responses),
        ),
    )


@router.post("/accounts")
async def add_account(request: AccountRequest):
    """Add a new account."""
    tm = get_token_manager()

    # Check if email already exists
    existing = await db.get_account_by_email(request.email)
    if existing:
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message=f"Account with email '{request.email}' already exists",
            status_code=400,
        )

    try:
        account_id = await tm.add_account(
            email=request.email,
            st=request.st or "",
            refresh_token=request.refresh_token,
            remark=request.remark,
            proxy=normalize_proxy_url(request.proxy) if request.proxy else None,
        )

        account = await db.get_account(account_id)
        if not account:
            raise ApiError(
                code=ErrorCode.INTERNAL_ERROR,
                message="Failed to retrieve created account",
                status_code=500,
            )

        return ApiResponse(
            success=True,
            data=AccountResponse(
                id=account.id,
                email=account.email,
                is_active=account.is_active,
                status=account.status,
                project_id=account.project_id,
                credits=account.credits,
                remark=account.remark,
                created_at=account.created_at,
                last_used_at=account.last_used_at,
                use_count=account.use_count,
            ),
            message="Account added successfully",
        )

    except Exception as e:
        logger.error(f"Add account error: {e}")
        raise ApiError(
            code=ErrorCode.INTERNAL_ERROR,
            message=str(e),
            status_code=500,
        )


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int):
    """Delete an account from DB and accounts.json."""
    account = await db.get_account(account_id)
    if not account:
        raise ApiError(
            code=ErrorCode.ACCOUNT_NOT_FOUND,
            message="Account not found",
            status_code=404,
        )

    await db.delete_account(account_id)

    # Also remove from accounts.json
    try:
        accounts_json = _read_accounts_json()
        accounts_json = [a for a in accounts_json if a.get("email") != account.email]
        _write_accounts_json(accounts_json)
        logger.info(f"Removed {account.email} from accounts.json")
    except Exception as e:
        logger.warning(f"Failed to remove from accounts.json: {e}")

    # Also remove from auth_tokens.json (prevents re-creation on restart)
    try:
        auth_tokens_path = CONFIG_PATH.parent / "auth_tokens.json"
        if auth_tokens_path.exists():
            with open(auth_tokens_path, "r", encoding="utf-8") as f:
                tokens = json.load(f)
            if account.email in tokens:
                del tokens[account.email]
                with open(auth_tokens_path, "w", encoding="utf-8") as f:
                    json.dump(tokens, f, indent=2, ensure_ascii=False)
                logger.info(f"Removed {account.email} from auth_tokens.json")
    except Exception as e:
        logger.warning(f"Failed to remove from auth_tokens.json: {e}")

    return ApiResponse(
        success=True,
        message=f"Account {account.email} deleted",
    )


@router.post("/accounts/{account_id}/enable")
async def enable_account(account_id: int):
    """Enable an account."""
    account = await db.get_account(account_id)
    if not account:
        raise ApiError(
            code=ErrorCode.ACCOUNT_NOT_FOUND,
            message="Account not found",
            status_code=404,
        )

    await db.update_account(account_id, is_active=True, status="active")
    await db.reset_consecutive_errors(account_id)

    return ApiResponse(
        success=True,
        message=f"Account {account.email} enabled",
    )


@router.post("/accounts/{account_id}/disable")
async def disable_account(account_id: int):
    """Disable an account."""
    account = await db.get_account(account_id)
    if not account:
        raise ApiError(
            code=ErrorCode.ACCOUNT_NOT_FOUND,
            message="Account not found",
            status_code=404,
        )

    await db.update_account(account_id, is_active=False, status="disabled")

    return ApiResponse(
        success=True,
        message=f"Account {account.email} disabled",
    )


@router.post("/accounts/{account_id}/cooldown")
async def set_account_cooldown(account_id: int, hours: float = 0.5):
    """Manually put an account into cooldown for ``hours`` hours.

    Sets status='resting' so the dashboard shows the standard cooldown
    badge; auto-reenable is scheduled exactly like UNUSUAL_ACTIVITY rest.
    """
    if hours <= 0 or hours > 168:  # max 7 days
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message="hours must be between 0 (exclusive) and 168",
            status_code=400,
        )
    tm = get_token_manager()
    ok = await tm.manual_cooldown(account_id, hours)
    if not ok:
        raise ApiError(
            code=ErrorCode.ACCOUNT_NOT_FOUND,
            message="Account not found",
            status_code=404,
        )
    return ApiResponse(success=True, message=f"Account placed in cooldown for {hours}h")


@router.post("/accounts/{account_id}/relaxed-mode")
async def set_account_relaxed_mode(account_id: int, enabled: bool):
    """Enable or disable relaxed mode for a specific account.

    When enabled, in browser mode the account is HARD PINNED to the
    lower-priority Flow queue (Veo 3.1 - Lite [Lower) regardless of
    which model the user requested, and the watchdog uses a longer
    SLA. ``force_relaxed`` controls that. ``relaxed_disabled`` (the
    inverse-named legacy flag — True means BLOCK relaxed) is also
    cleared/set so HTTP-mode behavior stays consistent: enabling
    relaxed mode obviously can't simultaneously block it.
    """
    account = await db.get_account(account_id)
    if not account:
        raise ApiError(
            code=ErrorCode.ACCOUNT_NOT_FOUND,
            message="Account not found",
            status_code=404,
        )
    await db.update_account(
        account_id,
        force_relaxed=enabled,
        relaxed_disabled=not enabled,
    )
    action = "enabled" if enabled else "disabled"
    logger.info(f"Relaxed mode {action} for account {account.email} (force_relaxed={enabled})")
    return ApiResponse(
        success=True,
        message=f"Relaxed mode {action} for {account.email}",
    )


@router.post("/accounts/{account_id}/refresh")
async def refresh_account(account_id: int):
    """Refresh account access token."""
    account = await db.get_account(account_id)
    if not account:
        raise ApiError(
            code=ErrorCode.ACCOUNT_NOT_FOUND,
            message="Account not found",
            status_code=404,
        )

    tm = get_token_manager()

    try:
        success = await tm.refresh_account_token(account_id, force=True)
        if success:
            return ApiResponse(
                success=True,
                message=f"Token refreshed for {account.email}",
            )
        else:
            # Check if account was auto-disabled (e.g. free tier)
            updated_account = await db.get_account(account_id)
            if updated_account and updated_account.status in ("free_tier", "invalid_tier", "low_credits"):
                return ApiResponse(
                    success=True,
                    message=f"Account {account.email} auto-disabled: {updated_account.status}",
                )
            elif updated_account and updated_account.status in ("needs_reauth", "check_required"):
                # Auto-start reauthorization
                asyncio.create_task(run_single_account_auth(account_id))
                return ApiResponse(
                    success=True,
                    message=f"Session expired for {account.email}. Auto-reauthorization started in background.",
                )
            raise ApiError(
                code=ErrorCode.SERVICE_TEMPORARILY_UNAVAILABLE,
                message="Token refresh failed",
                status_code=500,
            )
    except ApiError:
        raise
    except Exception as e:
        logger.error(f"Refresh token error: {e}")
        raise ApiError(
            code=ErrorCode.INTERNAL_ERROR,
            message=str(e),
            status_code=500,
        )


# ==================== Full Account Management ====================

@router.post("/accounts/add-full")
async def add_full_account(request: AddFullAccountRequest):
    """Add account to both DB and accounts.json."""
    # Check if email already exists in DB
    existing = await db.get_account_by_email(request.email)
    if existing:
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message=f"Account with email '{request.email}' already exists",
            status_code=400,
        )

    # Add to accounts.json
    try:
        accounts_json = _read_accounts_json()

        # Check for duplicate in accounts.json
        if any(a.get("email") == request.email for a in accounts_json):
            raise ApiError(
                code=ErrorCode.VALIDATION_INVALID_PARAMS,
                message=f"Account '{request.email}' already exists in accounts.json",
                status_code=400,
            )

        norm_rp = normalize_proxy_url(request.residential_proxy) if request.residential_proxy else ""

        # Auto-assign proxy from pool if none provided
        auto_proxy = request.proxy or ""
        if auto_proxy and "," in auto_proxy:
            parsed = _parse_proxy_line(auto_proxy)
            if parsed:
                auto_proxy = parsed

        if not auto_proxy and not norm_rp:
            try:
                import random
                pool_path = Path(config.proxy_pool_path)
                if pool_path.exists():
                    raw = pool_path.read_text(encoding="utf-8").splitlines()
                    pool = [p for line in raw if (p := _parse_proxy_line(line)) is not None]
                    if pool:
                        all_accounts = await db.get_all_accounts()
                        used = {a.proxy for a in all_accounts if a.proxy}
                        free = [p for p in pool if p not in used]
                        auto_proxy = random.choice(free) if free else random.choice(pool)
                        logger.info(f"Auto-assigned proxy {auto_proxy!r} to {request.email}")
            except Exception as e:
                logger.warning(f"Could not auto-assign proxy for {request.email}: {e}")

        new_entry = {
            "name": request.name,
            "email": request.email,
            "password": request.password,
            "otp_secret": request.otp_secret,
            "profile_path": f"./browser_profiles/{request.name}",
            "proxy": auto_proxy,
            "residential_proxy": norm_rp,
        }
        accounts_json.append(new_entry)
        _write_accounts_json(accounts_json)
        logger.info(f"Added {request.email} to accounts.json")
    except ApiError:
        raise
    except Exception as e:
        logger.error(f"Failed to write accounts.json: {e}")
        raise ApiError(
            code=ErrorCode.INTERNAL_ERROR,
            message=f"Failed to update accounts.json: {e}",
            status_code=500,
        )

    # Add to DB with pending auth status
    try:
        account_id = await db.create_account(
            email=request.email,
            st="pending_auth",
            remark=f"Name: {request.name}",
            proxy=auto_proxy or None,
        )
        _update_kwargs: dict = {"status": "needs_auth", "is_active": False}
        if norm_rp:
            _update_kwargs["residential_proxy"] = norm_rp
        await db.update_account(account_id, **_update_kwargs)

        account = await db.get_account(account_id)
        return ApiResponse(
            success=True,
            data=AccountResponse(
                id=account.id,
                email=account.email,
                is_active=account.is_active,
                status=account.status,
                remark=account.remark,
                created_at=account.created_at,
                last_used_at=account.last_used_at,
                use_count=account.use_count,
            ),
            message="Account added. Click 'Auth' to authorize via browser.",
        )
    except Exception as e:
        logger.error(f"Failed to add account to DB: {e}")
        raise ApiError(
            code=ErrorCode.INTERNAL_ERROR,
            message=str(e),
            status_code=500,
        )


@router.post("/accounts/{account_id}/authorize")
async def authorize_account(account_id: int):
    """Launch authorization for an account.

    In browser mode (provider_mode=browser): runs scripts/browser_login.py
    (Python/Playwright).  In veo mode: runs setup_puppeteer.js (Node.js).
    """
    account = await db.get_account(account_id)
    if not account:
        raise ApiError(
            code=ErrorCode.ACCOUNT_NOT_FOUND,
            message="Account not found",
            status_code=404,
        )

    # Check if already running
    if (
        account_id in _auth_processes and _auth_processes[account_id].get("running")
    ) or account_id in _auth_inflight:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "error": {
                    "code": "AUTH_ALREADY_RUNNING",
                    "message": "Authorization is already running for this account",
                },
            },
        )

    # ---- Browser provider path -----------------------------------------------
    if config.provider_mode == "browser":
        async def _run_browser():
            await run_single_account_auth(account_id)

        asyncio.create_task(_run_browser())
        return JSONResponse(
            status_code=202,
            content={
                "success": True,
                "message": f"Browser login started for {account.email}",
            },
        )
    # --------------------------------------------------------------------------

    # Find setup_puppeteer.js
    script_path = BASE_DIR / "setup_puppeteer.js"
    if not script_path.exists():
        raise ApiError(
            code=ErrorCode.INTERNAL_ERROR,
            message="setup_puppeteer.js not found",
            status_code=500,
        )

    # Clean up old browser profile for this account
    try:
        accounts_json = _read_accounts_json()
        acc_entry = next((a for a in accounts_json if a.get("email") == account.email), None)
        if acc_entry and acc_entry.get("profile_path"):
            import shutil
            for suffix in ["", "_puppeteer"]:
                profile_dir = BASE_DIR / f"{acc_entry['profile_path']}{suffix}"
                if profile_dir.exists():
                    shutil.rmtree(profile_dir)
                    logger.info(f"Deleted browser profile: {profile_dir}")
    except Exception as e:
        logger.warning(f"Failed to cleanup browser profile: {e}")

    # Remove old token from auth_tokens.json
    try:
        auth_tokens_path = CONFIG_PATH.parent / "auth_tokens.json"
        if auth_tokens_path.exists():
            with open(auth_tokens_path, "r", encoding="utf-8") as f:
                tokens = json.load(f)
            if account.email in tokens:
                del tokens[account.email]
                with open(auth_tokens_path, "w", encoding="utf-8") as f:
                    json.dump(tokens, f, indent=2, ensure_ascii=False)
                logger.info(f"Removed old token for {account.email} from auth_tokens.json")
    except Exception as e:
        logger.warning(f"Failed to cleanup auth_tokens.json: {e}")

    # Build command
    cmd = [
        "xvfb-run", "--auto-servernum",
        "--server-args=-screen 0 1280x800x24",
        "node", str(script_path), account.email,
    ]

    logger.info(f"Starting auth for {account.email}: {' '.join(cmd)}")

    # Initialize process tracking
    _auth_processes[account_id] = {
        "running": True,
        "exit_code": None,
        "output": "",
        "email": account.email,
    }

    async def run_auth():
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(BASE_DIR),
            )

            output_lines = []
            async for line in process.stdout:
                decoded = line.decode("utf-8", errors="replace")
                output_lines.append(decoded)
                # Keep last 200 lines
                if len(output_lines) > 200:
                    output_lines = output_lines[-200:]
                _auth_processes[account_id]["output"] = "".join(output_lines)

            await process.wait()
            _auth_processes[account_id]["exit_code"] = process.returncode
            _auth_processes[account_id]["running"] = False

            logger.info(
                f"Auth process for {account.email} finished: exit_code={process.returncode}"
            )

            # Auto-sync tokens, refresh AT, and enable account
            if process.returncode == 0:
                try:
                    tm = get_token_manager()
                    auth_tokens_path = CONFIG_PATH.parent / "auth_tokens.json"
                    await tm.sync_tokens_from_file(str(auth_tokens_path))
                    logger.info(f"Tokens synced after auth for {account.email}")

                    # Check if token actually appeared
                    updated_acc = await db.get_account(account_id)
                    if not updated_acc or updated_acc.st == "pending_auth":
                        # No token was produced — mark for review
                        await db.update_account(
                            account_id,
                            status="check_required",
                            is_active=False,
                            remark=(updated_acc.remark or "") + " | Auth completed but no token found",
                        )
                        logger.warning(f"Auth for {account.email} finished but no token appeared — marked check_required")
                    else:
                        # Refresh token (exchange ST for AT)
                        refreshed = await tm.refresh_account_token(account_id, force=True)
                        if refreshed:
                            logger.info(f"Token refreshed after auth for {account.email}")
                            await db.update_account(account_id, is_active=True, status="active")
                            logger.info(f"Account {account.email} enabled after auth")
                        else:
                            await db.update_account(
                                account_id,
                                status="check_required",
                                is_active=False,
                                remark=(updated_acc.remark or "") + " | Token refresh failed after auth",
                            )
                            logger.warning(f"Token refresh failed for {account.email} — marked check_required")
                except Exception as e:
                    logger.error(f"Post-auth setup failed for {account.email}: {e}")
            elif process.returncode == 11:
                # Account disabled by Google
                await db.update_account(
                    account_id,
                    status="banned",
                    is_active=False,
                    remark="Account disabled by Google Workspace policy",
                )
                logger.error(f"Auth for {account.email} BANNED by Google (exit=11)")
            else:
                # Non-zero exit code — mark for review
                await db.update_account(
                    account_id,
                    status="check_required",
                    is_active=False,
                )
                logger.warning(f"Auth for {account.email} failed (exit_code={process.returncode}) — marked check_required")

        except Exception as e:
            logger.error(f"Auth process error for {account.email}: {e}")
            _auth_processes[account_id]["running"] = False
            _auth_processes[account_id]["exit_code"] = -1
            _auth_processes[account_id]["output"] += f"\nError: {e}"

    asyncio.create_task(run_auth())

    return JSONResponse(
        status_code=202,
        content={
            "success": True,
            "message": f"Authorization started for {account.email}",
        },
    )


@router.get("/proxies")
async def list_proxies():
    """List available datacenter proxies from proxy_pool.txt."""
    try:
        pool_path = Path(config.proxy_pool_path)
        proxies = []
        if pool_path.exists():
            lines = pool_path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                line_str = line.strip()
                if not line_str or line_str.startswith("#"):
                    continue
                parts = line_str.split(",")
                if len(parts) >= 4:
                    if parts[0].strip().lower() == "id":
                        continue
                    label = parts[0].strip()
                    server = parts[1].strip()
                    username = parts[2].strip()
                    password = parts[3].strip()
                    enabled = parts[4].strip().lower() if len(parts) > 4 else "true"
                    if enabled == "false":
                        continue
                    if not server:
                        continue
                    # Parse using _parse_proxy_line to align with standard format
                    parsed_url = _parse_proxy_line(line_str)
                    if parsed_url:
                        proxies.append({
                            "raw": line_str,
                            "label": label,
                            "parsed": parsed_url
                        })
                elif line_str.startswith("http://") or line_str.startswith("https://") or line_str.startswith("socks"):
                    proxies.append({
                        "raw": line_str,
                        "label": line_str,
                        "parsed": line_str
                    })
        return ApiResponse(
            success=True,
            data={"proxies": proxies},
        )
    except Exception as e:
        logger.error(f"Error reading proxies: {e}")
        return ApiResponse(
            success=False,
            message=f"Failed to load proxies: {e}",
        )


@router.post("/accounts/rotate-proxies")
async def rotate_proxies():
    """Assign each account a different proxy from the configured proxy pool.

    Picks proxies that are not currently used by any account (unused first).
    If the pool is smaller than the account count, cycles through available proxies.
    """
    pool_path = Path(config.proxy_pool_path)
    if not pool_path.exists():
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message=f"Proxy pool file not found: {pool_path}",
            status_code=404,
        )

    raw_lines = pool_path.read_text(encoding="utf-8").splitlines()
    pool: list[str] = [p for line in raw_lines if (p := _parse_proxy_line(line)) is not None]

    if not pool:
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message="Proxy pool file is empty",
            status_code=400,
        )

    accounts = await db.get_all_accounts()
    if not accounts:
        return ApiResponse(success=True, message="No accounts to update", data={"updated": 0})

    # Proxies currently assigned to accounts
    used_proxies: set[str] = {a.proxy for a in accounts if a.proxy}

    # Prefer proxies not yet assigned to anyone
    import random
    free_pool = [p for p in pool if p not in used_proxies]
    random.shuffle(free_pool)

    # If free pool too small, pad with shuffled full pool (excluding current account's proxy)
    full_pool_shuffled = pool[:]
    random.shuffle(full_pool_shuffled)

    updated = 0
    for acc in accounts:
        # Pick from free pool first, fall back to full pool
        candidate = None
        for p in free_pool:
            if p != acc.proxy:
                candidate = p
                free_pool.remove(p)
                break
        if not candidate:
            for p in full_pool_shuffled:
                if p != acc.proxy:
                    candidate = p
                    break
        if not candidate:
            continue

        await db.update_account(acc.id, proxy=candidate)
        # Reset cached VeoClient for old proxy so next use picks up the new one
        if _token_manager:
            await _token_manager.reset_client_for_proxy(acc.proxy)
        updated += 1

    logger.info(f"Rotated proxies for {updated}/{len(accounts)} accounts")
    return ApiResponse(
        success=True,
        message=f"Rotated proxies for {updated} accounts",
        data={"updated": updated, "pool_size": len(pool)},
    )


@router.post("/accounts/force-rotate")
async def force_rotate_proxies(request: Request):
    """Manually trigger a proxy rotation (the same swap the
    proxy_rotation_monitor does automatically when all accounts get flagged).

    Body (optional JSON):
        {"all": true}   # rotate every participating account, not just flagged ones

    Swaps each target account onto a rested proxy from the pool, benches the
    burnt proxies on cooldown, and kicks off a background re-auth on the fresh
    IPs. The swap result is returned synchronously; the re-auth runs in the
    background (watch the logs / GET /api/accounts for status).
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    force_all = bool(body.get("all", False))

    from ..services.proxy_rotator import get_proxy_rotator, _norm
    from ..main import _PROXY_FLAGGED_STATUSES, reauth_rotated_accounts

    rotator = get_proxy_rotator()
    await rotator.sync()

    accounts = await db.get_all_accounts()
    participating = [
        a for a in accounts
        if a.proxy and a.status not in ("disabled", "banned", "free_tier")
    ]
    if force_all:
        targets = participating
    else:
        targets = [a for a in participating if a.status in _PROXY_FLAGGED_STATUSES]

    if not targets:
        return ApiResponse(
            success=False,
            message="No accounts to rotate (none flagged; pass {\"all\": true} to force)",
            data={"participating": len(participating)},
        )

    # Report fresh-proxy availability up front (the main failure mode).
    in_use = {_norm(a.proxy) for a in accounts if a.proxy}
    in_use.discard(None)
    fresh = await rotator.pick_fresh(len(targets), exclude=in_use)
    if len(fresh) < len(targets):
        return ApiResponse(
            success=False,
            message=(
                f"Not enough rested proxies: need {len(targets)}, have {len(fresh)}. "
                f"Add proxies to the pool or wait for cooldowns to expire."
            ),
            data={"needed": len(targets), "available": len(fresh)},
        )

    result = await rotator.rotate(targets)
    if result.get("ok"):
        # Re-auth on fresh IPs in the background so the request returns fast.
        asyncio.create_task(reauth_rotated_accounts(result["rotated"]))

    return ApiResponse(
        success=bool(result.get("ok")),
        message=(
            f"Rotated {len(result.get('rotated', []))} account(s); re-auth running in background"
            if result.get("ok") else f"Rotation failed: {result.get('reason')}"
        ),
        data=result,
    )


@router.post("/accounts/assign-residential")
async def assign_residential_proxies(request: Request):
    """Assign residential (sticky) proxies to accounts.

    Accepts a JSON body with a mapping of email -> residential_proxy_url.
    Proxy URLs may be in Evomi format (host:port:user:pass) or standard
    (http://user:pass@host:port) — both are accepted and normalized automatically.

    Example body:
        {
            "proxies": {
                "user1@example.com": "http://proxy.example.com:1000:user:PASS_country-NL_hardsession-XYZ",
                "user2@example.com": "http://proxy.example.com:1000:user:PASS_country-NL_hardsession-ABC"
            }
        }

    Or assign by account index (1-based) instead of email:
        {
            "proxies": [
                "http://..._hardsession-AAA",
                "http://..._hardsession-BBB"
            ]
        }
    """
    body = await request.json()
    proxies_input = body.get("proxies")
    if not proxies_input:
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message="Missing 'proxies' field in request body",
            status_code=400,
        )

    accounts = await db.get_all_accounts()
    if not accounts:
        return ApiResponse(success=True, message="No accounts to update", data={"updated": 0})

    updated = 0
    results = []
    # email -> normalized proxy (for accounts.json patch)
    email_to_norm: dict[str, str] = {}

    if isinstance(proxies_input, dict):
        # email -> proxy_url mapping
        email_to_account = {a.email: a for a in accounts}
        for email, raw_proxy in proxies_input.items():
            acc = email_to_account.get(email)
            if not acc:
                results.append({"email": email, "status": "not_found"})
                continue
            norm = normalize_proxy_url(raw_proxy)
            await db.update_account(acc.id, residential_proxy=norm)
            if _token_manager:
                # Reset cached client so next use picks up the new residential proxy
                await _token_manager.reset_client_for_proxy(acc.residential_proxy)
            email_to_norm[email] = norm or ""
            results.append({"email": email, "status": "assigned", "proxy": norm[:50] + "..." if norm and len(norm) > 50 else norm})
            updated += 1

    elif isinstance(proxies_input, list):
        # List assigned sequentially to accounts (by index)
        for i, raw_proxy in enumerate(proxies_input):
            if i >= len(accounts):
                break
            acc = accounts[i]
            norm = normalize_proxy_url(raw_proxy)
            await db.update_account(acc.id, residential_proxy=norm)
            if _token_manager:
                await _token_manager.reset_client_for_proxy(acc.residential_proxy)
            email_to_norm[acc.email] = norm or ""
            results.append({"email": acc.email, "status": "assigned", "proxy": norm[:50] + "..." if norm and len(norm) > 50 else norm})
            updated += 1
    else:
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message="'proxies' must be a dict {email: url} or list [url, ...]",
            status_code=400,
        )

    # Persist to accounts.json so the assignment survives server restarts
    if email_to_norm:
        try:
            accs_json = _read_accounts_json()
            for entry in accs_json:
                norm = email_to_norm.get(entry.get("email", ""))
                if norm is not None:
                    entry["residential_proxy"] = norm
            _write_accounts_json(accs_json)
        except Exception as _e:
            logger.warning(f"assign-residential: failed to update accounts.json: {_e}")

    logger.info(f"Assigned residential proxies to {updated}/{len(accounts)} accounts")
    return ApiResponse(
        success=True,
        message=f"Assigned residential proxies to {updated} accounts",
        data={"updated": updated, "results": results},
    )


@router.delete("/accounts/{account_id}/residential-proxy")
async def clear_residential_proxy(account_id: int):
    """Remove residential proxy from an account (falls back to datacenter/pool)."""
    account = await db.get_account(account_id)
    if not account:
        raise ApiError(
            code=ErrorCode.ACCOUNT_NOT_FOUND,
            message=f"Account {account_id} not found",
            status_code=404,
        )
    if _token_manager:
        await _token_manager.reset_client_for_proxy(account.residential_proxy)
    await db.update_account(account_id, residential_proxy=None)
    # Also clear from accounts.json so it doesn't come back on restart
    try:
        accs_json = _read_accounts_json()
        for entry in accs_json:
            if entry.get("email") == account.email:
                entry["residential_proxy"] = ""
                break
        _write_accounts_json(accs_json)
    except Exception as _e:
        logger.warning(f"clear-residential: failed to update accounts.json: {_e}")
    return ApiResponse(
        success=True,
        message=f"Residential proxy cleared for {account.email}",
        data={"account_id": account_id, "email": account.email},
    )


@router.patch("/accounts/{account_id}")
async def patch_account(account_id: int, request: Request):
    """Partially update an account. Supported fields: proxy."""
    account = await db.get_account(account_id)
    if not account:
        raise ApiError(
            code=ErrorCode.ACCOUNT_NOT_FOUND,
            message="Account not found",
            status_code=404,
        )
    body = await request.json()
    update_kwargs: dict = {}
    if "proxy" in body:
        raw = body.get("proxy") or ""
        update_kwargs["proxy"] = normalize_proxy_url(raw) if raw else None
    if not update_kwargs:
        raise ApiError(
            code=ErrorCode.VALIDATION_INVALID_PARAMS,
            message="No updatable fields provided",
            status_code=400,
        )
    old_proxy = account.proxy
    await db.update_account(account_id, **update_kwargs)
    if _token_manager and old_proxy:
        await _token_manager.reset_client_for_proxy(old_proxy)
    return ApiResponse(success=True, message=f"Account {account.email} updated")


@router.get("/accounts/{account_id}/auth-status")
async def get_auth_status(account_id: int):
    """Get authorization process status."""
    account = await db.get_account(account_id)
    if not account:
        raise ApiError(
            code=ErrorCode.ACCOUNT_NOT_FOUND,
            message="Account not found",
            status_code=404,
        )

    proc_info = _auth_processes.get(account_id)
    if not proc_info:
        return ApiResponse(
            success=True,
            data={
                "running": False,
                "exit_code": None,
                "output": "",
                "ever_started": False,
            },
        )

    return ApiResponse(
        success=True,
        data={
            "running": proc_info["running"],
            "exit_code": proc_info["exit_code"],
            "output": proc_info["output"][-5000:],  # Last 5000 chars
            "ever_started": True,
        },
    )
