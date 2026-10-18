"""FastAPI application for ImageAPI — image generation service."""

import asyncio
import gc
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .core.config import config, CONFIG_PATH
from .core.database import db
from .core.errors import ApiError
from .core.logger import logger
from .api.routes import router, set_image_handler, set_token_manager, set_startup_time
from .services.capsolver import get_capsolver
from .services.image_handler import ImageHandler
from .services.load_balancer import LoadBalancer
from .services.storage import close_storage
from .services.token_manager import TokenManager


# ---- Rate Limiter ----

class RateLimiter:
    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._store: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.time()

    def _cleanup(self) -> None:
        now = time.time()
        cutoff = now - self.window_seconds
        to_delete = []
        for key, timestamps in self._store.items():
            self._store[key] = [t for t in timestamps if t > cutoff]
            if not self._store[key]:
                to_delete.append(key)
        for key in to_delete:
            del self._store[key]
        self._last_cleanup = now

    def is_allowed(self, client_ip: str) -> tuple[bool, int]:
        now = time.time()
        if now - self._last_cleanup > 60:
            self._cleanup()

        cutoff = now - self.window_seconds
        self._store[client_ip] = [t for t in self._store[client_ip] if t > cutoff]

        if len(self._store[client_ip]) >= self.max_requests:
            oldest = min(self._store[client_ip])
            retry_after = int(self.window_seconds - (now - oldest)) + 1
            return False, retry_after

        self._store[client_ip].append(now)
        return True, 0


rate_limiter = RateLimiter(max_requests=60, window_seconds=60)


# ---- Background Tasks ----

async def _supervised(coro_func, name: str, restart_delay: float = 5.0):
    """Auto-restart crashed background tasks."""
    while True:
        try:
            await coro_func()
            break
        except asyncio.CancelledError:
            logger.info(f"Background task '{name}' cancelled")
            raise
        except Exception as e:
            logger.error(f"Background task '{name}' crashed: {e}. Restarting in {restart_delay}s...")
            try:
                await asyncio.sleep(restart_delay)
            except asyncio.CancelledError:
                raise


image_handler: ImageHandler = None
token_manager: TokenManager = None
background_tasks: list[asyncio.Task] = []


async def refresh_expiring_tokens():
    """Refresh tokens every 10 minutes."""
    while True:
        try:
            await asyncio.sleep(600)
            if token_manager:
                await token_manager.refresh_all_expiring(force=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            await asyncio.sleep(5)


async def cleanup_old_data():
    """Cleanup old data every 2 hours."""
    while True:
        try:
            await asyncio.sleep(7200)

            # Clean results
            if image_handler:
                await image_handler.cleanup_old_results(config.results_ttl_minutes)

            # Clean DB
            deleted = await db.cleanup_old_tasks(config.cleanup_max_age_days)
            if deleted:
                logger.info(f"Cleaned up {deleted} old tasks from DB")

            gc.collect()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            await asyncio.sleep(5)


async def monitor_account_health():
    """Monitor account health every 15 minutes. Auto-reauth if >=50% inactive."""
    while True:
        try:
            await asyncio.sleep(900)

            accounts = await db.get_all_accounts()
            if not accounts:
                continue

            total = len(accounts)
            inactive = [a for a in accounts if not a.is_active or a.status != "active"]

            if len(inactive) >= total / 2.0 and total > 0:
                logger.info(
                    f"Health Monitor: {len(inactive)}/{total} inactive. Starting auto-reauth."
                )
                from .api.routes import _run_puppeteer_auth
                for acc in inactive:
                    logger.info(f"Health Monitor: attempting re-auth for {acc.email}")
                    try:
                        await _run_puppeteer_auth(acc.id)
                    except Exception as e:
                        logger.error(f"Health Monitor: error re-authing {acc.email}: {e}")
                    await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Health monitor error: {e}")
            await asyncio.sleep(60)


async def immediate_reauth_monitor():
    """Instantly re-auth accounts with 'needs_reauth' or 'check_required' status (1h cooldown)."""
    last_triggered: dict[int, float] = {}
    while True:
        try:
            await asyncio.sleep(10)
            accounts = await db.get_all_accounts()
            if not accounts:
                continue

            now = time.time()
            for acc in accounts:
                if acc.status in ("needs_reauth", "check_required"):
                    if acc.id not in last_triggered or (now - last_triggered[acc.id] > 3600):
                        last_triggered[acc.id] = now
                        logger.info(
                            f"Immediate Reauth: triggering re-auth for {acc.email} (status={acc.status})"
                        )
                        try:
                            from .api.routes import _run_puppeteer_auth
                            asyncio.create_task(_run_puppeteer_auth(acc.id))
                        except Exception as e:
                            logger.error(f"Immediate Reauth: error for {acc.email}: {e}")
                        await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Immediate reauth monitor error: {e}")
            await asyncio.sleep(30)


async def scheduled_daily_reauth():
    """Full re-authorization of all accounts daily at 05:00 UTC."""
    from datetime import datetime as dt, timedelta

    while True:
        try:
            now = dt.utcnow()
            target = now.replace(hour=5, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()

            logger.info(
                f"Daily reauth: next run at {target.strftime('%Y-%m-%d %H:%M')} UTC"
                f" (in {int(wait_seconds // 3600)}h {int((wait_seconds % 3600) // 60)}m)"
            )
            await asyncio.sleep(wait_seconds)

            logger.info("Daily reauth: starting full re-authorization of all accounts")
            from .api.routes import _run_puppeteer_auth
            accounts = await db.get_all_accounts()
            success = 0
            failed = 0
            for acc in accounts:
                try:
                    result = await _run_puppeteer_auth(acc.id)
                    if result:
                        success += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.error(f"Daily reauth: error for {acc.email}: {e}")
                    failed += 1
                await asyncio.sleep(30)

            logger.info(
                f"Daily reauth: finished — {success}/{len(accounts)} succeeded, {failed} failed"
            )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Daily reauth task error: {e}")
            await asyncio.sleep(3600)


async def recover_stuck_tasks():
    """Recover tasks stuck in 'processing' status after server restart."""
    TASK_TIMEOUT = 300  # 5 min max for image generation

    processing = await db.get_tasks(limit=100, status="processing")
    if not processing:
        logger.info("No stuck tasks to recover")
        return

    logger.info(f"Recovering {len(processing)} stuck tasks...")
    from datetime import datetime

    now = datetime.utcnow()
    recovered = 0
    failed = 0

    for task in processing:
        if task.started_at:
            age = (now - task.started_at).total_seconds()
        else:
            age = TASK_TIMEOUT * 2  # assume old

        if age > TASK_TIMEOUT * 2:
            # Too old — mark as failed
            await db.update_task(
                task.task_id,
                status="failed",
                error_message="Task timed out (server restart recovery)",
                completed_at=now,
            )
            logger.warning(f"Task {task.task_id} marked failed (stuck for {int(age)}s)")
            failed += 1
        else:
            # Re-queue for processing
            await db.update_task(task.task_id, status="pending")
            try:
                image_handler._queue.put_nowait(task.task_id)
                recovered += 1
                logger.info(f"Task {task.task_id} re-queued for processing")
            except asyncio.QueueFull:
                await db.update_task(
                    task.task_id,
                    status="failed",
                    error_message="Queue full during recovery",
                    completed_at=now,
                )
                failed += 1

    logger.info(f"Recovery complete: {recovered} recovered, {failed} failed")


async def monitor_stuck_tasks():
    """Background task: periodically detect and recover tasks stuck in 'processing'."""
    TASK_TIMEOUT = 300  # 5 min

    while True:
        try:
            await asyncio.sleep(60)  # Check every minute
            if not image_handler:
                continue

            from datetime import datetime
            processing = await db.get_tasks(limit=50, status="processing")
            if not processing:
                continue

            now = datetime.utcnow()
            for task in processing:
                if task.started_at:
                    age = (now - task.started_at).total_seconds()
                else:
                    age = TASK_TIMEOUT + 1

                if age > TASK_TIMEOUT:
                    await db.update_task(
                        task.task_id,
                        status="failed",
                        error_message=f"Task timed out after {int(age)}s",
                        completed_at=now,
                    )
                    logger.warning(f"Stuck task {task.task_id} marked failed (age={int(age)}s)")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Stuck task monitor error: {e}")
            await asyncio.sleep(10)


# ---- IP Whitelist ----

def verify_ip_whitelist(request: Request) -> bool:
    if not config.ip_whitelist_enabled:
        return True
    client_ip = _get_client_ip(request)
    return client_ip in config.ip_whitelist_ips


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


# ---- Lifespan ----

@asynccontextmanager
async def lifespan(app: FastAPI):
    global image_handler, token_manager, background_tasks

    logger.info("Starting ImageAPI service...")
    set_startup_time()

    # Connect DB
    await db.connect()

    # Init services
    capsolver = get_capsolver(config.capsolver_api_key)
    proxy_url = config.proxy_url if config.proxy_enabled else ""

    token_manager = TokenManager(
        capsolver=capsolver,
        proxy=proxy_url or None,
    )

    load_balancer = LoadBalancer()

    image_handler = ImageHandler(
        token_manager=token_manager,
        load_balancer=load_balancer,
        max_concurrent=config.max_concurrent_tasks,
        max_queue_size=config.max_queue_size,
    )

    # Set handlers for routes
    set_image_handler(image_handler)
    set_token_manager(token_manager)

    # Sync tokens from shared files (same as veoapi2 — shared auth infrastructure)
    config_dir = CONFIG_PATH.parent
    await token_manager.sync_tokens_from_file(config_dir / "auth_tokens.json")
    await token_manager.sync_accounts_from_file(config_dir / "accounts.json")

    # Refresh tokens on startup
    await token_manager.refresh_all_expiring(force=True)

    # Start workers
    await image_handler.start_workers(count=config.max_concurrent_tasks)

    # Recover stuck tasks from previous crash
    await recover_stuck_tasks()

    # Re-queue pending tasks
    pending = await db.get_tasks(limit=100, status="pending")
    for task in pending:
        try:
            image_handler._queue.put_nowait(task.task_id)
        except asyncio.QueueFull:
            break

    # Start background tasks
    background_tasks = [
        asyncio.create_task(_supervised(refresh_expiring_tokens, "refresh_tokens")),
        asyncio.create_task(_supervised(cleanup_old_data, "cleanup")),
        asyncio.create_task(_supervised(monitor_account_health, "health_monitor")),
        asyncio.create_task(_supervised(immediate_reauth_monitor, "immediate_reauth")),
        asyncio.create_task(_supervised(scheduled_daily_reauth, "daily_reauth")),
        asyncio.create_task(_supervised(monitor_stuck_tasks, "stuck_task_monitor")),
    ]

    logger.info("ImageAPI service started successfully")

    yield

    # Shutdown
    logger.info("Shutting down ImageAPI service...")
    await image_handler.stop_workers()

    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)

    await token_manager.close()
    await close_storage()
    await db.close()
    logger.info("ImageAPI service stopped")


# ---- App ----

app = FastAPI(
    title="ImageAPI Service",
    description="Image generation API via Google Labs Flow (PINHOLE)",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def ip_whitelist_middleware(request: Request, call_next):
    if request.url.path in ("/health", "/api/v1/health"):
        return await call_next(request)
    if not verify_ip_whitelist(request):
        return JSONResponse(
            status_code=403,
            content={"success": False, "error": {"code": "FORBIDDEN", "message": "IP not whitelisted"}},
        )
    return await call_next(request)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.method == "GET":
        return await call_next(request)

    client_ip = _get_client_ip(request)
    allowed, retry_after = rate_limiter.is_allowed(client_ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"success": False, "error": {"code": "RATE_LIMITED", "message": "Too many requests"}},
            headers={"Retry-After": str(retry_after)},
        )
    return await call_next(request)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


# Root endpoints
@app.get("/")
async def root_info():
    return {
        "service": "ImageAPI",
        "version": "1.0.0",
        "description": "Image generation via Google Labs Flow (PINHOLE)",
        "docs": "/docs",
    }


@app.get("/health")
async def root_health():
    return {"status": "ok"}


@app.get("/dashboard")
async def dashboard():
    """Serve dashboard page."""
    from fastapi.responses import HTMLResponse
    html_path = Path(__file__).parent / "static" / "dashboard.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# Include API router
app.include_router(router)
