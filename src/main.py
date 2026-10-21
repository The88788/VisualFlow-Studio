"""FastAPI application for VisualFlow Studio."""

import asyncio
import json
import sys
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

# Fix for Windows: enable subprocess support in asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from .api.routes import router as api_router, set_video_handler, set_token_manager, set_startup_time, reauth_all_accounts, run_single_account_auth
from .api.test_routes import router as test_router
from .core.auth import get_client_ip, verify_ip_whitelist
from .core.config import config, IMAGES_DIR, CONFIG_PATH, TMP_DIR
from .core.database import db
from .core.errors import ApiError
from .core.logger import logger
from .services.capsolver import get_capsolver
from .services.ffmpeg_service import ffmpeg_service
from .services.token_manager import TokenManager
from .services.storage import storage, LocalStorage
from .services.video_handler import VideoHandler
from .version import APP_NAME, APP_VERSION

# Global instances
token_manager: Optional[TokenManager] = None
video_handler: Optional[VideoHandler] = None
background_tasks: list[asyncio.Task] = []

# Account IDs currently being handled by proxy_rotation_monitor (proxy swap +
# re-auth through the new IP). immediate_reauth_monitor skips these so it
# doesn't fire a competing re-auth on the OLD burnt proxy mid-rotation.
rotation_pending_accounts: set[int] = set()


# ==================== Rate Limiter with TTL ====================

class RateLimiter:
    """In-memory rate limiter with automatic cleanup."""

    def __init__(self, max_requests: int = 60, window_seconds: int = 60, max_entries: int = 10000):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._max_entries = max_entries
        self._store: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.time()
        self._cleanup_interval = 60

    def _cleanup(self) -> None:
        """Remove expired entries and enforce max entries cap."""
        now = time.time()
        self._last_cleanup = now
        cutoff = now - self.window_seconds

        keys_to_delete = []
        for key, timestamps in self._store.items():
            self._store[key] = [t for t in timestamps if t > cutoff]
            if not self._store[key]:
                keys_to_delete.append(key)

        for key in keys_to_delete:
            del self._store[key]

    def _cleanup_if_needed(self) -> None:
        """Periodically cleanup old entries to prevent memory leak."""
        now = time.time()
        # Force cleanup if store exceeds max entries cap
        if len(self._store) > self._max_entries:
            self._cleanup()
            return
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._cleanup()

    def is_allowed(self, client_ip: str) -> tuple[bool, int]:
        """Check if request is allowed. Returns (allowed, retry_after_seconds)."""
        self._cleanup_if_needed()

        now = time.time()
        cutoff = now - self.window_seconds

        self._store[client_ip] = [
            t for t in self._store[client_ip] if t > cutoff
        ]

        if len(self._store[client_ip]) >= self.max_requests:
            oldest = min(self._store[client_ip])
            retry_after = int(self.window_seconds - (now - oldest)) + 1
            return False, retry_after

        self._store[client_ip].append(now)
        return True, 0


# Global rate limiter instance
rate_limiter = RateLimiter(max_requests=60, window_seconds=60)


# ==================== Background Tasks ====================

async def _supervised(coro_func, name: str, restart_delay: float = 5.0):
    """Supervisor wrapper that auto-restarts a background task on crash."""
    while True:
        try:
            await coro_func()
            # If the coroutine returns normally, it means it was cancelled or finished
            break
        except asyncio.CancelledError:
            logger.info(f"Background task '{name}' cancelled")
            raise
        except Exception as e:
            logger.error(
                f"Background task '{name}' crashed: {e}. "
                f"Restarting in {restart_delay}s..."
            )
            try:
                await asyncio.sleep(restart_delay)
            except asyncio.CancelledError:
                logger.info(f"Background task '{name}' cancelled during restart")
                raise


async def poll_generating_videos():
    """Background task to poll generating video statuses."""
    while True:
        try:
            videos = await db.get_generating_videos()

            for video in videos:
                provider = getattr(video, "provider", "veo")
                # Browser provider runs its own in-tab lifecycle. Normally we
                # skip HTTP polling for those, EXCEPT when the sniffer
                # captured a Google operation name (task_id) — then HTTP
                # polling can finish the job even if the browser session
                # died (e.g. service restart).
                if provider == "browser" and not video.task_id:
                    continue
                try:
                    await video_handler.poll_video_status(video.video_id)
                except Exception as e:
                    logger.error(
                        f"Error polling video {video.video_id}: {e}"
                    )

            gen_config = await db.get_generation_config()
            interval = (
                gen_config.get("poll_interval", 5) if gen_config else 5
            )

            await asyncio.sleep(interval)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Poll task error: {e}")
            await asyncio.sleep(5)


async def refresh_expiring_tokens():
    """Background task to refresh all tokens every 10 minutes."""
    while True:
        try:
            # Refresh every 10 minutes (600s)
            # We enforce refresh even if not expired to keep session alive
            await asyncio.sleep(600)
            await token_manager.refresh_all_expiring(force=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Token refresh task error: {e}")
            await asyncio.sleep(60)


async def cleanup_old_data():
    """Background task to cleanup old data (aggressive for low disk/RAM)."""
    while True:
        try:
            # Clean storage artifacts before DB deletion
            max_age = config.cleanup_max_age_days
            try:
                old_videos = await db.get_old_videos(max_age)
                for v in old_videos:
                    try:
                        await storage.delete_prefix(f"images/{v.video_id}")
                    except Exception as e:
                        logger.debug(f"Error cleaning images for {v.video_id}: {e}")
                    # Delete trimmed/merged video files from storage
                    if v.download_url and not v.download_url.startswith("http"):
                        try:
                            await storage.delete(f"videos/{v.download_url}")
                        except Exception as e:
                            logger.debug(f"Error cleaning video file for {v.video_id}: {e}")

                # Cleanup old videos from DB
                deleted = await db.cleanup_old_videos(max_age)
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} old videos")
            except Exception as e:
                # Video cleanup must never prevent image retention from running.
                logger.warning(f"Video cleanup failed; continuing with images: {e}")

            # Image results use the same retention period as videos. Remove
            # storage first and delete the dashboard row only after storage
            # cleanup succeeds, so the two cannot drift apart again.
            deleted_images = 0
            old_image_tasks = await db.get_old_image_tasks(max_age)
            for task in old_image_tasks:
                image_id = str(task["image_id"])
                try:
                    await storage.delete_prefix(f"images/{image_id}")
                    await storage.delete_prefix(f"references/{image_id}")
                except Exception as e:
                    logger.warning(
                        f"Could not clean stored images for {image_id}; "
                        f"keeping dashboard record: {e}"
                    )
                    continue
                deleted_images += await db.delete_image_task(image_id)
            if deleted_images > 0:
                logger.info(f"Cleaned up {deleted_images} old image tasks")

            # Cleanup old request logs (keep 7 days)
            deleted = await db.cleanup_old_logs(7)
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} old request logs")

            # Cleanup tmp files (use config setting, default 6 hours)
            tmp_hours = config.cleanup_tmp_max_hours
            await ffmpeg_service.cleanup_old_files(tmp_hours)

            # Cleanup orphaned image directories (only for local storage)
            if isinstance(storage, LocalStorage):
                videos = await db.get_videos(limit=10000)
                video_ids = {v.video_id for v in videos}

                # Reconcile records left behind by the old cleanup bug, which
                # deleted image-task directories while retaining their DB rows.
                image_tasks = await db.get_image_tasks(limit=10000)
                for task in image_tasks:
                    if task.get("status") != "completed":
                        continue
                    try:
                        result_files = json.loads(task.get("result_files") or "[]")
                    except (json.JSONDecodeError, TypeError):
                        result_files = []
                    files_exist = bool(result_files)
                    for key in result_files:
                        if not await storage.exists(str(key)):
                            files_exist = False
                            break
                    if files_exist:
                        continue
                    image_id = str(task["image_id"])
                    await storage.delete_prefix(f"images/{image_id}")
                    await db.delete_image_task(image_id)
                    logger.info(f"Removed image task with missing files: {image_id}")

                image_tasks = await db.get_image_tasks(limit=10000)
                image_ids = {str(task["image_id"]) for task in image_tasks}
                known_image_dirs = video_ids | image_ids

                for dir_path in IMAGES_DIR.iterdir():
                    if dir_path.is_dir() and dir_path.name not in known_image_dirs:
                        import shutil
                        shutil.rmtree(dir_path)
                        logger.debug(f"Cleaned up orphaned images: {dir_path}")

            # Cleanup old rotating log files in logs/10min/
            try:
                from .core.logger import ROTATING_LOG_DIR
                import time
                now = time.time()
                max_log_age = 3600  # 1 hour for rotating logs
                for log_file in ROTATING_LOG_DIR.iterdir():
                    if log_file.is_file() and log_file.name != "app.log":
                        if now - log_file.stat().st_mtime > max_log_age:
                            log_file.unlink()
                            logger.debug(f"Cleaned up old log: {log_file.name}")
            except Exception as e:
                logger.debug(f"Log cleanup error (non-critical): {e}")

            # Run once at startup, then every 2 hours.
            await asyncio.sleep(3600 * 2)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")
            await asyncio.sleep(3600)


async def scheduled_daily_reauth():
    """Background task: re-authorize all accounts daily at 05:00.

    Skipped entirely when ``provider_mode == "browser"`` — the browser path
    uses its own profile tree (``/opt/veoapi_browser_profiles/<id>_puppeteer``)
    and is refreshed manually via ``scripts/browser_login.py``. Running the
    HTTP ``setup_puppeteer.js`` flow here would overwrite the ``st`` column
    with an HTTP cookie and is simply the wrong flow for browser accounts.
    """
    from datetime import datetime as dt, timedelta

    while True:
        try:
            # Calculate seconds until next 05:00
            now = dt.now()
            target = now.replace(hour=5, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()

            logger.info(
                f"Daily reauth: next run at {target.strftime('%Y-%m-%d %H:%M')}"
                f" (in {int(wait_seconds // 3600)}h {int((wait_seconds % 3600) // 60)}m)"
            )
            await asyncio.sleep(wait_seconds)

            if config.provider_mode == "browser":
                logger.info("Daily reauth: provider_mode=browser — running Python browser_login.py for all accounts")
                result = await reauth_all_accounts()
                logger.info(
                    f"Daily reauth (browser): finished — "
                    f"{result['success']}/{result['total']} succeeded, {result['failed']} failed"
                )
                continue

            logger.info("Daily reauth: starting full re-authorization of all accounts")
            result = await reauth_all_accounts()
            logger.info(
                f"Daily reauth: finished — "
                f"{result['success']}/{result['total']} succeeded, "
                f"{result['failed']} failed"
            )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Daily reauth task error: {e}")
            # On error, retry in 1 hour instead of waiting until next 5 AM
            await asyncio.sleep(3600)


async def monitor_account_health():
    """Background task: monitor account health and re-auth if >=50% are inactive."""
    while True:
        try:
            # Check every 15 minutes
            await asyncio.sleep(900)

            accounts = await db.get_all_accounts()
            if not accounts:
                continue

            total = len(accounts)
            # Skip disabled, free-tier, and accounts in active cooldown —
            # cooldowns are timer-driven and shouldn't trigger reauth.
            _SKIP_STATUSES = {"disabled", "throttled", "resting", "rate_limited", "free_tier"}
            inactive_accounts = [
                acc for acc in accounts
                if (not acc.is_active or acc.status != "active") and acc.status not in _SKIP_STATUSES
            ]

            if len(inactive_accounts) >= total / 2.0 and total > 0:
                logger.info(f"Health Monitor: {len(inactive_accounts)}/{total} accounts inactive. Starting auto-reauth.")
                for acc in inactive_accounts:
                    logger.info(f"Health Monitor: attempting re-auth for {acc.email}")
                    try:
                        await run_single_account_auth(acc.id)
                    except Exception as e:
                        logger.error(f"Health Monitor: error re-authing {acc.email}: {e}")
                    # Throttle heavily to avoid Google bans
                    await asyncio.sleep(30)
                    
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Health monitor task error: {e}")
            await asyncio.sleep(60)


async def immediate_reauth_monitor():
    """Background task: instantly re-auth accounts that hit 'check_required' or 'needs_reauth' with a cooldown.

    When ``provider_mode == "browser"`` this monitor only flips
    ``browser_ready=False`` so the browser pool stops picking the account
    — it does NOT launch the HTTP ``setup_puppeteer.js`` flow. Re-login for
    the browser path is a manual op via ``scripts/browser_login.py``.
    """
    last_triggered = {}
    while True:
        try:
            await asyncio.sleep(10)  # Check frequently
            accounts = await db.get_all_accounts()
            if not accounts:
                continue

            import time
            now = time.time()
            browser_mode = config.provider_mode == "browser"
            for acc in accounts:
                if acc.status in ("needs_reauth", "check_required"):
                    # Skip accounts the proxy rotator is mid-swap on — it will
                    # re-auth them itself through the fresh proxy. Re-authing
                    # here would race it onto the old burnt IP.
                    if acc.id in rotation_pending_accounts:
                        continue
                    # Cooldown: 1 hour between auto-triggers for the same account to avoid infinite loops
                    if acc.id not in last_triggered or (now - last_triggered[acc.id] > 3600):
                        last_triggered[acc.id] = now
                        if browser_mode:
                            # In browser mode, trigger Python browser_login.py
                            # reauth instead of setup_puppeteer.js.
                            logger.info(
                                f"Immediate Reauth Monitor: {acc.email} status={acc.status} "
                                f"(provider_mode=browser) — launching browser re-login"
                            )
                            try:
                                await db.update_account(acc.id, browser_ready=False)
                                asyncio.create_task(run_single_account_auth(acc.id))
                            except Exception as e:
                                logger.error(
                                    f"Immediate Reauth Monitor: failed to trigger browser reauth for {acc.email}: {e}"
                                )
                            await asyncio.sleep(5)
                            continue

                        logger.info(f"Immediate Reauth Monitor: triggering re-auth for {acc.email} (status={acc.status})")
                        try:
                            # Start in background, don't wait for it
                            asyncio.create_task(run_single_account_auth(acc.id))
                        except Exception as e:
                            logger.error(f"Immediate Reauth Monitor: error re-authing {acc.email}: {e}")

                        # Small delay to avoid hammering if there are multiple accounts
                        await asyncio.sleep(5)
                        
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Immediate reauth monitor error: {e}")
            await asyncio.sleep(30)


# Account statuses that mean "Google flagged this account / its IP" — the
# signal the proxy rotator reacts to. 'resting' = the unusual-activity rest
# imposed by TokenManager after repeated PUBLIC_ERROR_UNUSUAL_ACTIVITY hits.
_PROXY_FLAGGED_STATUSES = {"needs_reauth", "check_required", "resting"}


async def reauth_rotated_accounts(rotated: list) -> None:
    """Re-auth freshly-rotated accounts through their new proxy, throttled.

    ``rotated`` is the ``ProxyRotator.rotate()`` summary list (each item has
    ``account_id`` / ``email``). The accounts are held in
    ``rotation_pending_accounts`` for the whole pass so
    immediate_reauth_monitor doesn't fire a competing re-auth on the old IP.
    """
    ids = [e["account_id"] for e in rotated]
    rotation_pending_accounts.update(ids)
    try:
        throttle = config.proxy_rotation_reauth_throttle_seconds
        for entry in rotated:
            try:
                await run_single_account_auth(entry["account_id"])
            except Exception as e:
                logger.error(f"Proxy rotation: re-auth failed for {entry['email']}: {e}")
            await asyncio.sleep(throttle)
    finally:
        rotation_pending_accounts.difference_update(ids)


async def _run_proxy_rotation(flagged, rotator) -> None:
    """Swap burnt proxies for the flagged accounts and re-auth them through
    the fresh IPs. Marks the accounts pending up front so
    immediate_reauth_monitor keeps its hands off the whole time."""
    ids = [a.id for a in flagged]
    rotation_pending_accounts.update(ids)
    try:
        result = await rotator.rotate(flagged)
        if not result.get("ok"):
            logger.warning(
                f"Proxy rotation held off: {result.get('reason')} "
                f"(needed {result.get('needed')}, had {result.get('available')})"
            )
            return
        rotated = result.get("rotated", [])
        logger.info(f"Proxy rotation: swapped {len(rotated)} account(s), re-authing on fresh IPs")
        throttle = config.proxy_rotation_reauth_throttle_seconds
        for entry in rotated:
            try:
                await run_single_account_auth(entry["account_id"])
            except Exception as e:
                logger.error(f"Proxy rotation: re-auth failed for {entry['email']}: {e}")
            await asyncio.sleep(throttle)
    finally:
        rotation_pending_accounts.difference_update(ids)


async def proxy_rotation_monitor():
    """Background task: when Google flags the whole active proxy set with
    unusual activity, swap those accounts onto rested proxies from the pool,
    cooldown the burnt ones, and re-auth on the fresh IPs.

    Gated by ``[proxy_rotation].enabled``. Trigger strictness is configurable
    via ``trigger_threshold`` (1.0 = every participating account must be
    flagged before a rotation fires)."""
    import math
    from .services.proxy_rotator import get_proxy_rotator

    rotator = get_proxy_rotator()
    while True:
        try:
            await asyncio.sleep(60)
            if not config.proxy_rotation_enabled:
                continue

            # Keep proxy_state in sync with proxy_pool.txt and release any
            # proxies whose cooldown elapsed.
            await rotator.sync()
            await rotator.tick()

            accounts = await db.get_all_accounts()
            await rotator.reconcile_assignments(accounts)

            # Anti-flapping: don't rotate again too soon after the last swap.
            min_gap = config.proxy_rotation_min_minutes_between * 60
            if rotator.last_rotation_ts and (time.time() - rotator.last_rotation_ts) < min_gap:
                continue
            # Participating = accounts that actually generate on a pinned proxy.
            participating = [
                a for a in accounts
                if a.proxy and a.status not in ("disabled", "banned", "free_tier")
            ]
            if not participating:
                continue

            flagged = [a for a in participating if a.status in _PROXY_FLAGGED_STATUSES]
            threshold = config.proxy_rotation_trigger_threshold
            needed = max(1, math.ceil(len(participating) * threshold))

            if len(flagged) >= needed:
                logger.warning(
                    f"Proxy rotation triggered: {len(flagged)}/{len(participating)} "
                    f"participating accounts flagged (threshold {threshold})"
                )
                await _run_proxy_rotation(flagged, rotator)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Proxy rotation monitor error: {e}")
            await asyncio.sleep(30)


async def sweep_browser_idle():
    """Background task: close idle per-account Chrome sessions every few minutes."""
    # Lazy import so this file is safe to load even if the browser stack isn't wired.
    from .services.providers.browser_pool import get_pool

    while True:
        try:
            await asyncio.sleep(300)  # every 5 minutes
            try:
                await get_pool().sweep_idle()
            except Exception as e:
                logger.debug(f"Browser pool sweep error: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"sweep_browser_idle loop error: {e}")
            await asyncio.sleep(60)


async def sweep_browser_artifacts():
    """Background task: delete per-video browser artifact dirs once they've
    been idle for ``browser_artifacts_max_age_hours`` (default 1 hour).

    Cleans both:
    * ``TMP_DIR/browser_videos/<video_id>/`` — input images, local mp4 copy,
      failure dumps (``_failures/*.png|.html|.url.txt``).
    * ``logs/browser_screenshots/<video_id>/`` — per-step Flow screenshots.

    Uses directory ``mtime`` as the "last activity" signal — while a
    generation is in flight, files are written into the dir, bumping mtime.
    Once generation ends, mtime stops advancing and the sweep removes the
    dir an hour later (by default). Runs every 10 minutes.

    Skipped when ``browser_keep_artifacts`` is True (debug mode).
    """
    import shutil as _shutil
    import time as _time

    while True:
        try:
            await asyncio.sleep(600)  # every 10 minutes

            if config.browser_keep_artifacts:
                continue

            max_age = config.browser_artifacts_max_age_hours * 3600
            now = _time.time()
            roots = [
                TMP_DIR / "browser_videos",
                Path(__file__).parent.parent / "logs" / "browser_screenshots",
            ]
            removed = 0
            for root in roots:
                if not root.exists() or not root.is_dir():
                    continue
                for child in root.iterdir():
                    if not child.is_dir():
                        continue
                    try:
                        age = now - child.stat().st_mtime
                    except OSError:
                        continue
                    if age <= max_age:
                        continue
                    try:
                        await asyncio.to_thread(
                            _shutil.rmtree, child, ignore_errors=True
                        )
                        removed += 1
                        logger.debug(
                            f"sweep_browser_artifacts: removed {child} "
                            f"(idle {int(age)}s)"
                        )
                    except Exception as e:
                        logger.debug(
                            f"sweep_browser_artifacts: could not remove {child}: {e}"
                        )
            if removed:
                logger.info(
                    f"sweep_browser_artifacts: removed {removed} stale artifact dir(s)"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"sweep_browser_artifacts error: {e}")
            await asyncio.sleep(60)


async def fail_stale_pending_videos():
    """Background task: fail videos that have been stuck in PENDING for too
    long. Hard SLA — if a video can't enter generating within
    ``_PENDING_SLA_SECONDS`` it gets a clear failure instead of sitting in
    the queue forever.

    Uses ``updated_at`` as the "pending since" marker. Every transition INTO
    PENDING (initial create, cold-start re-queue, UNUSUAL_ACTIVITY retry,
    etc.) bumps ``updated_at``, so a video whose ``updated_at`` is older
    than the SLA AND still has ``status='pending'`` has been stalled the
    entire window.
    """
    from datetime import datetime, timedelta
    from .core.models import VideoStatus

    try:
        _PENDING_SLA_SECONDS = max(
            60,
            int(config.get("generation", "pending_start_timeout", 600)),
        )
    except (TypeError, ValueError):
        _PENDING_SLA_SECONDS = 600
    _CHECK_INTERVAL = 30

    while True:
        try:
            await asyncio.sleep(_CHECK_INTERVAL)

            cutoff = datetime.utcnow() - timedelta(seconds=_PENDING_SLA_SECONDS)
            rows = await db.fetchall(
                """
                SELECT video_id, updated_at FROM videos
                WHERE status = 'pending' AND updated_at < ?
                """,
                (cutoff,),
            )
            if not rows:
                continue

            for row in rows:
                vid = row["video_id"]
                try:
                    age = (datetime.utcnow() - datetime.fromisoformat(str(row["updated_at"]))).total_seconds()
                except Exception:
                    age = -1
                logger.warning(
                    f"fail_stale_pending: video {vid} pending for "
                    f"{int(age)}s (>{_PENDING_SLA_SECONDS}s SLA) — failing"
                )
                try:
                    # Conditional UPDATE — only flip to FAILED if the video
                    # is still PENDING. If a worker already managed to claim
                    # it (PENDING→GENERATING) between our SELECT above and
                    # this UPDATE, leave it alone — the generation is
                    # legitimately running now. Pairs with
                    # try_claim_video_for_generation on the worker side.
                    async with db._acquire() as conn:
                        cursor = await conn.execute(
                            """
                            UPDATE videos
                            SET status = 'failed',
                                error_code = ?,
                                error_message = ?,
                                progress_message = NULL,
                                updated_at = ?
                            WHERE video_id = ? AND status = 'pending'
                            """,
                            (
                                "RATE_LIMITED",
                                f"Service is busy — could not start generation "
                                f"within {_PENDING_SLA_SECONDS // 60} minute(s). "
                                f"Please retry.",
                                datetime.utcnow(),
                                vid,
                            ),
                        )
                        if cursor.rowcount == 0:
                            logger.info(
                                f"fail_stale_pending: {vid} was claimed by a "
                                f"worker just now — leaving as-is"
                            )
                except Exception as e:
                    logger.error(f"fail_stale_pending: could not fail {vid}: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"fail_stale_pending_videos error: {e}")
            await asyncio.sleep(30)


async def fail_stale_generating_videos():
    """Background task: fail videos that have been GENERATING for too long.

    SLA: 20 minutes from PENDING→GENERATING by default. The only exception is 4K
    upscale — legitimately slow on Google's side and we don't want to
    cut it short.

    Uses the dedicated ``generating_since`` column (set in
    ``try_claim_video_for_generation``) rather than ``updated_at``,
    because polling bumps ``updated_at`` every few seconds during a
    healthy generation and would mask a stuck job.
    """
    from datetime import datetime, timedelta
    from .core.models import VideoStatus

    try:
        _GENERATING_SLA_SECONDS = max(
            60,
            int(config.get("generation", "generating_timeout", 1200)),
        )
    except (TypeError, ValueError):
        _GENERATING_SLA_SECONDS = 1200
    # Relaxed_only accounts run on the lower-priority Flow queue which
    # is legitimately slower — give them a larger watchdog budget than
    # the standard queue.
    try:
        _RELAXED_GENERATING_SLA_SECONDS = max(
            _GENERATING_SLA_SECONDS,
            int(config.get("generation", "relaxed_generating_timeout", 1800)),
        )
    except (TypeError, ValueError):
        _RELAXED_GENERATING_SLA_SECONDS = max(_GENERATING_SLA_SECONDS, 1800)
    _CHECK_INTERVAL = 30

    while True:
        try:
            await asyncio.sleep(_CHECK_INTERVAL)

            now = datetime.utcnow()
            cutoff = now - timedelta(seconds=_GENERATING_SLA_SECONDS)
            relaxed_cutoff = now - timedelta(seconds=_RELAXED_GENERATING_SLA_SECONDS)
            # Skip 4K upscales — those are allowed to take longer.
            # ``requested_resolution`` is the target ('4k' / '1080p') and
            # ``video_type='upscale'`` covers both inline auto-upscale and
            # standalone /upscale jobs. Also give relaxed_only accounts
            # the longer budget — the lower-priority queue is slower by
            # design and we'd otherwise TIMEOUT every video.
            rows = await db.fetchall(
                """
                SELECT v.video_id, v.generating_since, v.video_type,
                       v.requested_resolution, a.status AS account_status,
                       coalesce(a.force_relaxed, 0) AS account_force_relaxed
                FROM videos v
                LEFT JOIN accounts a ON a.id = v.account_id
                WHERE v.status = 'generating'
                  AND v.generating_since IS NOT NULL
                  AND NOT (
                        v.video_type = 'upscale'
                        AND lower(coalesce(v.requested_resolution, '')) = '4k'
                  )
                  AND (
                        (
                            (a.status = 'relaxed_only' OR coalesce(a.force_relaxed, 0) = 1)
                            AND v.generating_since < ?
                        )
                     OR (
                            (coalesce(a.status, '') != 'relaxed_only' AND coalesce(a.force_relaxed, 0) = 0)
                            AND v.generating_since < ?
                        )
                  )
                """,
                (relaxed_cutoff, cutoff),
            )
            if not rows:
                continue

            for row in rows:
                vid = row["video_id"]
                try:
                    age = (datetime.utcnow() - datetime.fromisoformat(str(row["generating_since"]))).total_seconds()
                except Exception:
                    age = -1
                is_relaxed = (
                    row["account_status"] == "relaxed_only"
                    or bool(row["account_force_relaxed"])
                )
                effective_sla = (
                    _RELAXED_GENERATING_SLA_SECONDS if is_relaxed else _GENERATING_SLA_SECONDS
                )
                logger.warning(
                    f"fail_stale_generating: video {vid} generating for "
                    f"{int(age)}s (>{effective_sla}s SLA, "
                    f"account_status={row['account_status']!r}, "
                    f"force_relaxed={bool(row['account_force_relaxed'])}, "
                    f"video_type={row['video_type']!r}, "
                    f"resolution={row['requested_resolution']!r}) — failing"
                )
                try:
                    # Conditional UPDATE so we don't clobber a
                    # COMPLETED that landed in the same tick.
                    async with db._acquire() as conn:
                        cursor = await conn.execute(
                            """
                            UPDATE videos
                            SET status = 'failed',
                                error_code = ?,
                                error_message = ?,
                                progress_message = NULL,
                                updated_at = ?
                            WHERE video_id = ? AND status = 'generating'
                            """,
                            (
                                "TIMEOUT",
                                f"Generation did not finish within "
                                f"{effective_sla // 60} minute(s). "
                                f"Please retry.",
                                datetime.utcnow(),
                                vid,
                            ),
                        )
                        if cursor.rowcount == 0:
                            logger.info(
                                f"fail_stale_generating: {vid} finished or "
                                f"changed state in the meantime — leaving as-is"
                            )
                except Exception as e:
                    logger.error(f"fail_stale_generating: could not fail {vid}: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"fail_stale_generating_videos error: {e}")
            await asyncio.sleep(30)


async def recover_generating_videos():
    """Recover videos that were in 'generating' status when server crashed.

    Three cases:
    - HTTP video WITH task_id  → leave as 'generating'; poll_generating_videos
      resumes polling against Google's backend, which still has the job.
      No regeneration, no RATE_LIMITED noise.
    - HTTP video WITHOUT task_id → never submitted to Google, safe to re-queue
      as 'pending' and let the worker run it from scratch.
    - Browser video → in-browser Chrome session is gone, the generation
      cannot be resumed. Fail with SERVICE_RESTARTED so the user gets a
      clear "retry me" signal instead of a misleading RATE_LIMITED.
    """
    from datetime import datetime, timedelta
    from .core.models import VideoStatus

    videos = await db.get_generating_videos()

    if not videos:
        logger.info("No generating videos to recover")
        return

    logger.info(f"Recovering {len(videos)} generating videos...")

    gen_config = await db.get_generation_config()
    video_timeout = gen_config.get("video_timeout", 1800) if gen_config else 1800

    now = datetime.utcnow()
    polling = 0
    requeued = 0
    failed = 0

    for video in videos:
        video_age = (now - video.created_at).total_seconds()

        if video_age > video_timeout * 2:
            await db.update_video(
                video.video_id,
                status=VideoStatus.FAILED.value,
                error_code="TIMEOUT",
                error_message="Video generation timed out (server restart recovery)",
            )
            logger.warning(f"Video {video.video_id} marked as failed (stuck for {int(video_age)}s)")
            failed += 1
            continue

        provider = getattr(video, "provider", "veo") or "veo"

        # If we have a task_id, Google's backend still owns the operation
        # regardless of which provider submitted it — HTTP polling resumes.
        # The sniffer in the browser provider captures the operation name
        # off the wire and persists it as task_id exactly so this works.
        if video.task_id:
            # Backfill generating_since for old videos that pre-date the
            # column or that came back from a crash without one set.
            # Without this, fail_stale_generating_videos can never reach
            # them — and we'd miss timing-out a stuck Google operation.
            if not getattr(video, "generating_since", None):
                await db.update_video(video.video_id, generating_since=video.created_at)
            logger.info(
                f"Video {video.video_id} ({provider}) keeps polling "
                f"(task_id={video.task_id[:32]}...)"
            )
            polling += 1
            continue

        # No task_id — submit never reached Google. Safe to re-queue as
        # pending and let the worker run it from scratch (whether the
        # original provider was HTTP or browser). Clear generating_since
        # so the new generation owns its own SLA timer once claimed.
        await db.update_video(
            video.video_id,
            status=VideoStatus.PENDING.value,
            account_id=None,
            progress=0,
            progress_message=None,
            error_code=None,
            error_message=None,
            generating_since=None,
        )
        logger.info(
            f"Video {video.video_id} ({provider}, no task_id) re-queued for generation"
        )
        requeued += 1

    logger.info(
        f"Recovery complete: {polling} resumed polling, "
        f"{requeued} re-queued, {failed} failed"
    )


async def recover_image_tasks():
    """Fail interrupted image runs and restart only jobs never claimed by Chrome.

    A running gflow command cannot be resumed after a container restart. We do
    not automatically submit it again because that could spend credits twice.
    Pending rows, however, are safe to process because they were never claimed.
    """
    interrupted = await db.get_image_tasks(
        limit=100, status="generating", sort_asc=True
    )
    for task in interrupted:
        await db.update_image_task(
            task["image_id"],
            status="failed",
            progress=0,
            error_code="SERVICE_RESTARTED",
            error_message=(
                "Image generation was interrupted by a server restart. "
                "Please submit it again."
            ),
        )

    pending = await db.get_image_tasks(limit=100, status="pending", sort_asc=True)
    if not pending and not interrupted:
        logger.info("No image tasks to recover")
        return

    from .services.providers.gflow_image_provider import get_gflow_image_provider

    provider = get_gflow_image_provider()
    for task in pending:
        asyncio.create_task(provider.process(task["image_id"]))
    logger.info(
        "Image recovery: %d interrupted failed, %d pending re-queued",
        len(interrupted),
        len(pending),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global token_manager, video_handler, background_tasks

    # Startup
    logger.info("Starting %s...", APP_NAME)
    set_startup_time()

    # Connect to database
    await db.connect()

    # Get capsolver if configured
    capsolver_key = config.capsolver_api_key
    capsolver = get_capsolver(capsolver_key) if capsolver_key else None

    # Get proxy config
    proxy_config = await db.get_proxy_config()
    proxy_url = None
    if proxy_config and proxy_config.get("enabled"):
        proxy_url = proxy_config.get("url")
    elif config.proxy_enabled:
        proxy_url = config.proxy_url

    # Initialize services
    token_manager = TokenManager(
        capsolver=capsolver,
        proxy=proxy_url,
    )

    video_handler = VideoHandler(token_manager, max_concurrent_videos=config.max_concurrent_videos)

    # Set handlers for router
    set_video_handler(video_handler)
    set_token_manager(token_manager)

    # Sync tokens from collected file
    await token_manager.sync_tokens_from_file(CONFIG_PATH.parent / "auth_tokens.json")

    # Sync proxy settings from accounts.json
    await token_manager.sync_accounts_from_file(CONFIG_PATH.parent / "accounts.json")

    # Re-arm cooldown timers for throttled / resting / rate_limited accounts
    # so a service restart doesn't accidentally release them early.
    await token_manager.restore_cooldown_timers()

    # Refresh tokens aggressively on startup.
    #
    # Browser and gflow modes generate via logged-in Chrome sessions (cookies),
    # not the HTTP access token. When an account proxy is a SOCKS/VPN bridge,
    # curl_cffi's token exchange can hang the full per-request timeout (120s)
    # for every account, turning startup into a multi-minute stall for a
    # token nobody uses to generate. Skip the blocking startup refresh in
    # these modes; periodic maintenance remains non-blocking.
    if config.provider_mode in ("browser", "gflow"):
        logger.info(
            "%s mode: skipping blocking startup token refresh",
            config.provider_mode,
        )
    else:
        logger.info("Initializing tokens during startup...")
        await token_manager.refresh_all_expiring(force=True)

    # Recover videos from crash
    await recover_generating_videos()
    if config.provider_mode == "gflow":
        await recover_image_tasks()

    # Re-process pending videos. Delay each one by a stagger so the browser
    # pool / token refresh has time to come online — otherwise a cold-start
    # cluster of videos hits load_balancer with an empty pool and gets a
    # misleading RATE_LIMITED.
    #
    # Browser mode needs a much longer warm-up than HTTP: launching Chrome
    # for the first account takes 10-30s, and the per-account pool init
    # serializes (one global lock during launch). 60s baseline + 5s per item
    # in browser mode covers the typical "2-4 videos queued, 1-2 accounts
    # ready" cold-start case. HTTP mode keeps the old short window.
    pending_videos = await db.get_videos(limit=100, status="pending", sort_asc=True)

    async def _delayed_reprocess(video_id: str, delay: float):
        try:
            # Bump updated_at right away so fail_stale_pending_videos
            # measures the SLA from the moment the worker picks the
            # video up, not from the moment recovery re-queued it. This
            # closes a race where the watchdog could kill a recovered
            # video that was just waiting for the cold-start delay +
            # browser pool warmup, even though a worker was about to
            # process it.
            try:
                await db.update_video(
                    video_id,
                    progress_message="Cold-start: warming up browser pool...",
                )
            except Exception:
                pass
            await asyncio.sleep(delay)
            await video_handler._process_video_limited(video_id)
        except Exception as e:  # pragma: no cover
            logger.error(f"Cold-start reprocess failed for {video_id}: {e}")

    # Stay under the 5-min pending SLA enforced by
    # fail_stale_pending_videos. Browser cold-start needs more room than
    # HTTP, but we cap the worst-case (base + per_item * idx) so even item
    # #20 has time to actually run before the watchdog fails it.
    is_browser_mode = config.provider_mode == "browser"
    base_delay = 30.0 if is_browser_mode else 10.0
    per_item = 2.0 if is_browser_mode else 0.5

    for idx, video in enumerate(pending_videos):
        delay = base_delay + idx * per_item
        asyncio.create_task(_delayed_reprocess(video.video_id, delay))
        logger.info(
            f"Re-started processing for pending video {video.video_id} "
            f"(after {delay:.0f}s warm-up delay, mode={config.provider_mode})"
        )

    # Start background tasks with auto-restart supervisor
    background_tasks = [
        asyncio.create_task(_supervised(poll_generating_videos, "poll_generating_videos")),
        asyncio.create_task(_supervised(refresh_expiring_tokens, "refresh_expiring_tokens")),
        asyncio.create_task(_supervised(cleanup_old_data, "cleanup_old_data")),
        asyncio.create_task(_supervised(scheduled_daily_reauth, "scheduled_daily_reauth")),
        asyncio.create_task(_supervised(monitor_account_health, "monitor_account_health")),
        asyncio.create_task(_supervised(immediate_reauth_monitor, "immediate_reauth_monitor")),
        asyncio.create_task(_supervised(proxy_rotation_monitor, "proxy_rotation_monitor")),
        asyncio.create_task(_supervised(sweep_browser_idle, "sweep_browser_idle")),
        asyncio.create_task(_supervised(sweep_browser_artifacts, "sweep_browser_artifacts")),
        asyncio.create_task(_supervised(fail_stale_pending_videos, "fail_stale_pending_videos")),
        asyncio.create_task(_supervised(fail_stale_generating_videos, "fail_stale_generating_videos")),
    ]

    logger.info("%s started successfully", APP_NAME)

    yield

    # Shutdown
    logger.info("Shutting down %s...", APP_NAME)

    for task in background_tasks:
        task.cancel()

    await asyncio.gather(*background_tasks, return_exceptions=True)

    # Close browser captcha if it was used
    try:
        from .services.browser_captcha import get_browser_captcha
        await get_browser_captcha().close()
    except Exception:
        pass

    # Close browser2api Chrome pool (if any sessions were spawned)
    try:
        from .services.providers.browser_pool import get_pool
        await get_pool().shutdown_all()
    except Exception as e:
        logger.warning(f"Browser pool shutdown error: {e}")

    await storage.close()
    await token_manager.close()
    await db.close()

    logger.info("%s shutdown complete", APP_NAME)


# Create FastAPI app
app = FastAPI(
    title=APP_NAME,
    description="Платформа генерации и доработки изображений и видео через Google Flow",
    version=APP_VERSION,
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# IP Whitelist middleware
@app.middleware("http")
async def ip_whitelist_middleware(request: Request, call_next):
    """Block requests from IPs not in whitelist (when enabled)."""
    # Skip for health check
    if request.url.path == "/health":
        return await call_next(request)

    if not verify_ip_whitelist(request):
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "error": {
                    "code": "IP_NOT_WHITELISTED",
                    "message": "Your IP address is not whitelisted",
                },
            },
        )

    return await call_next(request)


# Rate limiting middleware
# Paths exempt from rate limiting (read-only / polling / monitoring)
_RATE_LIMIT_EXEMPT = frozenset((
    "/api/status",
    "/api/dashboard/data",
    "/api/models",
))
_RATE_LIMIT_EXEMPT_PREFIXES = (
    "/api/videos/",   # GET polling / download — harmless reads
    "/api/images",    # GET list / polling / download — harmless reads
    "/api/accounts",  # GET account list
)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Rate limiter for API write endpoints only."""
    path = request.url.path

    if not path.startswith("/api/"):
        return await call_next(request)

    # Skip rate limiting for read-only / polling endpoints
    if path in _RATE_LIMIT_EXEMPT:
        return await call_next(request)
    if request.method == "GET" and path.startswith(_RATE_LIMIT_EXEMPT_PREFIXES):
        return await call_next(request)

    client_ip = get_client_ip(request)

    allowed, retry_after = rate_limiter.is_allowed(client_ip)

    if not allowed:
        return JSONResponse(
            status_code=429,
            content={
                "success": False,
                "error": {
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": f"Too many requests. Please retry after {retry_after} seconds.",
                },
            },
            headers={"Retry-After": str(retry_after)},
        )

    return await call_next(request)


# Request ID middleware
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a unique request ID to each request."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# Request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all API requests."""
    start_time = time.time()
    response = None
    error_message = None

    try:
        response = await call_next(request)
        return response
    except Exception as e:
        error_message = str(e)
        raise
    finally:
        duration_ms = int((time.time() - start_time) * 1000)
        request_id = getattr(request.state, "request_id", None)

        if request.url.path.startswith("/api/"):
            try:
                video_id = None
                parts = request.url.path.split("/")
                if "videos" in parts:
                    idx = parts.index("videos")
                    if idx + 1 < len(parts) and parts[idx + 1] not in (
                        "merge",
                        "download",
                    ):
                        video_id = parts[idx + 1]

                await db.log_request(
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code if response else 500,
                    client_ip=get_client_ip(request),
                    duration_ms=duration_ms,
                    video_id=video_id,
                    error_message=error_message,
                )
            except Exception:
                pass

            if duration_ms > 1000:
                logger.warning(
                    f"Slow request: {request.method} {request.url.path} "
                    f"{duration_ms}ms [rid={request_id}]"
                )


# Include routers
app.include_router(api_router)
app.include_router(test_router)


# Dashboard
@app.get("/dashboard")
async def dashboard():
    """Serve dashboard page."""
    from pathlib import Path
    from fastapi.responses import HTMLResponse
    
    html_path = Path(__file__).parent / "static" / "dashboard.html"
    html = html_path.read_text(encoding="utf-8").replace(
        "__VISUALFLOW_VERSION__", APP_VERSION
    )
    return HTMLResponse(content=html)


# Health check
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": APP_NAME, "version": APP_VERSION}


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": f"{APP_NAME} is running",
        "version": APP_VERSION,
        "docs": "/docs",
    }


# Error handlers
@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    """Handle ApiError exceptions with structured response."""
    request_id = getattr(request.state, "request_id", None)
    content = exc.to_dict()
    if request_id:
        content["error"]["requestId"] = request_id
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers={"X-Request-ID": request_id} if request_id else {},
    )


@app.exception_handler(ClientDisconnect)
async def client_disconnect_handler(request: Request, exc: ClientDisconnect):
    """Handle ClientDisconnect gracefully when client closes connection early."""
    request_id = getattr(request.state, "request_id", None)
    logger.warning(f"Client disconnected early [rid={request_id}]")
    from fastapi import Response
    return Response(status_code=499)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler."""
    request_id = getattr(request.state, "request_id", None)
    logger.exception(f"Unhandled exception [rid={request_id}]: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Internal server error",
                **({"requestId": request_id} if request_id else {}),
            }
        },
        headers={"X-Request-ID": request_id} if request_id else {},
    )
