"""Browser-based reCAPTCHA solver using Puppeteer (Node.js).

Primary captcha solver. Spawns a Node.js subprocess that runs Puppeteer
with stealth plugin to solve reCAPTCHA Enterprise tokens.

Concurrency control:
- Semaphore limits concurrent browser instances (default: 2)
- Prevents server OOM from too many Chrome processes
- Additional requests queue and wait for a slot

Retry logic (ported from flow2api):
- Node.js script retries up to 3 times with fresh browser + fingerprint
- Python wrapper tracks consecutive failures for self-healing
- On reCAPTCHA evaluation failures, the error counter resets on success

Puppeteer handles proxy auth natively via page.authenticate().
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

from ..core.logger import logger

# Path to the Node.js captcha script
SCRIPT_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "scripts" / "browser_captcha.js"
)

# Default retries in the Node.js script (per subprocess call)
# Outer retry loop is in veo_client._solve_recaptcha()
DEFAULT_RETRIES = 1

# Max concurrent Puppeteer processes (to avoid OOM)
MAX_CONCURRENT_BROWSERS = 2

# Global browser overload threshold: if active + queued >= this, new requests go to capsolver
MAX_GLOBAL_BROWSER_QUEUE = 10

# Per-account capsolver-only thresholds
_CAPSOLVER_ONLY_REQUEST_WINDOW = 5   # check last N requests
_CAPSOLVER_ONLY_403_THRESHOLD = 15   # total browser-403s across the window
_CAPSOLVER_ONLY_DURATION = 8 * 3600  # 8 hours (one shift)


class BrowserCaptchaError(Exception):
    pass


class BrowserCaptchaService:
    """Spawns Puppeteer via Node.js subprocess to solve reCAPTCHA.

    Uses a semaphore to limit concurrent Chrome instances and prevent
    server resource exhaustion.
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_BROWSERS):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._active_count = 0
        self._queue_count = 0
        self._solve_count = 0
        self._error_count = 0
        self._consecutive_failures = 0
        self._last_solve_time: Optional[float] = None

        # --- Per-account 403 tracking (capsolver-only mode) ---
        # email -> list of browser-403 counts for recent requests
        self._account_403_history: dict[str, list[int]] = {}
        # email -> capsolver-only until timestamp
        self._capsolver_only_until: dict[str, float] = {}

    async def solve(
        self,
        action: str = "VIDEO_GENERATION",
        proxy: Optional[str] = None,
        retries: int = DEFAULT_RETRIES,
        proxy_fallbacks: Optional[list[str]] = None,
    ) -> str:
        """Get a fresh reCAPTCHA token by running Puppeteer.

        Waits for a semaphore slot if max concurrent browsers are running.
        The Node.js script handles internal retries (fresh browser + fingerprint
        rotation per attempt).
        """
        self._queue_count += 1
        logger.info(
            f"BrowserCaptcha: queued (action={action}, "
            f"proxy={'yes' if proxy else 'no'}, "
            f"active={self._active_count}/{self._max_concurrent}, "
            f"waiting={self._queue_count})"
        )

        try:
            async with self._semaphore:
                self._queue_count -= 1
                self._active_count += 1
                try:
                    return await self._run_puppeteer(action, proxy, retries, proxy_fallbacks or [])
                finally:
                    self._active_count -= 1
        except BrowserCaptchaError:
            self._queue_count = max(0, self._queue_count - 1)
            raise
        except Exception:
            self._queue_count = max(0, self._queue_count - 1)
            raise

    async def _run_puppeteer(
        self, action: str, proxy: Optional[str], retries: int, proxy_fallbacks: list[str] = []
    ) -> str:
        """Actually spawn the Puppeteer subprocess."""
        cmd = [
            "node", SCRIPT_PATH,
            "--action", action,
            "--retries", str(retries),
        ]
        if proxy:
            cmd.extend(["--proxy", proxy])
        if proxy_fallbacks:
            cmd.extend(["--proxy-fallbacks", ",".join(proxy_fallbacks)])

        # CloakBrowser binary in the Node subprocess — gated by the
        # [captcha] use_cloakbrowser flag (default on). Each captcha
        # session is short (≤10s per token), so the long-session
        # detection issues we saw in the browser provider don't apply.
        # Read the config lazily to avoid circular imports at module load.
        try:
            from ..core.config import config as _cfg
            if _cfg.captcha_use_cloakbrowser:
                cmd.append("--use-cloakbrowser")
        except Exception as _e:
            logger.debug(f"BrowserCaptcha: config read failed, skipping cloak flag: {_e}")

        proxy_hint = "none"
        if proxy:
            # Show only host:port to avoid leaking credentials in logs
            try:
                from urllib.parse import urlparse
                p = urlparse(proxy)
                proxy_hint = f"{p.hostname}:{p.port}"
            except Exception:
                proxy_hint = proxy[:30]
        logger.info(
            f"BrowserCaptcha: solving (action={action}, "
            f"proxy={proxy_hint}, "
            f"active={self._active_count}/{self._max_concurrent})"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path(SCRIPT_PATH).parent.parent),
            )
            # Timeout: retries * 45s per attempt + buffer
            timeout = retries * 45 + 15
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            self._error_count += 1
            self._consecutive_failures += 1
            raise BrowserCaptchaError(
                f"Puppeteer timed out ({timeout}s, {retries} retries)"
            )
        except Exception as e:
            self._error_count += 1
            self._consecutive_failures += 1
            raise BrowserCaptchaError(f"Failed to run Puppeteer: {e}")

        output = stdout.decode("utf-8", errors="replace").strip()
        err_output = stderr.decode("utf-8", errors="replace").strip()

        if err_output:
            for line in err_output.split("\n"):
                line = line.strip()
                if line:
                    logger.debug(f"BrowserCaptcha: {line[:300]}")

        if proc.returncode != 0:
            self._error_count += 1
            self._consecutive_failures += 1
            try:
                data = json.loads(output)
                error_msg = data.get("error", output)
                attempts = data.get("attempts", "?")
            except (json.JSONDecodeError, ValueError):
                error_msg = output or err_output or f"exit code {proc.returncode}"
                attempts = "?"
            raise BrowserCaptchaError(
                f"Puppeteer failed after {attempts} attempts: {error_msg}"
            )

        # Parse result
        try:
            data = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            self._error_count += 1
            self._consecutive_failures += 1
            raise BrowserCaptchaError(
                f"Invalid JSON from Puppeteer: {output[:200]}"
            )

        if "error" in data:
            self._error_count += 1
            self._consecutive_failures += 1
            raise BrowserCaptchaError(f"Puppeteer error: {data['error']}")

        token = data.get("token")
        if not token or not isinstance(token, str):
            self._error_count += 1
            self._consecutive_failures += 1
            raise BrowserCaptchaError(f"Invalid token: {data}")

        # Success — reset consecutive failures
        self._solve_count += 1
        self._consecutive_failures = 0
        self._last_solve_time = time.time()

        attempt = data.get("attempt", 1)
        logger.info(
            f"BrowserCaptcha: token ({len(token)} chars, "
            f"action={action}, attempt={attempt}/{retries})"
        )
        return token

    # ---- Per-account capsolver-only logic ----

    def record_account_browser_403s(self, email: str, count: int) -> None:
        """Record how many browser-captcha 403s happened for one generate_video() call.

        When the last ``_CAPSOLVER_ONLY_REQUEST_WINDOW`` requests accumulate
        ``_CAPSOLVER_ONLY_403_THRESHOLD`` or more browser-403s, the account is
        switched to capsolver-only for ``_CAPSOLVER_ONLY_DURATION``.
        """
        if count <= 0:
            # Success with no 403s — still record a 0 so the sliding window moves
            pass

        history = self._account_403_history.setdefault(email, [])
        history.append(count)
        # Keep only the last N entries
        if len(history) > _CAPSOLVER_ONLY_REQUEST_WINDOW:
            self._account_403_history[email] = history[-_CAPSOLVER_ONLY_REQUEST_WINDOW:]
            history = self._account_403_history[email]

        if len(history) >= _CAPSOLVER_ONLY_REQUEST_WINDOW:
            total = sum(history)
            if total >= _CAPSOLVER_ONLY_403_THRESHOLD:
                until = time.time() + _CAPSOLVER_ONLY_DURATION
                self._capsolver_only_until[email] = until
                self._account_403_history[email] = []  # reset window
                logger.warning(
                    f"BrowserCaptcha: account {email} hit {total} browser-403s "
                    f"in last {_CAPSOLVER_ONLY_REQUEST_WINDOW} requests — "
                    f"capsolver-only for {_CAPSOLVER_ONLY_DURATION // 3600}h"
                )

    def is_capsolver_only(self, email: str) -> bool:
        """True if this account should skip browser captcha and use capsolver only."""
        until = self._capsolver_only_until.get(email, 0)
        if until > time.time():
            return True
        if until > 0:
            # Expired — clean up
            self._capsolver_only_until.pop(email, None)
        return False

    def is_overloaded(self, max_total: int = MAX_GLOBAL_BROWSER_QUEUE) -> bool:
        """True if browser queue is overloaded (active + queued >= threshold).

        When overloaded, callers should route new requests via capsolver instead
        of adding more items to the browser queue.
        """
        return (self._active_count + self._queue_count) >= max_total

    def report_error(self, error_reason: str = "") -> None:
        """Report an upstream error (e.g. 403 / reCAPTCHA evaluation failed)."""
        self._error_count += 1
        self._consecutive_failures += 1
        error_lower = (error_reason or "").lower()

        is_recaptcha = "recaptcha" in error_lower
        is_403 = "403" in error_lower or "forbidden" in error_lower

        if is_recaptcha or is_403:
            logger.warning(
                f"BrowserCaptcha: upstream reCAPTCHA/403 error "
                f"(streak={self._consecutive_failures}): {error_reason[:200]}"
            )
        else:
            logger.info(
                f"BrowserCaptcha: upstream error "
                f"(streak={self._consecutive_failures}): {error_reason[:200]}"
            )

    async def close(self) -> None:
        """No persistent state to clean up — each solve() is a fresh browser."""
        logger.info("BrowserCaptcha: closed (no-op, subprocess mode)")

    def status(self) -> dict:
        return {
            "mode": "puppeteer-subprocess",
            "script": SCRIPT_PATH,
            "script_exists": os.path.isfile(SCRIPT_PATH),
            "max_concurrent": self._max_concurrent,
            "active_browsers": self._active_count,
            "queued_requests": self._queue_count,
            "overloaded": self.is_overloaded(),
            "solve_count": self._solve_count,
            "error_count": self._error_count,
            "consecutive_failures": self._consecutive_failures,
            "last_solve_time": self._last_solve_time,
            "capsolver_only_accounts": [
                email for email, until in self._capsolver_only_until.items()
                if until > time.time()
            ],
        }


_instance: Optional[BrowserCaptchaService] = None


def get_browser_captcha() -> BrowserCaptchaService:
    global _instance
    if _instance is None:
        _instance = BrowserCaptchaService()
    return _instance
