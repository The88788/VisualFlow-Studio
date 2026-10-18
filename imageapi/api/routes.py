"""API routes for ImageAPI (matching geminiskiyyy endpoints)."""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..core.config import CONFIG_PATH
from ..core.database import db
from ..core.errors import ApiError, ErrorCode
from ..core.logger import logger
from ..services.image_handler import ImageHandler
from ..services.token_manager import TokenManager
from .schemas import (
    AccountInfo,
    AccountsResponse,
    AspectRatioEnum,
    GenerateRequest,
    GenerateResponse,
    HealthResponse,
    ImageInfo,
    MemoryResponse,
    StatsResponse,
    TaskStatusEnum,
    TaskStatusResponse,
)

router = APIRouter(prefix="/api/v1", tags=["api"])

# Service instances (set by main.py during startup)
_image_handler: Optional[ImageHandler] = None
_token_manager: Optional[TokenManager] = None
_startup_time: float = 0

# Auth process tracking
_auth_processes: dict[int, dict] = {}

# Paths
_BASE_DIR = Path(__file__).parent.parent.parent
_ACCOUNTS_JSON = CONFIG_PATH.parent / "accounts.json"
_AUTH_TOKENS_JSON = CONFIG_PATH.parent / "auth_tokens.json"


class AddFullAccountRequest(BaseModel):
    name: str
    email: str
    password: str
    otp_secret: str = ""
    proxy: str = ""


def _read_accounts_json() -> list[dict]:
    if not _ACCOUNTS_JSON.exists():
        return []
    with open(_ACCOUNTS_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_accounts_json(accounts: list[dict]) -> None:
    _ACCOUNTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(_ACCOUNTS_JSON, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=4, ensure_ascii=False)


async def _run_puppeteer_auth(account_id: int) -> bool:
    """Run setup_puppeteer.js for an account. Returns True on success."""
    import shutil

    account = await db.get_account(account_id)
    if not account:
        return False

    script_path = _BASE_DIR / "setup_puppeteer.js"
    if not script_path.exists():
        logger.error("setup_puppeteer.js not found")
        return False

    email = account.email
    logger.info(f"Auth: starting puppeteer for {email}")

    # Clean old browser profile
    try:
        accs = _read_accounts_json()
        entry = next((a for a in accs if a.get("email") == email), None)
        if entry and entry.get("profile_path"):
            for suffix in ["", "_puppeteer"]:
                profile_dir = _BASE_DIR / f"{entry['profile_path']}{suffix}"
                if profile_dir.exists():
                    shutil.rmtree(profile_dir)
                    logger.info(f"Auth: deleted browser profile: {profile_dir}")
    except Exception as e:
        logger.warning(f"Auth: profile cleanup failed for {email}: {e}")

    # Remove old token from auth_tokens.json before re-auth
    try:
        if _AUTH_TOKENS_JSON.exists():
            tokens = json.loads(_AUTH_TOKENS_JSON.read_text(encoding="utf-8"))
            if email in tokens:
                del tokens[email]
                _AUTH_TOKENS_JSON.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
                logger.info(f"Auth: removed old token for {email} from auth_tokens.json")
    except Exception as e:
        logger.warning(f"Auth: failed to cleanup auth_tokens.json: {e}")

    _auth_processes[account_id] = {"running": True, "exit_code": None, "output": "", "email": email}

    cmd = [
        "xvfb-run", "--auto-servernum",
        "--server-args=-screen 0 1280x800x24",
        "node", str(script_path), email,
    ]

    try:
        logs_dir = _BASE_DIR / "logs"
        logs_dir.mkdir(exist_ok=True)
        log_path = logs_dir / f"imageapi_auth_{email.replace('@', '_').replace('.', '_')}.log"

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(_BASE_DIR),
        )

        output_lines = []
        with open(log_path, "w", encoding="utf-8") as log_file:
            log_file.write(f"--- Auth for {email} at {datetime.utcnow().isoformat()} ---\n")
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

        logger.info(f"Auth: {email} finished, exit_code={process.returncode}")

        if process.returncode == 0:
            try:
                tm = _require_token_manager()
                await tm.sync_tokens_from_file(_AUTH_TOKENS_JSON)
                logger.info(f"Auth: tokens synced after auth for {email}")

                updated = await db.get_account(account_id)
                if not updated or updated.st == "pending_auth":
                    await db.update_account(
                        account_id, status="check_required", is_active=False,
                        remark=(updated.remark or "") + " | Auth completed but no token found",
                    )
                    logger.warning(f"Auth: {email} finished but no token appeared — marked check_required")
                    return False

                ok = await tm.refresh_account_token(account_id, force=True)
                if ok:
                    await db.update_account(account_id, status="active", is_active=True)
                    logger.info(f"Auth: {email} — token refreshed, account enabled")
                    return True
                else:
                    await db.update_account(
                        account_id, status="check_required", is_active=False,
                        remark=(updated.remark or "") + " | Token refresh failed after auth",
                    )
                    logger.warning(f"Auth: {email} — token refresh failed after puppeteer — marked check_required")
                    return False
            except Exception as e:
                logger.error(f"Auth: post-auth setup failed for {email}: {e}")
                return False

        elif process.returncode == 11:
            await db.update_account(
                account_id, status="banned", is_active=False,
                remark="Account disabled by Google Workspace policy",
            )
            logger.error(f"Auth: {email} — BANNED by Google (exit=11)")
            return False
        else:
            await db.update_account(account_id, status="check_required", is_active=False)
            logger.warning(f"Auth: {email} — puppeteer failed (exit={process.returncode}) — marked check_required")
            return False

    except Exception as e:
        logger.error(f"Auth: {email} — error: {e}")
        _auth_processes[account_id]["running"] = False
        _auth_processes[account_id]["exit_code"] = -1
        return False


def set_image_handler(handler: ImageHandler) -> None:
    global _image_handler
    _image_handler = handler


def set_token_manager(tm: TokenManager) -> None:
    global _token_manager
    _token_manager = tm


def set_startup_time() -> None:
    global _startup_time
    _startup_time = time.time()


def _require_handler() -> ImageHandler:
    if not _image_handler:
        raise ApiError(ErrorCode.SERVICE_NOT_INITIALIZED, "Service not ready", status_code=503)
    return _image_handler


def _require_token_manager() -> TokenManager:
    if not _token_manager:
        raise ApiError(ErrorCode.SERVICE_NOT_INITIALIZED, "Service not ready", status_code=503)
    return _token_manager


# ---- Generation ----

@router.post("/generate", response_model=GenerateResponse)
async def generate_image(request: GenerateRequest):
    """Queue an image generation task."""
    handler = _require_handler()

    prompt = request.prompt.strip()
    if not prompt:
        raise ApiError(ErrorCode.VALIDATION_PROMPT_REQUIRED, "Prompt is required")

    if len(request.references) > 10:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Maximum 10 reference images", status_code=400)

    model = request.model.value
    aspect_ratio = request.aspect_ratio.value if request.aspect_ratio else None

    references = request.references if request.references else None

    try:
        task_id = await handler.submit_task(
            prompt=prompt,
            model=model,
            aspect_ratio=aspect_ratio,
            references=references,
            resolution=request.resolution,
        )
    except Exception as e:
        if "Queue full" in str(e):
            raise ApiError(ErrorCode.QUEUE_FULL, "Task queue is full, try later", status_code=429)
        raise ApiError(ErrorCode.INTERNAL_ERROR, str(e), status_code=500)

    return GenerateResponse(
        task_id=task_id,
        status=TaskStatusEnum.PENDING,
        message="Task queued successfully",
    )


@router.get("/status/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    """Get task status and results."""
    handler = _require_handler()

    task = await db.get_task(task_id)
    if not task:
        raise ApiError(ErrorCode.TASK_NOT_FOUND, "Task not found", status_code=404)

    # Return S3 URLs if available, otherwise load base64 from disk
    images = None
    image_urls = None
    if task.status == "success":
        if task.image_urls:
            try:
                image_urls = json.loads(task.image_urls)
            except Exception:
                pass
        if not image_urls:
            raw_images = await handler.get_task_images_base64(task_id)
            if raw_images:
                images = [ImageInfo(**img) for img in raw_images]

    # Get account name if available
    account_name = None
    if task.account_id:
        account = await db.get_account(task.account_id)
        if account:
            account_name = account.email

    return TaskStatusResponse(
        task_id=task.task_id,
        status=TaskStatusEnum(task.status),
        account_name=account_name,
        images=images,
        image_urls=image_urls,
        processing_time=task.processing_time,
        error=task.error_message,
        created_at=task.created_at.isoformat() if task.created_at else "",
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
    )


# ---- Health & Stats ----

@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    accounts = await db.get_all_accounts()
    active = [a for a in accounts if a.is_active and a.status == "active"]
    queue_size = _image_handler.queue_size if _image_handler else 0

    if len(active) == 0:
        status = "unhealthy"
    elif len(active) < len(accounts) / 2:
        status = "degraded"
    else:
        status = "healthy"

    return HealthResponse(
        status=status,
        accounts_active=len(active),
        accounts_total=len(accounts),
        queue_size=queue_size,
    )


@router.get("/stats", response_model=StatsResponse)
async def get_stats():
    """Comprehensive statistics."""
    pending = await db.get_task_count("pending")
    processing = await db.get_task_count("processing")
    success = await db.get_task_count("success")
    failed = await db.get_task_count("failed")
    filtered = await db.get_task_count("filtered")
    total = await db.get_task_count()

    accounts = await db.get_all_accounts()
    active = [a for a in accounts if a.is_active and a.status == "active"]

    return StatsResponse(
        total_tasks=total,
        pending=pending,
        processing=processing,
        success=success,
        failed=failed,
        filtered=filtered,
        accounts_active=len(active),
        accounts_total=len(accounts),
        queue_size=_image_handler.queue_size if _image_handler else 0,
        uptime_seconds=time.time() - _startup_time,
    )


@router.get("/memory", response_model=MemoryResponse)
async def get_memory():
    """Memory usage info."""
    import psutil
    process = psutil.Process()
    mem = process.memory_info()
    total = await db.get_task_count()
    return MemoryResponse(
        rss_mb=round(mem.rss / 1024 / 1024, 1),
        vms_mb=round(mem.vms / 1024 / 1024, 1),
        task_count=total,
    )


# ---- Account Management ----

@router.get("/accounts", response_model=AccountsResponse)
async def list_accounts():
    """List all accounts with stats."""
    accounts = await db.get_all_accounts()
    active_count = sum(1 for a in accounts if a.is_active and a.status == "active")

    result = []
    for acc in accounts:
        stats = await db.get_account_stats(acc.id)
        result.append(AccountInfo(
            id=acc.id,
            email=acc.email,
            status=acc.status,
            is_active=acc.is_active,
            credits=acc.credits,
            tier=acc.user_paygate_tier,
            proxy=acc.proxy,
            last_used_at=acc.last_used_at.isoformat() if acc.last_used_at else None,
            use_count=acc.use_count,
            total_tasks=stats.total_tasks if stats else 0,
            success_count=stats.success_count if stats else 0,
            error_count=stats.error_count if stats else 0,
            consecutive_errors=stats.consecutive_errors if stats else 0,
        ))

    return AccountsResponse(
        total=len(accounts),
        active=active_count,
        inactive=len(accounts) - active_count,
        accounts=result,
    )


@router.get("/accounts/{account_name}")
async def get_account(account_name: str):
    """Get account details by email."""
    account = await db.get_account_by_email(account_name)
    if not account:
        # Try by ID
        try:
            account = await db.get_account(int(account_name))
        except (ValueError, TypeError):
            pass
    if not account:
        raise ApiError(ErrorCode.ACCOUNT_NOT_FOUND, "Account not found", status_code=404)

    stats = await db.get_account_stats(account.id)
    return {
        "id": account.id,
        "email": account.email,
        "status": account.status,
        "is_active": account.is_active,
        "credits": account.credits,
        "tier": account.user_paygate_tier,
        "project_id": account.project_id,
        "proxy": account.proxy,
        "last_used_at": account.last_used_at.isoformat() if account.last_used_at else None,
        "use_count": account.use_count,
        "stats": {
            "total_tasks": stats.total_tasks if stats else 0,
            "success_count": stats.success_count if stats else 0,
            "error_count": stats.error_count if stats else 0,
            "consecutive_errors": stats.consecutive_errors if stats else 0,
            "today_count": stats.today_count if stats else 0,
        },
    }


@router.post("/accounts/{account_name}/activate")
async def activate_account(account_name: str, force: bool = Query(False)):
    """Activate account (return to rotation)."""
    account = await _find_account(account_name)
    await db.update_account(account.id, status="active", is_active=True)
    if force:
        await db.reset_account_failures(account.id)
    return {"success": True, "message": f"Account {account.email} activated"}


@router.post("/accounts/{account_name}/deactivate")
async def deactivate_account(account_name: str, reason: str = Query("")):
    """Deactivate account (exclude from rotation)."""
    account = await _find_account(account_name)
    status = "inactive"
    remark = reason if reason else account.remark
    await db.update_account(account.id, status=status, is_active=False, remark=remark)
    return {"success": True, "message": f"Account {account.email} deactivated"}


@router.post("/accounts/{account_name}/reset-failures")
async def reset_failures(account_name: str):
    """Reset consecutive failure counters."""
    account = await _find_account(account_name)
    await db.reset_account_failures(account.id)
    return {"success": True, "message": f"Failures reset for {account.email}"}


@router.post("/accounts/{account_name}/reset-and-activate")
async def reset_and_activate(account_name: str):
    """Reset failures and activate."""
    account = await _find_account(account_name)
    await db.reset_account_failures(account.id)
    await db.update_account(account.id, status="active", is_active=True)
    return {"success": True, "message": f"Account {account.email} reset and activated"}


@router.post("/accounts/activate-all")
async def activate_all(force: bool = Query(False)):
    """Activate all inactive accounts."""
    accounts = await db.get_all_accounts()
    activated = 0
    for acc in accounts:
        if not acc.is_active or acc.status != "active":
            if acc.status == "disabled" and not force:
                continue
            await db.update_account(acc.id, status="active", is_active=True)
            if force:
                await db.reset_account_failures(acc.id)
            activated += 1
    return {"success": True, "activated": activated, "total": len(accounts)}


@router.get("/accounts/rotation-status")
async def rotation_status():
    """Quick overview of account rotation."""
    accounts = await db.get_all_accounts()
    in_rotation = [a for a in accounts if a.is_active and a.status == "active"]
    out_rotation = [a for a in accounts if not a.is_active or a.status != "active"]
    return {
        "in_rotation": [{"email": a.email, "status": a.status} for a in in_rotation],
        "out_of_rotation": [{"email": a.email, "status": a.status} for a in out_rotation],
        "total": len(accounts),
        "in_rotation_count": len(in_rotation),
    }


# ---- Admin ----

@router.post("/accounts")
async def add_account(
    email: str = Query(...),
    st: str = Query(...),
    remark: str = Query(""),
    proxy: str = Query(""),
):
    """Add a new account."""
    tm = _require_token_manager()
    result = await tm.add_account(email=email, st=st, remark=remark, proxy=proxy)
    return {"success": True, **result}


@router.post("/accounts/{account_name}/refresh")
async def refresh_account(account_name: str):
    """Force token refresh for account."""
    tm = _require_token_manager()
    account = await _find_account(account_name)
    ok = await tm.refresh_account_token(account.id, force=True)
    return {"success": ok, "message": "Token refreshed" if ok else "Refresh failed"}


@router.post("/accounts/add-full")
async def add_full_account(request: AddFullAccountRequest):
    """Add account with email/password to accounts.json and DB, then authorize via puppeteer."""
    # Check imageapi DB
    existing = await db.get_account_by_email(request.email)
    if existing:
        raise ApiError(ErrorCode.VALIDATION_ERROR, f"Account '{request.email}' already exists in ImageAPI", status_code=400)

    # Check shared accounts.json — if email already there (e.g. from veoapi2), reuse it
    accs = _read_accounts_json()
    existing_entry = next((a for a in accs if a.get("email") == request.email), None)

    if existing_entry:
        # Email already in accounts.json (likely from veoapi2) — update fields if needed
        logger.info(f"Account {request.email} already in accounts.json (name={existing_entry.get('name')}), reusing")
        if request.proxy and not existing_entry.get("proxy"):
            existing_entry["proxy"] = request.proxy
            _write_accounts_json(accs)
    else:
        # New account — add to shared accounts.json
        accs.append({
            "name": request.name,
            "email": request.email,
            "password": request.password,
            "otp_secret": request.otp_secret,
            "profile_path": f"./browser_profiles/{request.name}",
            "proxy": request.proxy or "",
        })
        _write_accounts_json(accs)

    # Determine proxy: from request, from accounts.json, or empty (puppeteer will auto-assign from pool)
    proxy = request.proxy or (existing_entry.get("proxy", "") if existing_entry else "")

    # Add to imageapi DB with pending status
    account_id = await db.add_account(
        email=request.email,
        st="pending_auth",
        remark=f"Name: {request.name}",
        proxy=proxy,
    )
    await db.update_account(account_id, status="needs_reauth", is_active=False)

    # Check if token already exists in auth_tokens.json (account may already be authorized for veoapi2)
    try:
        if _AUTH_TOKENS_JSON.exists():
            tokens = json.loads(_AUTH_TOKENS_JSON.read_text(encoding="utf-8"))
            if request.email in tokens and tokens[request.email]:
                await db.update_account(account_id, st=tokens[request.email])
                logger.info(f"Found existing token for {request.email} in auth_tokens.json")
    except Exception as e:
        logger.warning(f"Failed to check auth_tokens.json: {e}")

    return {
        "success": True,
        "account_id": account_id,
        "email": request.email,
        "message": "Account added. Click Auth to authorize via browser.",
    }


@router.post("/accounts/{account_name}/authorize")
async def authorize_account(account_name: str):
    """Launch puppeteer authorization for an account."""
    account = await _find_account(account_name)

    if _auth_processes.get(account.id, {}).get("running"):
        return JSONResponse(status_code=409, content={
            "success": False,
            "error": {"code": "AUTH_ALREADY_RUNNING", "message": "Authorization already running"},
        })

    script_path = _BASE_DIR / "setup_puppeteer.js"
    if not script_path.exists():
        raise ApiError(ErrorCode.INTERNAL_ERROR, "setup_puppeteer.js not found", status_code=500)

    asyncio.create_task(_run_puppeteer_auth(account.id))

    return {"success": True, "message": f"Authorization started for {account.email}"}


@router.get("/accounts/{account_name}/auth-status")
async def get_auth_status(account_name: str):
    """Get current auth process status for account."""
    account = await _find_account(account_name)
    proc = _auth_processes.get(account.id)
    if not proc:
        return {"running": False, "exit_code": None, "output": ""}
    return {
        "running": proc.get("running", False),
        "exit_code": proc.get("exit_code"),
        "output": proc.get("output", "")[-2000:],  # last 2000 chars
    }


@router.delete("/accounts/{account_name}")
async def delete_account(account_name: str):
    """Delete account from imageapi DB. Shared accounts.json is NOT modified (veoapi2 may use it)."""
    account = await _find_account(account_name)

    await db.delete_account(account.id)

    # Note: we do NOT remove from accounts.json or auth_tokens.json
    # because they are shared with veoapi2. Only imageapi DB entry is removed.

    return {"success": True, "message": f"Account {account.email} deleted from ImageAPI"}


# ---- Debug ----

@router.post("/debug/reauth/{account_name}")
async def debug_reauth(account_name: str):
    """Force full re-authentication for account."""
    tm = _require_token_manager()
    account = await _find_account(account_name)
    ok = await tm.refresh_account_token(account.id, force=True)
    if ok:
        await db.reset_account_failures(account.id)
        await db.update_account(account.id, status="active", is_active=True)
    return {"success": ok, "email": account.email, "message": "Re-auth done" if ok else "Re-auth failed"}


@router.post("/debug/cleanup")
async def debug_cleanup():
    """Force cleanup of old data."""
    handler = _require_handler()
    from ..core.config import config
    cleaned_results = await handler.cleanup_old_results(config.results_ttl_minutes)
    cleaned_tasks = await db.cleanup_old_tasks(config.cleanup_max_age_days)
    return {
        "success": True,
        "cleaned_results": cleaned_results,
        "cleaned_tasks": cleaned_tasks,
    }


# ---- Dashboard ----

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


@router.get("/dashboard/data")
async def get_dashboard_data():
    """Get all dashboard data in a single request."""
    import os

    accounts = await db.get_all_accounts()
    all_stats = await db.get_all_account_stats()
    task_counts = await db.get_task_counts()

    # Uptime
    uptime_seconds = time.time() - _startup_time if _startup_time else 0
    uptime_str = _format_uptime(uptime_seconds)

    # Memory
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
        is_active = acc.is_active and acc.status == "active"
        if is_active:
            active_count += 1

        total = stats.total_tasks if stats else 0
        success = stats.success_count if stats else 0
        success_rate = round((success / total * 100), 1) if total > 0 else 0

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
            "total_tasks": total,
            "success_count": success,
            "error_count": stats.error_count if stats else 0,
            "consecutive_errors": stats.consecutive_errors if stats else 0,
            "today_count": stats.today_count if stats else 0,
            "success_rate": success_rate,
        })

    # Recent tasks
    recent_tasks = await db.get_recent_tasks(limit=25)
    tasks_data = []
    for t in recent_tasks:
        acc_email = None
        if t.account_id:
            for a in accounts:
                if a.id == t.account_id:
                    acc_email = a.email
                    break

        tasks_data.append({
            "task_id": t.task_id,
            "prompt": t.prompt[:100] if t.prompt else "",
            "model": t.model,
            "status": t.status,
            "images_count": t.images_count,
            "error_message": t.error_message,
            "account_email": acc_email,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            "processing_time": t.processing_time,
        })

    # Overall stats
    total_tasks = task_counts.get("total", 0)
    success_count = task_counts.get("success", 0)
    overall_success_rate = round((success_count / total_tasks * 100), 1) if total_tasks > 0 else 0

    return {
        "task_counts": {
            "total": total_tasks,
            "success": success_count,
            "failed": task_counts.get("failed", 0),
            "filtered": task_counts.get("filtered", 0),
            "processing": task_counts.get("processing", 0),
            "pending": task_counts.get("pending", 0),
        },
        "success_rate": overall_success_rate,
        "accounts": accounts_data,
        "active_accounts": active_count,
        "total_accounts": len(accounts),
        "recent_tasks": tasks_data,
        "memory_mb": memory_mb,
        "uptime": uptime_str,
        "uptime_seconds": uptime_seconds,
    }


# ---- Helpers ----

async def _find_account(account_name: str):
    """Find account by email or ID."""
    account = await db.get_account_by_email(account_name)
    if not account:
        try:
            account = await db.get_account(int(account_name))
        except (ValueError, TypeError):
            pass
    if not account:
        raise ApiError(ErrorCode.ACCOUNT_NOT_FOUND, "Account not found", status_code=404)
    return account
