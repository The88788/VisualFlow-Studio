"""Test endpoints for experimental features."""

import time

from fastapi import APIRouter

from ..core.logger import logger
from ..core.database import db
from ..services.browser_captcha import get_browser_captcha, BrowserCaptchaError

router = APIRouter(prefix="/api/test", tags=["test"])


@router.post("/browser-captcha")
async def test_browser_captcha(
    action: str = "VIDEO_GENERATION",
    proxy: str = None,
    account_id: int = None,
):
    """Solve reCAPTCHA via headless browser (experimental).

    Returns a fresh token from grecaptcha.enterprise.execute().
    First call will take ~5-10s (browser startup), subsequent ~1-2s.

    Proxy resolution: explicit proxy param > account's proxy > global config proxy.
    """
    # Resolve proxy from account if account_id provided
    resolved_proxy = proxy
    if not resolved_proxy and account_id:
        account = await db.get_account(account_id)
        if account and account.proxy:
            resolved_proxy = account.proxy

    svc = get_browser_captcha()
    start = time.monotonic()

    try:
        token = await svc.solve(action=action, proxy=resolved_proxy)
        elapsed = int((time.monotonic() - start) * 1000)
        return {
            "success": True,
            "token": token,
            "token_length": len(token),
            "elapsed_ms": elapsed,
        }
    except BrowserCaptchaError as e:
        elapsed = int((time.monotonic() - start) * 1000)
        logger.error(f"Test browser-captcha failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "elapsed_ms": elapsed,
        }


@router.get("/browser-captcha/health")
async def browser_captcha_health():
    """Health check for browser captcha service."""
    svc = get_browser_captcha()
    return svc.status()


@router.post("/browser-captcha/restart")
async def browser_captcha_restart():
    """Force restart the browser instance."""
    svc = get_browser_captcha()
    await svc.close()
    return {"success": True, "message": "Browser closed. Next solve will re-init."}
