"""Google Veo API client using curl_cffi."""

import asyncio
import base64
import json
import os
import re
import time
import random
from dataclasses import dataclass
from typing import Any, Callable, Optional

from curl_cffi.requests import AsyncSession


from ..core.logger import logger
from .capsolver import CapsolverService, CapsolverError
from .browser_captcha import BrowserCaptchaService, BrowserCaptchaError

# Per-client browser fingerprint profiles.  Each VeoClient instance picks one
# deterministically from its proxy URL so the same account always presents the
# same browser identity across requests (consistent fingerprint), while
# different accounts present different identities (diversity in the farm).
_UA_PROFILES: list[dict] = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="136", "Chromium";v="136", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"Windows"',
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="135", "Chromium";v="135", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"Windows"',
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="136", "Chromium";v="136", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"macOS"',
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="135", "Chromium";v="135", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"macOS"',
    },
    {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="136", "Chromium";v="136", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"Linux"',
    },
]


def _pick_ua_profile(proxy: Optional[str]) -> dict:
    """Select a UA profile deterministically from the proxy URL.

    Using the proxy as a seed means the same account (same proxy) always
    gets the same profile across restarts, while different accounts get
    different profiles — fingerprint consistency within an account,
    diversity across the farm.
    """
    seed = proxy or "default"
    idx = hash(seed) % len(_UA_PROFILES)
    return _UA_PROFILES[idx]


def normalize_proxy_url(proxy: Optional[str]) -> Optional[str]:
    """Convert Evomi-style host:port:user:pass to standard http://user:pass@host:port.

    Evomi Proxy Generator exports URLs in the format:
        http://core-residential.evomi.com:1000:erapro412:PASSWORD_country-NL_hardsession-XYZ
    curl_cffi expects the standard RFC format:
        http://erapro412:PASSWORD_country-NL_hardsession-XYZ@core-residential.evomi.com:1000
    """
    if not proxy or "@" in proxy:
        return proxy
    m = re.match(r"^(https?://)([^:]+):(\d+):([^:]+):(.+)$", proxy)
    if m:
        scheme, host, port, user, password = m.groups()
        return f"{scheme}{user}:{password}@{host}:{port}"
    # Format without scheme: host:port:user:pass
    m = re.match(r"^([^:]+):(\d+):([^:]+):(.+)$", proxy)
    if m:
        host, port, user, password = m.groups()
        return f"http://{user}:{password}@{host}:{port}"
    return proxy


def _is_transport_error(msg: str) -> bool:
    """True if an error string looks like a proxy/TLS/connection transport
    failure (as opposed to an HTTP-level error like 401/403).

    These are exactly the failures curl_cffi's BoringSSL hits when the proxy
    is a SOCKS/VPN bridge (``TLS connect error: invalid library``, connection
    timeouts, refused connects) — while the system ``curl`` binary negotiates
    the same proxy fine. So they're worth a one-shot fallback retry.
    """
    m = (msg or "").lower()
    needles = (
        "tls connect error", "invalid library", "timed out", "timeout",
        "could not connect", "connection refused", "connection reset",
        "failed to perform", "openssl", "curl: (7)", "curl: (28)",
        "curl: (35)", "curl: (56)", "curl: (97)",
    )
    return any(n in m for n in needles)


# Constants
VEO_BASE_URL = "https://aisandbox-pa.googleapis.com"
VEO_WEBSITE_URL = "https://labs.google"
VEO_RECAPTCHA_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

# Model mapping - veo31_fast uses fast models (ultra_relaxed suffix removed by Google ~Jun 2026)
MODEL_MAPPING = {
    # t2v
    ("veo31_fast", "t2v", "16:9"): "veo_3_1_t2v_fast",
    ("veo31_fast", "t2v", "9:16"): "veo_3_1_t2v_fast_portrait",
    # i2v
    ("veo31_fast", "i2v", "16:9"): "veo_3_1_i2v_s_fast",
    ("veo31_fast", "i2v", "9:16"): "veo_3_1_i2v_s_fast_portrait",
    ("veo31_fast", "i2v_dual", "16:9"): "veo_3_1_i2v_s_fast",
    ("veo31_fast", "i2v_dual", "9:16"): "veo_3_1_i2v_s_fast_portrait",
    # r2v
    ("veo31_fast", "r2v", "16:9"): "veo_3_1_r2v_fast_landscape",
    ("veo31_fast", "r2v", "9:16"): "veo_3_1_r2v_fast_portrait",
    ("veo31_fast", "extend", "16:9"): "veo_3_1_extend_fast_landscape",
    ("veo31_fast", "extend", "9:16"): "veo_3_1_extend_fast_portrait",
    ("veo31_fast", "insert", "16:9"): "veo_2_0_object_insertion_landscape",
    ("veo31_fast", "insert", "9:16"): "veo_2_0_object_insertion_portrait",
    ("veo31_fast", "reshoot", "16:9"): "veo_3_0_reshoot_landscape",
    ("veo31_fast", "reshoot", "9:16"): "veo_3_0_reshoot_portrait",
    ("veo31_fast", "remove", "16:9"): "veo_2_0_object_removal_landscape",
    ("veo31_fast", "remove", "9:16"): "veo_2_0_object_removal_portrait",
    # veo31_pro
    ("veo31_pro", "t2v", "16:9"): "veo_3_1_t2v",
    ("veo31_pro", "t2v", "9:16"): "veo_3_1_t2v_portrait",
    ("veo31_pro", "i2v", "16:9"): "veo_3_1_i2v_s",
    ("veo31_pro", "i2v", "9:16"): "veo_3_1_i2v_s_portrait",
    ("veo31_pro", "i2v_dual", "16:9"): "veo_3_1_i2v_s",
    ("veo31_pro", "i2v_dual", "9:16"): "veo_3_1_i2v_s_portrait",
    ("veo31_pro", "extend", "16:9"): "veo_3_1_extend",
    ("veo31_pro", "extend", "9:16"): "veo_3_1_extend_portrait",
    ("veo31_pro", "remove", "16:9"): "veo_2_0_object_removal_landscape",
    ("veo31_pro", "remove", "9:16"): "veo_2_0_object_removal_portrait",
    ("veo31_pro", "insert", "16:9"): "veo_2_0_object_insertion_landscape",
    ("veo31_pro", "insert", "9:16"): "veo_2_0_object_insertion_portrait",
    ("veo31_pro", "reshoot", "16:9"): "veo_3_0_reshoot_landscape",
    ("veo31_pro", "reshoot", "9:16"): "veo_3_0_reshoot_portrait",
    # omni_flash
    ("omni_flash", "t2v", "16:9"): "veo_3_1_t2v_omni_flash",
    ("omni_flash", "t2v", "9:16"): "veo_3_1_t2v_omni_flash_portrait",
    ("omni_flash", "r2v", "16:9"): "veo_3_1_r2v_omni_flash",
    ("omni_flash", "r2v", "9:16"): "veo_3_1_r2v_omni_flash_portrait",
    ("omni_flash", "extend", "16:9"): "veo_3_1_extend_omni_flash",
    ("omni_flash", "extend", "9:16"): "veo_3_1_extend_omni_flash_portrait",
}

# Fallback: _ultra suffix removed by Google ~Jun 2026, map to same fast models
RELAXED_TO_FAST_FALLBACK = {
    "veo_3_1_t2v_fast": "veo_3_1_t2v_fast",
    "veo_3_1_t2v_fast_portrait": "veo_3_1_t2v_fast_portrait",
    "veo_3_1_i2v_s_fast": "veo_3_1_i2v_s_fast",
    "veo_3_1_i2v_s_fast_portrait": "veo_3_1_i2v_s_fast_portrait",
    "veo_3_1_r2v_fast_landscape": "veo_3_1_r2v_fast_landscape",
    "veo_3_1_r2v_fast_portrait": "veo_3_1_r2v_fast_portrait",
    "veo_3_1_extend_fast_landscape": "veo_3_1_extend_fast_landscape",
    "veo_3_1_extend_fast_portrait": "veo_3_1_extend_fast_portrait",
}



# Model aliases
MODEL_ALIASES = {
    "veo3_fast": "veo31_fast",
    "veo3_pro": "veo31_pro",
    "veo3_quality": "veo31_pro",
    "veo2_fast": "veo31_fast",
    "sora_2": "veo31_fast",
    "sora-2": "veo31_fast",
    "omni_flash": "omni_flash",
    "omni-flash": "omni_flash",
}

# Upscaling models (from flow2api)
UPSCALE_MODELS = {
    "4k": {
        "resolution": "VIDEO_RESOLUTION_4K",
        "model_key": "veo_3_1_upsampler_4k",
    },
    "1080p": {
        "resolution": "VIDEO_RESOLUTION_1080P",
        "model_key": "veo_3_1_upsampler_1080p",
    },
}


@dataclass
class TokenInfo:
    """Token information."""

    at: str
    expires_at: float
    project_id: str
    credits: Optional[int] = None
    user_paygate_tier: Optional[str] = None


@dataclass
class GenerationResult:
    """Video generation result."""

    task_id: str
    scene_id: Optional[str] = None
    status: str = "pending"
    progress: int = 0
    download_url: Optional[str] = None
    video_url: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    cost: Optional[float] = None
    seed: Optional[int] = None
    project_id: Optional[str] = None


class VeoClientError(Exception):
    """Veo client error."""

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        retry: bool = False,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retry = retry


class VeoClient:
    """Google Veo API client."""

    def __init__(
        self,
        capsolver: Optional[CapsolverService] = None,
        proxy: Optional[str] = None,
        browser_captcha: Optional[BrowserCaptchaService] = None,
    ):
        self.capsolver = capsolver
        self.proxy = proxy
        self.browser_captcha = browser_captcha
        self.proxy_pool: list[str] = []  # fallback proxies for browser captcha nav timeout
        self._session: Optional[AsyncSession] = None
        self._ua_profile: dict = _pick_ua_profile(proxy)

    async def _get_session(self) -> AsyncSession:
        """Get or create async session."""
        if self._session is None:
            self._session = AsyncSession(
                impersonate="chrome",
                proxy=self.proxy,
                timeout=120,  # 120s default (curl_cffi default is 30s — too low for Veo API)
            )
        return self._session

    async def close(self) -> None:
        """Close the session."""
        if self._session:
            await self._session.close()
            self._session = None

    def _should_use_browser(self, account_email: str = "") -> bool:
        """Check if browser captcha should be used (not overloaded, not capsolver-only)."""
        if not self.browser_captcha:
            return False
        if account_email and self.browser_captcha.is_capsolver_only(account_email):
            logger.info(f"Browser captcha skipped for {account_email}: capsolver-only mode")
            return False
        if self.browser_captcha.is_overloaded():
            logger.info(
                f"Browser captcha skipped: overloaded "
                f"(active={self.browser_captcha._active_count}, "
                f"queued={self.browser_captcha._queue_count})"
            )
            return False
        return True

    async def _solve_recaptcha_browser(self, action: str = "VIDEO_GENERATION") -> Optional[str]:
        """Try browser captcha up to 5 times. Returns token or None."""
        if not self.browser_captcha:
            return None

        # Build fallback proxy list: other proxies from pool, excluding current one
        fallback_proxies = [p for p in self.proxy_pool if p != self.proxy]

        for attempt in range(1, 6):
            try:
                token = await self.browser_captcha.solve(
                    action=action,
                    proxy=self.proxy,
                    retries=1,
                    proxy_fallbacks=fallback_proxies,
                )
                logger.info(f"reCAPTCHA solved via browser captcha (attempt {attempt}/5)")
                return token
            except BrowserCaptchaError as e:
                logger.warning(f"Browser captcha attempt {attempt}/5 failed: {e}")
                if attempt < 5:
                    await asyncio.sleep(1)

        logger.warning("Browser captcha failed all 5 attempts")
        return None

    async def _solve_recaptcha_capsolver(self, project_id: str, action: str = "VIDEO_GENERATION") -> Optional[str]:
        """Try capsolver up to 15 times. Returns token or None.

        Passes self.proxy so Capsolver generates the token from the same IP
        as the subsequent generate_video request. IP mismatch between token
        creation and request is the primary cause of 403 / UNUSUAL_ACTIVITY.
        """
        if not self.capsolver:
            return None

        website_url = f"{VEO_WEBSITE_URL}/fx/tools/flow/project/{project_id}" if project_id else VEO_WEBSITE_URL

        for attempt in range(1, 16):
            try:
                token = await self.capsolver.solve_recaptcha_v3(
                    website_url=website_url,
                    website_key=VEO_RECAPTCHA_KEY,
                    page_action=action,
                    proxy=self.proxy,
                )
                logger.info(f"reCAPTCHA solved via Capsolver (attempt {attempt}/15)")
                return token
            except CapsolverError as e:
                logger.warning(f"Capsolver attempt {attempt}/15 failed: {e}")
                if attempt < 15:
                    await asyncio.sleep(1)

        logger.warning("Capsolver failed all 15 attempts")
        return None

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        **kwargs,
    ):
        """
        Make request with exponential backoff retry.
        
        Args:
            method: HTTP method (POST, GET, etc.)
            url: Request URL
            max_retries: Maximum retry attempts
            **kwargs: Additional request arguments
            
        Returns:
            Response object
        """
        session = await self._get_session()
        delay = 1
        last_error = None
        
        for attempt in range(max_retries):
            try:
                if method.upper() == "POST":
                    response = await session.post(url, **kwargs)
                elif method.upper() == "GET":
                    response = await session.get(url, **kwargs)
                elif method.upper() == "PUT":
                    response = await session.put(url, **kwargs)
                else:
                    response = await session.request(method, url, **kwargs)
                
                # Don't retry on client errors (4xx) except 429
                if response.status_code < 500 and response.status_code != 429:
                    return response
                    
                # Retry on 5xx or 429
                if response.status_code == 429:
                    logger.warning(f"Rate limited, attempt {attempt + 1}/{max_retries}")
                else:
                    logger.warning(f"Server error {response.status_code}, attempt {attempt + 1}/{max_retries}")
                    
            except Exception as e:
                logger.warning(f"Request error: {e}, attempt {attempt + 1}/{max_retries}")
                last_error = e
                
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2  # Exponential backoff
        
        if last_error:
            raise last_error
        return response

    def _get_headers(self, at: Optional[str] = None) -> dict[str, str]:
        """Get request headers using this client's per-account UA profile.

        x-client-data is intentionally omitted: it is a user-specific Chrome
        Variations token that is identical across all accounts in the farm —
        a clear bot signal.  x-browser-validation is also omitted for the
        same reason (single static value shared across all clients).
        """
        ua = self._ua_profile["user_agent"]
        sec_ch_ua = self._ua_profile["sec_ch_ua"]
        sec_ch_ua_platform = self._ua_profile["sec_ch_ua_platform"]
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://labs.google",
            "priority": "u=1, i",
            "referer": "https://labs.google/",
            "user-agent": ua,
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": sec_ch_ua_platform,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "x-browser-channel": "stable",
            "x-browser-copyright": "Copyright 2026 Google LLC. All Rights reserved.",
            "x-browser-year": "2026",
        }
        if at:
            headers["authorization"] = f"Bearer {at}"
        return headers

    async def _system_curl_get(self, url: str, headers: dict) -> tuple[int, str]:
        """GET ``url`` via the system ``curl`` binary through ``self.proxy``.

        Fallback transport for when curl_cffi can't complete TLS through a
        SOCKS/VPN proxy bridge. Returns ``(status_code, body)``. Loses Chrome
        impersonation, but the only endpoints routed here (NextAuth session,
        credits) are JSON APIs keyed on the cookie/bearer — not TLS-fingerprint
        gated — so plain curl is fine as a last resort.
        """
        cmd = ["curl", "-sS", "--max-time", "30"]
        if self.proxy:
            cmd += ["--proxy", self.proxy]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        cmd += ["-w", "\n__HTTPSTATUS__%{http_code}", url]

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=35)
        except FileNotFoundError as e:
            raise VeoClientError(
                f"system curl fallback unavailable: {e}",
                error_code="TOKEN_EXCHANGE_ERROR", retry=True,
            )
        except asyncio.TimeoutError:
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            raise VeoClientError(
                "system curl fallback timed out",
                error_code="TOKEN_EXCHANGE_ERROR", retry=True,
            )

        text = out.decode("utf-8", "replace")
        marker = "\n__HTTPSTATUS__"
        body, _, status_str = text.rpartition(marker)
        try:
            status = int(status_str.strip())
        except ValueError:
            status, body = 0, text
        if status == 0:
            raise VeoClientError(
                f"system curl fallback failed: {err.decode('utf-8', 'replace')[:200]}",
                error_code="TOKEN_EXCHANGE_ERROR", retry=True,
            )
        logger.info(f"[curl-fallback] GET {url} → {status}")
        return status, body

    async def _get_with_fallback(self, url: str, headers: dict) -> tuple[int, str]:
        """GET via curl_cffi (Chrome impersonation); on a transport-level
        failure through the proxy, retry once via the system ``curl`` binary.

        Returns ``(status_code, body_text)``. HTTP-level errors (401/403/…)
        propagate from curl_cffi untouched — only proxy/TLS/timeout failures
        trigger the fallback.
        """
        session = await self._get_session()
        try:
            resp = await session.get(url, headers=headers)
            return resp.status_code, resp.text
        except Exception as e:
            if not _is_transport_error(str(e)):
                raise
            logger.warning(
                f"curl_cffi GET {url} failed via proxy ({e}); "
                f"retrying through system curl fallback"
            )
            return await self._system_curl_get(url, headers)

    async def _system_curl_post(self, url: str, data: dict) -> tuple[int, str]:
        """POST ``url`` via the system ``curl`` binary through ``self.proxy``.

        Fallback for when curl_cffi can't complete TLS through a SOCKS/VPN proxy
        bridge. Posts form-encoded data. Returns ``(status_code, body)``.
        """
        from urllib.parse import urlencode
        cmd = ["curl", "-sS", "--max-time", "30"]
        if self.proxy:
            cmd += ["--proxy", self.proxy]
        cmd += ["--data", urlencode(data)]
        cmd += ["-w", "\n__HTTPSTATUS__%{http_code}", url]

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=35)
        except FileNotFoundError as e:
            raise VeoClientError(
                f"system curl fallback unavailable: {e}",
                error_code="TOKEN_EXCHANGE_ERROR", retry=True,
            )
        except asyncio.TimeoutError:
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            raise VeoClientError(
                "system curl fallback timed out",
                error_code="TOKEN_EXCHANGE_ERROR", retry=True,
            )

        text = out.decode("utf-8", "replace")
        marker = "\n__HTTPSTATUS__"
        body, _, status_str = text.rpartition(marker)
        try:
            status = int(status_str.strip())
        except ValueError:
            status, body = 0, text
        if status == 0:
            raise VeoClientError(
                f"system curl fallback failed: {err.decode('utf-8', 'replace')[:200]}",
                error_code="TOKEN_EXCHANGE_ERROR", retry=True,
            )
        logger.info(f"[curl-fallback] POST {url} → {status}")
        return status, body

    async def _post_with_fallback(self, url: str, data: dict) -> tuple[int, str]:
        """POST via curl_cffi; on transport-level failure through the proxy,
        retry once via the system ``curl`` binary.

        Returns ``(status_code, body_text)``. HTTP-level errors propagate
        untouched — only proxy/TLS/timeout failures trigger the fallback.
        """
        session = await self._get_session()
        try:
            resp = await session.post(url, data=data)
            return resp.status_code, resp.text
        except Exception as e:
            if not _is_transport_error(str(e)):
                raise
            logger.warning(
                f"curl_cffi POST {url} failed via proxy ({e}); "
                f"retrying through system curl fallback"
            )
            return await self._system_curl_post(url, data)

    async def get_credits(self, at: str) -> dict:
        """
        Get credits and userPaygateTier for an account.
        
        Args:
            at: Access token
            
        Returns:
            Dict with 'credits' and 'userPaygateTier'
        """
        session = await self._get_session()
        headers = self._get_headers(at)
        
        url = f"{VEO_BASE_URL}/v1/credits"
        
        try:
            status_code, body = await self._get_with_fallback(url, headers)

            logger.info(f"Credits API response: {status_code} - {body[:500] if body else 'empty'}")

            if status_code == 401:
                logger.warning(f"Failed to get credits: 401 UNAUTHORIZED")
                raise VeoClientError(
                    "Token unauthorized (401)",
                    error_code="UNAUTHORIZED",
                    retry=False,
                )
            
            if status_code != 200:
                logger.warning(f"Failed to get credits: {status_code}")
                # Return None for credits so we don't auto-ban for low credits on API error
                return {"credits": None, "userPaygateTier": "PAYGATE_TIER_ONE"}
            
            data = json.loads(body)
            tier = data.get("userPaygateTier", "PAYGATE_TIER_ONE")
            credits = data.get("credits") # Don't default to 0 here either
            
            logger.info(f"Got credits: tier={tier}, credits={credits}")
            
            return {
                "credits": credits,
                "userPaygateTier": tier,
            }
        except VeoClientError:
            raise
        except Exception as e:
            logger.warning(f"Failed to get credits: {e}")
            return {"credits": None, "userPaygateTier": "PAYGATE_TIER_ONE"}

    async def exchange_st_for_at(
        self, st: str
    ) -> TokenInfo:
        """
        Exchange ST (session token) for AT (access token).

        Args:
            st: Session token from Google

        Returns:
            TokenInfo with access token and project info
        """
        session = await self._get_session()
        
        # Clear any cached cookies to prevent stale session-token
        # from overriding our explicit cookie header
        session.cookies.clear()
        
        # flow2api logic: GET https://labs.google/fx/api/auth/session
        # Headers: Cookie with session token
        
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "cookie": f"__Secure-next-auth.session-token={st}",
            "user-agent": self._ua_profile["user_agent"],
        }

        try:
            # Note: /fx/api might be needed in base url
            url = "https://labs.google/fx/api/auth/session"
            
            status_code, body = await self._get_with_fallback(url, headers)

            if status_code == 401:
                raise VeoClientError(
                    "Invalid session token",
                    error_code="UNAUTHORIZED",
                    retry=False,
                )

            if status_code == 403:
                raise VeoClientError(
                    "Access forbidden",
                    error_code="FORBIDDEN",
                    retry=True,
                )

            if status_code == 429:
                raise VeoClientError(
                    "Rate limited",
                    error_code="RATE_LIMITED",
                    retry=False,
                )

            if status_code != 200:
                raise VeoClientError(
                    f"Token exchange failed: {status_code}",
                    error_code="TOKEN_EXCHANGE_FAILED",
                    retry=True,
                )

            # Response format from flow2api:
            # {
            #     "access_token": "...",
            #     "expires": "2025-...",
            #     "user": { "id": "...", ... }
            # }
            data = json.loads(body)

            at = data.get("access_token")
            # Note: flow2api generic request might map keys differently but looking at st_to_at it seems direct.
            # However, looking at standard NextAuth responses, it's usually snake_case or camelCase.
            # flow2api st_to_at comments say "access_token".
            
            if not at:
                 # Try camelCase just in case
                at = data.get("accessToken")

            if not at:
                logger.warning(f"No access token in response body: {body[:500]}")
                raise VeoClientError(
                    "No access token in response",
                    error_code="NO_ACCESS_TOKEN",
                    retry=False,
                )

            # Parse expiry
            # data['expires'] is likely ISO string "2025-11-15T04:46:04.000Z"
            expires_str = data.get("expires")
            expires_at = time.time() + 3600
            if expires_str:
                try:
                    # Simple ISO parse if possible, or just default to 1 hour
                    from datetime import datetime
                    dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                    expires_at = dt.timestamp()
                except:
                    pass

            # Project ID is not returned in auth/session usually.
            # We might need to fetch it separately or just use empty for now.
            # flow2api has create_project separately.
            # Existing code used data.get("projectId") but that was from RPC Login.
            # We'll set empty string, logic elsewhere might need to handle it.
            project_id = ""
            
            # Extract user paygate tier if available (maybe in user obj)
            user_data = data.get("user", {})
            user_id = user_data.get("id", "")
            
            # Fetch credits and userPaygateTier from API (like flow2api does)
            credits_data = await self.get_credits(at)
            credits = credits_data.get("credits", 0)
            user_paygate_tier = credits_data.get("userPaygateTier", "PAYGATE_TIER_ONE")
            
            logger.info(
                f"Token exchanged successfully (tier={user_paygate_tier}, credits={credits})"
            )

            return TokenInfo(
                at=at,
                expires_at=expires_at,
                project_id=project_id,
                credits=credits,
                user_paygate_tier=user_paygate_tier,
            )

        except VeoClientError:
            raise
        except Exception as e:
            logger.error(f"Token exchange error: {e}")
            raise VeoClientError(
                f"Token exchange failed: {e}",
                error_code="TOKEN_EXCHANGE_ERROR",
                retry=True,
            )

    async def exchange_refresh_token_for_at(self, refresh_token: str) -> "TokenInfo":
        """Exchange Google OAuth refresh_token for access_token.

        The legacy OAuth client credentials must be supplied by the operator.
        They are intentionally never embedded in the source tree.
        """
        client_id = os.environ.get("VEOAPI_GOOGLE_OAUTH_CLIENT_ID", "").strip()
        client_secret = os.environ.get("VEOAPI_GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise VeoClientError(
                "Legacy Google OAuth refresh is not configured. Set "
                "VEOAPI_GOOGLE_OAUTH_CLIENT_ID and VEOAPI_GOOGLE_OAUTH_CLIENT_SECRET.",
                error_code="OAUTH_NOT_CONFIGURED",
                retry=False,
            )

        oauth_data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        # oauth2.googleapis.com is a global endpoint — not geo-restricted and not
        # tied to the account's IP. cockpit-tools does the same (no proxy in create_client).
        # Proxies like pool.proxy.market block CONNECT tunnels to Google OAuth domains.
        direct_session = AsyncSession(impersonate="chrome", timeout=30)
        try:
            resp = await direct_session.post(
                "https://oauth2.googleapis.com/token",
                data=oauth_data,
            )
            status_code, body = resp.status_code, resp.text

            if status_code == 400:
                if "invalid_grant" in body:
                    raise VeoClientError(
                        "Refresh token revoked or expired",
                        error_code="UNAUTHORIZED",
                        retry=False,
                    )
                raise VeoClientError(
                    f"OAuth token exchange failed (400): {body[:200]}",
                    error_code="TOKEN_EXCHANGE_FAILED",
                    retry=False,
                )

            if status_code != 200:
                raise VeoClientError(
                    f"OAuth token exchange failed: {status_code}",
                    error_code="TOKEN_EXCHANGE_FAILED",
                    retry=True,
                )

            data = json.loads(body)
            at = data.get("access_token")
            if not at:
                raise VeoClientError(
                    "No access_token in OAuth response",
                    error_code="NO_ACCESS_TOKEN",
                    retry=False,
                )

            expires_at = time.time() + data.get("expires_in", 3600)

            credits_data = await self.get_credits(at)
            credits = credits_data.get("credits", 0)
            user_paygate_tier = credits_data.get("userPaygateTier", "PAYGATE_TIER_ONE")

            logger.info(
                f"OAuth refresh_token exchanged successfully "
                f"(tier={user_paygate_tier}, credits={credits})"
            )

            return TokenInfo(
                at=at,
                expires_at=expires_at,
                project_id="",
                credits=credits,
                user_paygate_tier=user_paygate_tier,
            )

        except VeoClientError:
            raise
        except Exception as e:
            logger.error(f"OAuth token exchange error: {e}")
            raise VeoClientError(
                f"OAuth token exchange failed: {e}",
                error_code="TOKEN_EXCHANGE_ERROR",
                retry=True,
            )
        finally:
            await direct_session.close()

    @staticmethod
    def _detect_image_mime_type(image_bytes: bytes) -> str:
        """Detect image MIME type from magic bytes."""
        if len(image_bytes) < 12:
            return "image/jpeg"
        if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
            return "image/webp"
        if image_bytes[:4] == b'\x89PNG':
            return "image/png"
        if image_bytes[:3] == b'\xff\xd8\xff':
            return "image/jpeg"
        if image_bytes[:6] in (b'GIF87a', b'GIF89a'):
            return "image/gif"
        if image_bytes[:2] == b'BM':
            return "image/bmp"
        return "image/jpeg"

    async def upload_image(
        self,
        at: str,
        image_data: bytes,
        aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
        request_proxy: Optional[str] = None,
    ) -> str:
        """
        Upload an image and get its URI.

        Args:
            at: Access token
            image_data: Image bytes
            aspect_ratio: Image aspect ratio (VIDEO_* will be auto-converted to IMAGE_*)

        Returns:
            Image URI/ID for use in generation
        """
        # Use datacenter pool proxy for upload (heavy traffic) if provided,
        # otherwise fall back to the account's main residential session.
        _own_session = False
        norm_rp = normalize_proxy_url(request_proxy)
        if norm_rp and norm_rp != normalize_proxy_url(self.proxy):
            session = AsyncSession(impersonate="chrome", proxy=norm_rp, timeout=120)
            _own_session = True
            logger.debug(f"upload_image: using datacenter proxy {norm_rp[:40]}...")
        else:
            session = await self._get_session()
        headers = self._get_headers(at)

        url = f"{VEO_BASE_URL}/v1:uploadUserImage"

        # Auto-convert VIDEO_* to IMAGE_* format (as flow2api does)
        if aspect_ratio.startswith("VIDEO_"):
            aspect_ratio = aspect_ratio.replace("VIDEO_", "IMAGE_")

        # Detect MIME type from magic bytes
        mime_type = self._detect_image_mime_type(image_data)

        image_base64 = base64.b64encode(image_data).decode('utf-8')

        payload = {
            "imageInput": {
                "rawImageBytes": image_base64,
                "mimeType": mime_type,
                "isUserUploaded": True,
                "aspectRatio": aspect_ratio,
            },
            "clientContext": {
                "sessionId": f";{int(time.time() * 1000)}",
                "tool": "ASSET_MANAGER",
            }
        }

        try:
            response = await session.post(
                url,
                headers=headers,
                json=payload,
            )

            if response.status_code != 200:
                # Extract Flow's actual error code from Google response
                resp_text = response.text[:500] if response.text else ""
                flow_error_code = "IMAGE_UPLOAD_FAILED"
                flow_error_message = ""
                try:
                    err_data = response.json()
                    # Deep-search for PUBLIC_ERROR in the response
                    found = self._find_public_error(err_data)
                    if found:
                        flow_error_code = found
                    elif isinstance(err_data, dict):
                        err_obj = err_data.get("error", err_data)
                        if isinstance(err_obj, dict):
                            for field in ("status", "failureReason", "code"):
                                val = str(err_obj.get(field, ""))
                                if val and val not in ("0", ""):
                                    flow_error_code = val
                                    break
                            flow_error_message = str(err_obj.get("message", ""))
                except Exception:
                    pass
                is_public_error = flow_error_code.startswith("PUBLIC_ERROR")
                logger.info(f"Image upload error response: {resp_text}")
                raise VeoClientError(
                    flow_error_message or f"Image upload failed: {response.status_code} {resp_text[:200]}",
                    error_code=flow_error_code,
                    retry=not is_public_error,
                )

            data = response.json()
            media_id = data.get("mediaGenerationId", {}).get("mediaGenerationId")
            
            if not media_id:
                 raise VeoClientError(
                    "No mediaGenerationId in response",
                    error_code="NO_MEDIA_ID",
                    retry=False,
                )
            
            # flow2api returns distinct ID. 
            # We return it directly. generate_video usage will need to handle it.
            # Previously we returned f"tapestry://image/{image_id}"
            # Let's log it.
            logger.debug(f"Image uploaded: {media_id}")
            return media_id

        except VeoClientError:
            raise
        except Exception as e:
            logger.error(f"Image upload error: {e}")
            raise VeoClientError(
                f"Image upload failed: {e}",
                error_code="IMAGE_UPLOAD_ERROR",
                retry=True,
            )
        finally:
            if _own_session:
                await session.close()

    async def create_project(self, st: str, at: Optional[str] = None, title: str = "Veo Project") -> str:
        """Create a new project."""
        session = await self._get_session()

        # flow2api uses /fx/api for TRPC calls
        url = "https://labs.google/fx/api/trpc/project.createProject"

        # ST accounts: authenticate via NextAuth session cookie.
        # OAuth accounts (refresh_token, no ST): try Authorization: Bearer instead.
        headers = self._get_headers(at if not st else None)
        if st:
            headers["cookie"] = f"__Secure-next-auth.session-token={st}"
        
        payload = {
            "json": {
                "projectTitle": title,
                "toolName": "PINHOLE"
            }
        }
        
        try:
            response = await session.post(url, headers=headers, json=payload)
            
            if response.status_code != 200:
                logger.error(f"Create project failed: {response.text}")
                return ""
                
            data = response.json()
            # Extract projectId from deep nested structure
            # result -> data -> json -> result -> projectId
            project_id = data.get("result", {}).get("data", {}).get("json", {}).get("result", {}).get("projectId")
            return project_id or ""
            
        except Exception as e:
            logger.error(f"Create project error: {e}")
            return ""

    async def delete_media(self, st: str, media_names: list[str]) -> None:
        """Delete uploaded media files from account.

        Args:
            st: Session Token
            media_names: List of media IDs to delete
        """
        if not media_names:
            return

        session = await self._get_session()
        url = "https://labs.google/fx/api/trpc/media.deleteMedia"

        headers = self._get_headers()
        headers["cookie"] = f"__Secure-next-auth.session-token={st}"

        payload = {
            "json": {
                "names": media_names
            }
        }

        try:
            response = await session.post(url, headers=headers, json=payload)
            if response.status_code == 200:
                logger.info(f"Deleted {len(media_names)} uploaded media files")
            else:
                logger.warning(f"Media delete returned {response.status_code}: {response.text[:200]}")
        except Exception as e:
            logger.warning(f"Failed to delete media: {e}")

    async def generate_video(
        self,
        at: str,
        st: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str = "16:9",
        project_id: Optional[str] = None,
        start_image_uri: Optional[str] = None,
        end_image_uri: Optional[str] = None,
        reference_image_uris: Optional[list[str]] = None,
        continuation_video_id: Optional[str] = None,
        seed: Optional[int] = None,
        user_paygate_tier: str = "PAYGATE_TIER_TWO",
        native_extend_media_id: Optional[str] = None,
        native_insert_media_id: Optional[str] = None,
        native_remove_media_id: Optional[str] = None,
        native_reshoot_media_id: Optional[str] = None,
        reshoot_motion: Optional[str] = None,
        insertion_mask_base64: Optional[str] = None,
        account_email: str = "",
        request_proxy: Optional[str] = None,
    ) -> GenerationResult:
        """
        Start video generation.

        Args:
            at: Access token
            st: Session token (for project creation)
            prompt: Text prompt
            model_key: Google model key (e.g., 'veo_3_1_t2v_fast')
            aspect_ratio: Video aspect ratio ('16:9' or '9:16')
            project_id: Project ID (required)
            start_image_uri: Starting image mediaId for I2V
            end_image_uri: Ending image mediaId for dual mode
            reference_image_uris: Reference images mediaIds for R2V
            continuation_video_id: Video ID for extend mode
            seed: Optional random seed for reproducibility
            user_paygate_tier: Paygate processing priority
            native_extend_media_id: Google mediaGenerationId for native extend mode
            native_insert_media_id: Google mediaGenerationId for native insert mode
            native_remove_media_id: Google batchId for native remove mode
            native_reshoot_media_id: Google mediaGenerationId for reshoot mode
            reshoot_motion: Optional motion string

        Returns:
            GenerationResult with task_id
        """
        import random
        import uuid

        # Normalize legacy _ultra model keys (removed by Google ~Jun 2026)
        if "_ultra" in model_key:
            normalized_key = model_key.replace("_ultra", "")
            logger.info(f"Normalizing legacy model key: {model_key} → {normalized_key}")
            model_key = normalized_key

        # Ensure we have a project ID
        if not project_id:
            project_id = await self.create_project(st, at=at)
            if not project_id:
                # ST accounts: project creation failed (network/auth issue).
                # OAuth accounts (no ST): labs.google tRPC doesn't accept AT.
                # Try the Veo API anyway with empty projectId — it may succeed
                # and return a project_id in the response that we can save to DB.
                logger.warning(
                    "Project creation failed, attempting generation with empty projectId"
                )
                project_id = ""

        # Use a one-off session with the pool proxy for this request if provided,
        # otherwise fall back to the account's cached session.
        _own_session = False
        if request_proxy and request_proxy != self.proxy:
            gen_session = AsyncSession(impersonate="chrome", proxy=request_proxy, timeout=120)
            _own_session = True
            logger.debug(f"generate_video: using pool proxy {request_proxy}")
        else:
            gen_session = await self._get_session()

        headers = self._get_headers(at)

        # Use provided seed or generate random
        actual_seed = seed if seed is not None else random.randint(1, 99999)

        # Generate session and scene IDs
        session_id = f";{int(time.time() * 1000)}"
        scene_id = str(uuid.uuid4())

        # Map aspect ratio to Google format
        video_aspect = (
            "VIDEO_ASPECT_RATIO_PORTRAIT"
            if aspect_ratio == "9:16"
            else "VIDEO_ASPECT_RATIO_LANDSCAPE"
        )

        # Solve reCAPTCHA: browser captcha first (5 attempts), unless overloaded/capsolver-only
        _use_browser = self._should_use_browser(account_email)
        _browser_403_count = 0  # track browser-sourced 403s for per-account stats

        recaptcha_token = None
        _used_browser_captcha = False
        if _use_browser:
            recaptcha_token = await self._solve_recaptcha_browser(action="VIDEO_GENERATION")
            if recaptcha_token:
                _used_browser_captcha = True
        if not recaptcha_token:
            # Browser unavailable/failed → capsolver
            recaptcha_token = await self._solve_recaptcha_capsolver(project_id, action="VIDEO_GENERATION")
        if not recaptcha_token:
            raise VeoClientError(
                "All captcha methods failed",
                error_code="CAPTCHA_FAILED",
                retry=True,
            )

        # Generate batch ID for the new API requirement to avoid watermarks
        batch_id = str(uuid.uuid4())

        # Base Request Object
        request_obj = {
            "aspectRatio": video_aspect,
            "seed": actual_seed,
            "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
            "videoModelKey": model_key,
            "metadata": {"sceneId": scene_id},
        }

        # Determine Endpoint and Mode specific payload
        url_suffix = ""

        if reference_image_uris:
            # R2V Mode
            url_suffix = "video:batchAsyncGenerateVideoReferenceImages"
            request_obj["referenceImages"] = [
                {
                    "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
                    "mediaId": uri,
                }
                for uri in reference_image_uris
            ]

        elif start_image_uri and end_image_uri:
            # I2V Start + End Mode (dual frame — uses _fl model key as-is)
            url_suffix = "video:batchAsyncGenerateVideoStartAndEndImage"
            request_obj["startImage"] = {"mediaId": start_image_uri}
            request_obj["endImage"] = {"mediaId": end_image_uri}

        elif start_image_uri:
            # I2V Start Only Mode
            url_suffix = "video:batchAsyncGenerateVideoStartImage"
            request_obj["startImage"] = {
                "mediaId": start_image_uri,
                "cropCoordinates": {"top": 0, "left": 0.0, "bottom": 1, "right": 1.0}
            }

        elif native_extend_media_id:
            # Native Extend Mode
            url_suffix = "video:batchAsyncGenerateVideoExtendVideo"
            request_obj["videoInput"] = {"mediaId": native_extend_media_id}
            # For Extend, Google optionally expects the workflowId in metadata
            request_obj["metadata"] = {"sceneId": scene_id, "workflowId": str(uuid.uuid4())}

        elif native_insert_media_id:
            # Native Insert Mode
            url_suffix = "video:batchAsyncGenerateVideoObjectInsertion"
            request_obj["videoInput"] = {"mediaId": native_insert_media_id}
            # For Insert, Google optionally expects the workflowId in metadata
            request_obj["metadata"] = {"sceneId": scene_id, "workflowId": str(uuid.uuid4())}
            
            # Submitting custom drawn mask if provided
            if insertion_mask_base64:
                clean_base64 = insertion_mask_base64
                if "," in clean_base64:
                    clean_base64 = clean_base64.split(",")[1]
                request_obj["imageMask"] = {
                    "imageBytes": clean_base64,
                    "imageUsageType": "IMAGE_USAGE_TYPE_MASK"
                }

        elif native_remove_media_id:
            # Native Remove Mode
            url_suffix = "video:batchAsyncGenerateVideoObjectRemoval"
            request_obj["videoInput"] = {"mediaId": native_remove_media_id}
            # For Remove, Google expects the workflowId in metadata
            request_obj["metadata"] = {"sceneId": scene_id, "workflowId": str(uuid.uuid4())}
            
            # Submitting custom drawn mask if provided
            if insertion_mask_base64:
                clean_base64 = insertion_mask_base64
                if "," in clean_base64:
                    clean_base64 = clean_base64.split(",")[1]
                request_obj["imageMask"] = {
                    "imageBytes": clean_base64,
                    "imageUsageType": "IMAGE_USAGE_TYPE_MASK"
                }

            # If prompt string is completely empty, send empty parts array (acceptable in Remove syntax)
            if not prompt or prompt.strip() == "":
                request_obj["textInput"]["structuredPrompt"]["parts"] = []

        elif native_reshoot_media_id:
            # Native Reshoot (Camera Motion) Mode
            url_suffix = "video:batchAsyncGenerateVideoReshootVideo"
            request_obj["videoInput"] = {"mediaId": native_reshoot_media_id}
            MOTION_MAP = {
                # Camera motion types (movement)
                "forward": "RESHOOT_MOTION_TYPE_FORWARD",
                "backward": "RESHOOT_MOTION_TYPE_BACKWARD",
                "orbit_left": "RESHOOT_MOTION_TYPE_RIGHT_TO_LEFT",
                "orbit_right": "RESHOOT_MOTION_TYPE_LEFT_TO_RIGHT",
                "orbit_up": "RESHOOT_MOTION_TYPE_UP",
                "orbit_down": "RESHOOT_MOTION_TYPE_DOWN",
                "dolly_in_zoom_out": "RESHOOT_MOTION_TYPE_DOLLY_IN_ZOOM_OUT",
                "dolly_out_zoom_in": "RESHOOT_MOTION_TYPE_DOLLY_OUT_ZOOM_IN_LARGE",
                # Camera position types (stationary)
                "center": "RESHOOT_MOTION_TYPE_STATIONARY",
                "left": "RESHOOT_MOTION_TYPE_STATIONARY_LEFT_LARGE",
                "right": "RESHOOT_MOTION_TYPE_STATIONARY_RIGHT_LARGE",
                "high": "RESHOOT_MOTION_TYPE_STATIONARY_UP",
                "low": "RESHOOT_MOTION_TYPE_STATIONARY_DOWN",
                "closer": "RESHOOT_MOTION_TYPE_STATIONARY_FORWARD",
                "further": "RESHOOT_MOTION_TYPE_STATIONARY_BACKWARD",
            }
            mapped_motion = MOTION_MAP.get(reshoot_motion, "RESHOOT_MOTION_TYPE_FORWARD")
            request_obj["reshootMotionType"] = mapped_motion
            request_obj["textInput"] = {"structuredPrompt": {"parts": []}}
            request_obj["metadata"] = {"sceneId": scene_id, "workflowId": str(uuid.uuid4())}

        else:
            # T2V Mode
            url_suffix = "video:batchAsyncGenerateVideoText"

        # Build final payload
        payload = {
            "mediaGenerationContext": {
                "batchId": batch_id,
                "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
            },
            "clientContext": {
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier,
            },
            "requests": [request_obj],
            "useV2ModelConfig": True,
        }

        if recaptcha_token:
            payload["clientContext"]["recaptchaContext"] = {
                "token": recaptcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            }

        try:
            full_url = f"{VEO_BASE_URL}/v1/{url_suffix}"

            logger.info(f"Generating video: url={full_url}")
            logger.info(f"  model_key={request_obj['videoModelKey']}, tier={user_paygate_tier}")
            logger.info(f"  payload={json.dumps(payload, indent=2)}")
            headers["content-type"] = "text/plain;charset=UTF-8"
            payload_str = json.dumps(payload, separators=(",", ":"))

            for _attempt in range(1, 4):
                try:
                    response = await gen_session.post(full_url, headers=headers, data=payload_str)
                    break
                except Exception as _exc:
                    _es = str(_exc)
                    if any(k in _es for k in ("curl: (56)", "curl: (7)", "curl: (28)", "Connection reset", "RemoteDisconnected")) and _attempt < 3:
                        logger.warning(f"Generate network error (attempt {_attempt}/3), retrying: {_es[:120]}")
                        await asyncio.sleep(1)
                    else:
                        raise

            if response.status_code == 401:
                raise VeoClientError(
                    "Unauthorized",
                    error_code="UNAUTHORIZED",
                    retry=True,
                )

            if response.status_code == 403:
                logger.warning(f"403 response body: {response.text[:500]}")
                # Count initial 403 against browser if browser token was used
                if _used_browser_captcha:
                    _browser_403_count += 1
                    if self.browser_captcha:
                        self.browser_captcha.report_error("403 Forbidden - reCAPTCHA evaluation failed")

                    # Retry with fresh browser captcha tokens (up to 5 times)
                    # Re-check overload on each attempt — stop browser retries if queue filled up
                    browser_retry_success = False
                    for browser_403_attempt in range(1, 6):
                        if not self._should_use_browser(account_email):
                            logger.warning(f"403 — browser overloaded/capsolver-only, skipping remaining browser retries")
                            break
                        logger.warning(f"403 — browser captcha retry ({browser_403_attempt}/5)...")
                        retry_token = await self._solve_recaptcha_browser(action="VIDEO_GENERATION")
                        if not retry_token:
                            continue
                        payload["clientContext"]["recaptchaContext"] = {
                            "token": retry_token,
                            "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                        }
                        payload_str = json.dumps(payload, separators=(",", ":"))
                        response = await gen_session.post(
                            full_url, headers=headers, data=payload_str,
                        )
                        if response.status_code == 200:
                            logger.info(f"Browser captcha retry succeeded on attempt {browser_403_attempt}")
                            browser_retry_success = True
                            break
                        else:
                            _browser_403_count += 1
                            logger.warning(f"Browser captcha retry attempt {browser_403_attempt} got {response.status_code}")
                            if self.browser_captcha:
                                self.browser_captcha.report_error(f"403 retry attempt {browser_403_attempt}")

                    if not browser_retry_success:
                        # Report browser 403 stats before falling back to capsolver
                        if self.browser_captcha and account_email:
                            self.browser_captcha.record_account_browser_403s(account_email, _browser_403_count)

                        # All browser retries failed — fall back to capsolver (up to 3 attempts)
                        if self.capsolver:
                            capsolver_success = False
                            for capsolver_attempt in range(1, 4):
                                logger.warning(f"Capsolver fallback attempt ({capsolver_attempt}/3)...")
                                fallback_token = await self._solve_recaptcha_capsolver(
                                    project_id, action="VIDEO_GENERATION"
                                )
                                if not fallback_token:
                                    logger.warning(f"Capsolver attempt {capsolver_attempt} failed to get token")
                                    continue
                                payload["clientContext"]["recaptchaContext"] = {
                                    "token": fallback_token,
                                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                                }
                                payload_str = json.dumps(payload, separators=(",", ":"))
                                response = await gen_session.post(
                                    full_url, headers=headers, data=payload_str,
                                )
                                if response.status_code == 200:
                                    logger.info(f"Capsolver fallback succeeded on attempt {capsolver_attempt}")
                                    capsolver_success = True
                                    break
                                else:
                                    logger.warning(f"Capsolver attempt {capsolver_attempt} got {response.status_code}")

                            if not capsolver_success:
                                raise VeoClientError(
                                    "Forbidden (all browser + capsolver retries exhausted)",
                                    error_code="FORBIDDEN",
                                    retry=True,
                                )
                        else:
                            raise VeoClientError(
                                "Forbidden (all browser captcha retries exhausted, no capsolver)",
                                error_code="FORBIDDEN",
                                retry=True,
                            )
                else:
                    if self.browser_captcha:
                        self.browser_captcha.report_error("403 Forbidden - reCAPTCHA evaluation failed")
                    raise VeoClientError(
                        "Forbidden (reCAPTCHA or permission)",
                        error_code="FORBIDDEN",
                        retry=True,
                    )

            if response.status_code == 429:
                resp_body = response.text[:2000] if response.text else ""
                logger.warning(f"429 Rate limited full response: {resp_body}")
                raise VeoClientError(
                    "Rate limited",
                    error_code="RATE_LIMITED",
                    retry=False,
                )

            if response.status_code != 200:
                # Extract Flow's actual error code from Google response
                resp_text = response.text[:500] if response.text else ""
                flow_error_code = "GENERATION_FAILED"
                flow_error_message = ""
                try:
                    err_data = response.json()
                    found = self._find_public_error(err_data)
                    if found:
                        flow_error_code = found
                    elif isinstance(err_data, dict):
                        err_obj = err_data.get("error", err_data)
                        if isinstance(err_obj, dict):
                            # Extract code: prefer "status" (e.g. "INVALID_ARGUMENT")
                            # over "message" (human-readable text)
                            for field in ("status", "failureReason", "code"):
                                val = str(err_obj.get(field, ""))
                                if val and val not in ("0", ""):
                                    flow_error_code = val
                                    break
                            flow_error_message = str(err_obj.get("message", ""))
                except Exception:
                    pass

                is_public_error = flow_error_code.startswith("PUBLIC_ERROR")
                logger.info(f"Generation error response: {resp_text}")
                raise VeoClientError(
                    flow_error_message or f"Generation failed: {response.status_code} {resp_text[:300]}",
                    error_code=flow_error_code,
                    retry=not is_public_error,
                )

            data = response.json()
            
            # Extract task ID from operations
            operations = data.get("operations", [])
            if not operations:
                 raise VeoClientError(
                    "No operations returned",
                    error_code="NO_OPERATIONS",
                    retry=False,
                )
                
            task_id = operations[0].get("operation", {}).get("name")
            if not task_id:
                raise VeoClientError(
                    "No task ID in response",
                    error_code="NO_TASK_ID",
                    retry=False,
                )

            logger.info(f"Video generation started: task_id={task_id}")

            # Record browser 403 stats (even 0 — slides the window on success)
            if self.browser_captcha and account_email:
                self.browser_captcha.record_account_browser_403s(account_email, _browser_403_count)

            return GenerationResult(
                task_id=task_id,
                scene_id=scene_id,
                status="generating",
                seed=actual_seed,
                project_id=project_id,
            )

        except VeoClientError:
            # Record browser 403 stats on error too
            if self.browser_captcha and account_email and _browser_403_count > 0:
                self.browser_captcha.record_account_browser_403s(account_email, _browser_403_count)
            raise
        except Exception as e:
            logger.error(f"Generate video error: {e}")
            raise VeoClientError(
                f"Generation failed: {e}",
                error_code="GENERATION_ERROR",
                retry=True,
            )
        finally:
            if _own_session:
                try:
                    await gen_session.close()
                except Exception:
                    pass

    @staticmethod
    def _find_public_error(blob: Any) -> Optional[str]:
        """Recursively search a dict/list for a PUBLIC_ERROR_* string."""
        if isinstance(blob, str):
            if "PUBLIC_ERROR" in blob:
                m = re.search(r'(PUBLIC_ERROR\w+)', blob)
                return m.group(1) if m else None
            return None
        if isinstance(blob, dict):
            for v in blob.values():
                found = VeoClient._find_public_error(v)
                if found:
                    return found
        if isinstance(blob, list):
            for item in blob:
                found = VeoClient._find_public_error(item)
                if found:
                    return found
        return None

    @staticmethod
    def _extract_flow_error(op_data: dict) -> tuple[str, str]:
        """Extract the original Flow/Google PUBLIC_ERROR code from a failed operation.

        Searches multiple known locations in Google's response:
          - op_data.error.message / error.code
          - op_data.operation.metadata.failureReason
          - op_data.operation.metadata.error.code / .message
          - op_data.operation.metadata.generatedVideos[*].video.failureReason
          - Deep recursive scan of the entire payload as last resort

        Returns:
            (error_code, error_message)
        """
        error_info = op_data.get("error", {}) or {}
        op_obj = op_data.get("operation", {}) or {}
        op_meta = op_obj.get("metadata", {}) or {}
        # Google often puts the error inside operation.error (gRPC style)
        op_error = op_obj.get("error", {}) or {}

        raw_code = str(error_info.get("code", "") or op_error.get("code", ""))
        raw_message = str(
            error_info.get("message", "") or op_error.get("message", "")
        )

        # Ordered list of candidate fields that may contain PUBLIC_ERROR
        candidates: list[str] = []

        # 1) operation.error.message (most common for gRPC-style responses)
        op_err_msg = str(op_error.get("message", ""))
        if op_err_msg:
            candidates.append(op_err_msg)

        # 2) metadata.failureReason (common for Flow)
        fr = op_meta.get("failureReason", "")
        if fr:
            candidates.append(str(fr))

        # 3) metadata.publicErrorCode
        pec = op_meta.get("publicErrorCode", "")
        if pec:
            candidates.append(str(pec))

        # 4) top-level error.message, error.code
        top_msg = str(error_info.get("message", ""))
        top_code = str(error_info.get("code", ""))
        if top_msg and top_msg not in candidates:
            candidates.append(top_msg)
        if top_code:
            candidates.append(top_code)

        # 4) metadata.error.code / message
        meta_err = op_meta.get("error", {})
        if isinstance(meta_err, dict):
            for field in ("code", "message", "failureReason"):
                v = meta_err.get(field, "")
                if v:
                    candidates.append(str(v))
        elif meta_err:
            candidates.append(str(meta_err))

        # 5) generatedVideos[*].video.failureReason / .error
        for gv in op_meta.get("generatedVideos", []):
            vid = gv.get("video", {}) or {}
            for field in ("failureReason", "publicErrorCode", "error"):
                v = vid.get(field, "")
                if v:
                    candidates.append(str(v))

        # 6) error.details[*] (gRPC style)
        for detail in error_info.get("details", []):
            if isinstance(detail, dict):
                for v in detail.values():
                    if v:
                        candidates.append(str(v))

        # Search candidates for PUBLIC_ERROR
        for candidate in candidates:
            if "PUBLIC_ERROR" in candidate:
                m = re.search(r'(PUBLIC_ERROR\w+)', candidate)
                if m:
                    code = m.group(1)
                    # Build the best message: prefer text that differs
                    # from the bare code, otherwise use the full candidate.
                    if candidate != code:
                        msg = candidate
                    elif raw_message and raw_message != code:
                        msg = raw_message
                    else:
                        msg = code
                    return code, msg

        # No PUBLIC_ERROR found in known fields — deep scan entire payload
        deep = VeoClient._find_public_error(op_data)
        if deep:
            msg = (
                raw_message if raw_message and raw_message != deep
                else (candidates[0] if candidates and candidates[0] != deep else deep)
            )
            return deep, msg

        # Last resort: return the best non-empty candidate or generic fallback
        # Prefer non-numeric codes
        for candidate in candidates:
            stripped = candidate.strip()
            if stripped and not stripped.isdigit() and stripped != "0":
                return stripped, raw_message or "Video generation failed"

        return "GENERATION_FAILED", raw_message or "Video generation failed"

    async def get_video_status(
        self, at: str, task_id: str, request_proxy: Optional[str] = None
    ) -> GenerationResult:
        """
        Get video generation status.

        Args:
            at: Access token
            task_id: Task ID directly (e.g. "projects/.../operations/...")
            request_proxy: Optional datacenter proxy for polling (saves residential traffic)

        Returns:
            GenerationResult with current status
        """
        # Use datacenter proxy for polling if provided (saves residential bandwidth).
        _own_session = False
        norm_rp = normalize_proxy_url(request_proxy)
        if norm_rp and norm_rp != normalize_proxy_url(self.proxy):
            session = AsyncSession(impersonate="chrome", proxy=norm_rp, timeout=120)
            _own_session = True
        else:
            session = await self._get_session()
        headers = self._get_headers(at)

        url = f"{VEO_BASE_URL}/v1/video:batchCheckAsyncVideoGenerationStatus"

        payload = {
            "operations": [
                {
                    "operation": {
                        "name": task_id
                    }
                }
            ]
        }

        try:
            headers["content-type"] = "text/plain;charset=UTF-8"
            payload_str = json.dumps(payload, separators=(",", ":"))

            response = await session.post(
                url,
                headers=headers,
                data=payload_str,
            )

            if response.status_code == 401:
                raise VeoClientError(
                    "Unauthorized",
                    error_code="UNAUTHORIZED",
                    retry=True,
                )

            if response.status_code != 200:
                raise VeoClientError(
                    f"Status check failed: {response.status_code} {response.text}",
                    error_code="STATUS_CHECK_FAILED",
                    retry=True,
                )

            data = response.json()
            
            # Extract result
            ops = data.get("operations", [])
            if not ops:
                 # Should not happen usually
                return GenerationResult(task_id=task_id, status="pending")
            
            op_data = ops[0]
            # status enum string
            status_str = op_data.get("status", "")
            
            # Map status
            status = "generating" # default to generating/running
            progress = 0 # API doesn't seem to give explicit progress % usually, just status
            
            error_code = None
            error_message = None
            video_url = None
            download_url = None
            seed = None # Initialize seed
            
            if status_str == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                status = "completed"
                progress = 100
                
                # Debug: Log full response to find the correct URL field
                logger.debug(f"Success Payload for {task_id}: {json.dumps(op_data)}")
                
                # Extract URL
                # metadata -> generatedVideos -> [0] -> video -> fifeUrl
                try:
                    metadata = op_data.get("operation", {}).get("metadata", {})
                    generated_videos = []

                    if isinstance(metadata, dict):
                        # Case 1: metadata -> video -> fifeUrl (Veo3)
                        video_data = metadata.get("video", {})
                        download_url = video_data.get("fifeUrl")
                        video_url = video_data.get("mediaGenerationId")

                        # Case 2: metadata -> generatedVideos -> [0] -> video -> fifeUrl
                        if not download_url:
                            generated_videos = metadata.get("generatedVideos", [])
                            if generated_videos:
                                gv = generated_videos[0].get("video", {})
                                download_url = gv.get("fifeUrl")
                                if not video_url:
                                    video_url = gv.get("mediaGenerationId")

                        # Extract seed
                        if "seed" in video_data:
                            seed = int(video_data["seed"])
                        elif generated_videos:
                            raw_seed = generated_videos[0].get("video", {}).get("seed")
                            if raw_seed:
                                seed = int(raw_seed)
                                
                        # Fallback for upscaled videos (they use a redirect endpoint instead of returning fifeUrl)
                        if not download_url:
                            # Try to find the inner operation name, e.g. "450e..._4k_upsampled"
                            inner_op_name = op_data.get("operation", {}).get("operation", {}).get("name")
                            if inner_op_name and "_upsampled" in inner_op_name:
                                logger.info(f"Using getMediaUrlRedirect for upsampled video: {inner_op_name}")
                                try:
                                    download_url = await self._get_redirect_url(at, inner_op_name)
                                except Exception as redir_e:
                                    logger.error(f"Failed to get redirect URL for {inner_op_name}: {redir_e}")

                    if not download_url:
                        logger.warning(f"Could not extract downloadUrl from success response: {json.dumps(op_data)}")

                except Exception as e:
                    logger.warning(f"Failed to extract video data from success response: {e}")

            elif status_str in ("MEDIA_GENERATION_STATUS_FAILED", "MEDIA_GENERATION_STATUS_CANCELLED"):
                status = "failed"

                # Log FULL failure response so we can see exactly what Google returns
                logger.info(f"Failure payload for {task_id}: {json.dumps(op_data)}")

                # Deep-extract PUBLIC_ERROR code from all known Google response locations
                error_code, error_message = self._extract_flow_error(op_data)

                logger.warning(f"Video {task_id} failed: code={error_code}, msg={error_message}")

            result = GenerationResult(
                task_id=task_id,
                status=status,
                progress=progress,
                video_url=video_url,
                download_url=download_url,
                error_code=error_code,
                error_message=error_message,
                seed=seed
            )

            return result

        except VeoClientError:
            raise
        except Exception as e:
            logger.error(f"Get status error: {e}")
            raise VeoClientError(
                f"Status check failed: {e}",
                error_code="STATUS_CHECK_ERROR",
                retry=True,
            )
        finally:
            if _own_session:
                await session.close()

    async def _get_redirect_url(
        self, at: str, media_name: str
    ) -> str:
        """
        Fetch the actual Google Cloud Storage download URL for upscaled media.
        """
        session = await self._get_session()
        headers = self._get_headers(at)
        
        # TRPC endpoint expects specific formatting in some cases, but a simple GET often works
        url = f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={media_name}"
        headers["origin"] = "https://labs.google"
        
        try:
            response = await session.get(url, headers=headers)
            if response.status_code == 401:
                raise VeoClientError(
                    "Unauthorized for getMediaUrlRedirect",
                    error_code="UNAUTHORIZED",
                    retry=True,
                )
                
            if response.status_code != 200:
                raise VeoClientError(
                    f"Redirect fetch failed: {response.status_code} {response.text}",
                    error_code="REDIRECT_FAILED",
                    retry=True,
                )
                
            data = response.json()
            # TRPC responses wrap the result in {"result": {"data": ...}}
            result_data = data.get("result", {}).get("data")
            
            if isinstance(result_data, str) and result_data.startswith("http"):
                return result_data
            elif isinstance(result_data, dict) and "url" in result_data:
                return result_data["url"]
                
            logger.warning(f"Unexpected getMediaUrlRedirect response format: {data}")
            return ""
            
        except VeoClientError:
            raise
        except Exception as e:
            logger.error(f"Failed to fetch redirect URL: {e}")
            return ""

    async def poll_until_complete(
        self,
        at: str,
        task_id: str,
        timeout: int = 1800,
        poll_interval: int = 5,
    ) -> GenerationResult:
        """
        Poll video status until completion or timeout.

        Args:
            at: Access token
            task_id: Task ID
            timeout: Maximum wait time in seconds
            poll_interval: Seconds between polls

        Returns:
            Final GenerationResult
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            result = await self.get_video_status(at, task_id)

            if result.status in ("completed", "failed"):
                return result

            await asyncio.sleep(poll_interval)

        # Timeout
        return GenerationResult(
            task_id=task_id,
            status="failed",
            error_code="TIMEOUT",
            error_message=f"Video generation timed out after {timeout}s",
        )

    async def upscale_video(
        self,
        at: str,
        project_id: str,
        video_media_id: str,
        aspect_ratio: str,
        resolution: str = "4k",
        user_paygate_tier: str = "PAYGATE_TIER_TWO",
        account_email: str = "",
        request_proxy: Optional[str] = None,
    ) -> GenerationResult:
        """
        Upscale a video to higher resolution.
        
        Based on flow2api implementation:
        POST /video:batchAsyncGenerateVideoUpsampleVideo
        
        Args:
            at: Access token
            project_id: Project ID
            video_media_id: Media ID of the video to upscale
            aspect_ratio: VIDEO_ASPECT_RATIO_PORTRAIT or VIDEO_ASPECT_RATIO_LANDSCAPE
            resolution: Target resolution (4k or 1080p)
            user_paygate_tier: Paygate tier for the account
            
        Returns:
            GenerationResult with task_id for the upscaling job
        """
        _own_session = False
        if request_proxy and request_proxy != self.proxy:
            session = AsyncSession(impersonate="chrome", proxy=request_proxy, timeout=120)
            _own_session = True
            logger.debug(f"upscale_video: using pool proxy {request_proxy}")
        else:
            session = await self._get_session()
        headers = self._get_headers(at)

        # Get upscale config
        upscale_config = UPSCALE_MODELS.get(resolution.lower())
        if not upscale_config:
            raise VeoClientError(
                f"Invalid resolution: {resolution}. Use '4k' or '1080p'",
                error_code="INVALID_RESOLUTION",
                retry=False,
            )
        
        # Solve reCAPTCHA: browser captcha first (unless overloaded/capsolver-only)
        _use_browser = self._should_use_browser(account_email)
        _browser_403_count = 0

        recaptcha_token = None
        _used_browser_captcha = False
        if _use_browser:
            recaptcha_token = await self._solve_recaptcha_browser(action="VIDEO_GENERATION")
            if recaptcha_token:
                _used_browser_captcha = True
        if not recaptcha_token:
            recaptcha_token = await self._solve_recaptcha_capsolver(project_id, action="VIDEO_GENERATION")
        if not recaptcha_token:
            raise VeoClientError(
                "All captcha methods failed",
                error_code="CAPTCHA_FAILED",
                retry=True,
            )
        
        import uuid
        import random
        
        batch_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        scene_id = str(uuid.uuid4())
        session_id = f";{int(time.time() * 1000)}"

        payload = {
            "mediaGenerationContext": {
                "batchId": batch_id,
                "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
            },
            "clientContext": {
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier,
                "sessionId": session_id,
            },
            "requests": [{
                "resolution": upscale_config["resolution"],
                "aspectRatio": aspect_ratio,
                "seed": random.randint(1, 99999),
                "videoModelKey": upscale_config["model_key"],
                "metadata": {
                    "sceneId": scene_id,
                    "workflowId": workflow_id,
                },
                "videoInput": {
                    "mediaId": video_media_id
                },
            }],
            "useV2ModelConfig": True,
        }
        
        if recaptcha_token:
            payload["clientContext"]["recaptchaContext"] = {
                "token": recaptcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
            }
        
        try:
            # Force text/plain content type to match working curl
            headers["content-type"] = "text/plain;charset=UTF-8"
            _upscale_url = f"{VEO_BASE_URL}/v1/video:batchAsyncGenerateVideoUpsampleVideo"
            _upscale_data = json.dumps(payload)

            for _attempt in range(1, 4):
                try:
                    response = await session.post(_upscale_url, headers=headers, data=_upscale_data)
                    break
                except Exception as _exc:
                    _es = str(_exc)
                    if any(k in _es for k in ("curl: (56)", "curl: (7)", "curl: (28)", "Connection reset", "RemoteDisconnected")) and _attempt < 3:
                        logger.warning(f"Upscale network error (attempt {_attempt}/3), retrying: {_es[:120]}")
                        await asyncio.sleep(1)
                    else:
                        raise

            if response.status_code == 401:
                raise VeoClientError(
                    "Unauthorized",
                    error_code="UNAUTHORIZED",
                    retry=True,
                )
            
            if response.status_code == 403:
                if _used_browser_captcha:
                    _browser_403_count += 1
                    if self.browser_captcha:
                        self.browser_captcha.report_error("403 Forbidden - upscale reCAPTCHA failed")
                    # Retry with fresh browser captcha tokens (up to 5 times)
                    browser_retry_success = False
                    for browser_403_attempt in range(1, 6):
                        if not self._should_use_browser(account_email):
                            logger.warning("Upscale 403 — browser overloaded/capsolver-only, skipping remaining browser retries")
                            break
                        logger.warning(f"Upscale 403 — browser captcha retry ({browser_403_attempt}/5)...")
                        retry_token = await self._solve_recaptcha_browser(action="VIDEO_GENERATION")
                        if not retry_token:
                            continue
                        payload["clientContext"]["recaptchaContext"] = {
                            "token": retry_token,
                            "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                        }
                        response = await session.post(
                            f"{VEO_BASE_URL}/v1/video:batchAsyncGenerateVideoUpsampleVideo",
                            headers=headers,
                            data=json.dumps(payload),
                        )
                        if response.status_code == 200:
                            logger.info(f"Upscale browser captcha retry succeeded on attempt {browser_403_attempt}")
                            browser_retry_success = True
                            break
                        else:
                            _browser_403_count += 1
                            logger.warning(f"Upscale browser captcha retry attempt {browser_403_attempt} got {response.status_code}")
                            if self.browser_captcha:
                                self.browser_captcha.report_error(f"Upscale 403 retry attempt {browser_403_attempt}")

                    if not browser_retry_success:
                        # Report browser 403 stats before capsolver fallback
                        if self.browser_captcha and account_email:
                            self.browser_captcha.record_account_browser_403s(account_email, _browser_403_count)

                        if self.capsolver:
                            capsolver_success = False
                            for capsolver_attempt in range(1, 4):
                                logger.warning(f"Upscale capsolver fallback attempt ({capsolver_attempt}/3)...")
                                fallback_token = await self._solve_recaptcha_capsolver(
                                    project_id, action="VIDEO_GENERATION"
                                )
                                if not fallback_token:
                                    logger.warning(f"Upscale capsolver attempt {capsolver_attempt} failed to get token")
                                    continue
                                payload["clientContext"]["recaptchaContext"] = {
                                    "token": fallback_token,
                                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                                }
                                response = await session.post(
                                    f"{VEO_BASE_URL}/v1/video:batchAsyncGenerateVideoUpsampleVideo",
                                    headers=headers,
                                    data=json.dumps(payload),
                                )
                                if response.status_code == 200:
                                    logger.info(f"Upscale capsolver fallback succeeded on attempt {capsolver_attempt}")
                                    capsolver_success = True
                                    break
                                else:
                                    logger.warning(f"Upscale capsolver attempt {capsolver_attempt} got {response.status_code}")

                            if not capsolver_success:
                                raise VeoClientError(
                                    "Upscale forbidden (all browser + capsolver retries exhausted)",
                                    error_code="FORBIDDEN",
                                    retry=True,
                                )
                        else:
                            raise VeoClientError(
                                "Upscale forbidden (all browser captcha retries exhausted, no capsolver)",
                                error_code="FORBIDDEN",
                                retry=True,
                            )
                else:
                    if self.browser_captcha:
                        self.browser_captcha.report_error("403 Forbidden - upscale reCAPTCHA failed")
                    raise VeoClientError(
                        "Upscale access forbidden",
                        error_code="FORBIDDEN",
                        retry=True,
                    )
            
            if response.status_code == 429:
                # 429 from upscale = reCAPTCHA rate-limit — retry with fresh captcha + new proxy each attempt
                from .proxy_pool import get_proxy_pool
                logger.warning("Upscale 429 reCAPTCHA rate-limit, retrying with fresh captcha + rotating proxies...")
                if self.browser_captcha:
                    self.browser_captcha.report_error("429 reCAPTCHA rate-limit on upscale")
                retry_success = False
                _retry_sessions: list[AsyncSession] = []
                try:
                    # Browser retries
                    for browser_429_attempt in range(1, 6):
                        if not self._should_use_browser(account_email):
                            break
                        retry_token = await self._solve_recaptcha_browser(action="VIDEO_GENERATION")
                        if not retry_token:
                            continue
                        payload["clientContext"]["recaptchaContext"] = {
                            "token": retry_token,
                            "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                        }
                        retry_proxy = await get_proxy_pool().get_next()
                        retry_session = AsyncSession(impersonate="chrome", proxy=retry_proxy, timeout=120)
                        _retry_sessions.append(retry_session)
                        logger.debug(f"Upscale 429 retry {browser_429_attempt}: proxy={retry_proxy}")
                        response = await retry_session.post(
                            f"{VEO_BASE_URL}/v1/video:batchAsyncGenerateVideoUpsampleVideo",
                            headers=headers,
                            data=json.dumps(payload),
                        )
                        if response.status_code == 200:
                            logger.info(f"Upscale 429 browser retry succeeded on attempt {browser_429_attempt}")
                            retry_success = True
                            break
                        logger.warning(f"Upscale 429 browser retry {browser_429_attempt} got {response.status_code}")
                    # Capsolver fallback
                    if not retry_success and self.capsolver:
                        for capsolver_attempt in range(1, 4):
                            fallback_token = await self._solve_recaptcha_capsolver(project_id, action="VIDEO_GENERATION")
                            if not fallback_token:
                                continue
                            payload["clientContext"]["recaptchaContext"] = {
                                "token": fallback_token,
                                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                            }
                            retry_proxy = await get_proxy_pool().get_next()
                            retry_session = AsyncSession(impersonate="chrome", proxy=retry_proxy, timeout=120)
                            _retry_sessions.append(retry_session)
                            logger.debug(f"Upscale 429 capsolver retry {capsolver_attempt}: proxy={retry_proxy}")
                            response = await retry_session.post(
                                f"{VEO_BASE_URL}/v1/video:batchAsyncGenerateVideoUpsampleVideo",
                                headers=headers,
                                data=json.dumps(payload),
                            )
                            if response.status_code == 200:
                                logger.info(f"Upscale 429 capsolver retry succeeded on attempt {capsolver_attempt}")
                                retry_success = True
                                break
                            logger.warning(f"Upscale 429 capsolver retry {capsolver_attempt} got {response.status_code}")
                finally:
                    for s in _retry_sessions:
                        try:
                            await s.close()
                        except Exception:
                            pass
                if not retry_success:
                    raise VeoClientError(
                        "Upscale rate-limited (all captcha retries exhausted)",
                        error_code="RATE_LIMITED",
                        retry=True,
                    )

            if response.status_code != 200:
                resp_text = response.text[:500] if response.text else ""
                flow_error_code = "UPSCALING_ERROR"
                flow_error_message = ""
                try:
                    err_data = response.json()
                    found = self._find_public_error(err_data)
                    if found:
                        flow_error_code = found
                    if isinstance(err_data, dict):
                        flow_error_message = err_data.get("error", {}).get("message", "") or ""
                except Exception:
                    pass
                is_public_error = flow_error_code.startswith("PUBLIC_ERROR")
                logger.error(f"Upscale API error: {response.status_code} {resp_text}")
                raise VeoClientError(
                    flow_error_message or f"Upscaling failed: {response.status_code}",
                    error_code=flow_error_code,
                    retry=not is_public_error,
                )
            
            data = response.json()
            
            # Extract task ID from operations
            operations = data.get("operations", [])
            if not operations:
                raise VeoClientError(
                    "No operation returned",
                    error_code="NO_OPERATION",
                    retry=False,
                )
            
            op = operations[0]
            task_id = op.get("operation", {}).get("name", "")
            
            logger.info(f"Upscaling started: {task_id}")
            
            return GenerationResult(
                task_id=task_id,
                scene_id=workflow_id,
                status="generating",
            )
            
        except VeoClientError:
            raise
        except Exception as e:
            logger.error(f"Upscale video error: {e}")
            raise VeoClientError(
                f"Upscaling failed: {e}",
                error_code="UPSCALING_ERROR",
                retry=True,
            )
        finally:
            if _own_session:
                try:
                    await session.close()
                except Exception:
                    pass


    async def run_concatenation(
        self,
        at: str,
        input_videos: list[dict],
    ) -> str:
        """
        Start a server-side video concatenation on Google's side.

        Used after Extend generation completes to merge original + extension.

        Args:
            at: Access token
            input_videos: List of video descriptors, each with:
                - mediaGenerationId: str
                - lengthNanos: int (duration in nanos, e.g. 8000 for 8s)
                - startTimeOffset: str (e.g. "0s")
                - endTimeOffset: str (e.g. "8s")

        Returns:
            Operation name (job ID) for polling
        """
        session = await self._get_session()
        headers = self._get_headers(at)
        headers["content-type"] = "text/plain;charset=UTF-8"

        payload = {"inputVideos": input_videos}

        url = f"{VEO_BASE_URL}/v1:runVideoFxConcatenation"

        try:
            response = await session.post(
                url,
                headers=headers,
                data=json.dumps(payload, separators=(",", ":")),
            )

            if response.status_code == 401:
                raise VeoClientError(
                    "Unauthorized", error_code="UNAUTHORIZED", retry=True
                )

            if response.status_code != 200:
                resp_text = response.text[:500] if response.text else ""
                raise VeoClientError(
                    f"Concatenation request failed: {response.status_code} {resp_text}",
                    error_code="CONCAT_FAILED",
                    retry=True,
                )

            data = response.json()

            # Response: {"operation": {"operation": {"name": "projects/.../jobs/..."}}}
            op = data.get("operation", {})
            if isinstance(op, dict):
                inner_op = op.get("operation", {})
                if isinstance(inner_op, dict):
                    name = inner_op.get("name", "")
                    if name:
                        logger.info(f"Concatenation started: {name}")
                        return name

            raise VeoClientError(
                f"No operation name in concatenation response: {json.dumps(data)[:300]}",
                error_code="CONCAT_NO_OPERATION",
                retry=False,
            )

        except VeoClientError:
            raise
        except Exception as e:
            logger.error(f"Concatenation request error: {e}")
            raise VeoClientError(
                f"Concatenation failed: {e}",
                error_code="CONCAT_ERROR",
                retry=True,
            )

    async def check_concatenation_status(
        self,
        at: str,
        operation_name: str,
    ) -> tuple[str, Optional[bytes]]:
        """
        Check concatenation job status.

        Args:
            at: Access token
            operation_name: Operation name from run_concatenation

        Returns:
            (status_str, video_bytes_or_none)
            status_str: "completed", "generating", or "failed"
            video_bytes_or_none: decoded video bytes when completed, None otherwise
        """
        session = await self._get_session()
        headers = self._get_headers(at)
        headers["content-type"] = "text/plain;charset=UTF-8"

        payload = {
            "operation": {
                "operation": {
                    "name": operation_name,
                }
            }
        }

        url = f"{VEO_BASE_URL}/v1:runVideoFxCheckConcatenationStatus"

        try:
            response = await session.post(
                url,
                headers=headers,
                data=json.dumps(payload, separators=(",", ":")),
            )

            if response.status_code == 401:
                raise VeoClientError(
                    "Unauthorized", error_code="UNAUTHORIZED", retry=True
                )

            if response.status_code != 200:
                resp_text = response.text[:500] if response.text else ""
                raise VeoClientError(
                    f"Concatenation status check failed: {response.status_code} {resp_text}",
                    error_code="CONCAT_STATUS_FAILED",
                    retry=True,
                )

            data = response.json()
            status_str = data.get("status", "")

            if status_str == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                encoded_video = data.get("encodedVideo", "")
                if encoded_video:
                    video_bytes = base64.b64decode(encoded_video)
                    logger.info(
                        f"Concatenation complete: {len(video_bytes)} bytes"
                    )
                    return "completed", video_bytes
                else:
                    logger.warning("Concatenation succeeded but no encodedVideo in response")
                    return "completed", None

            if "FAILED" in status_str or "CANCELLED" in status_str:
                logger.warning(f"Concatenation failed: {status_str}")
                return "failed", None

            return "generating", None

        except VeoClientError:
            raise
        except Exception as e:
            logger.error(f"Concatenation status check error: {e}")
            raise VeoClientError(
                f"Concatenation status check failed: {e}",
                error_code="CONCAT_STATUS_ERROR",
                retry=True,
            )

    async def poll_concatenation(
        self,
        at: str,
        operation_name: str,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> Optional[bytes]:
        """
        Poll concatenation status until complete.

        Args:
            at: Access token
            operation_name: Operation name from run_concatenation
            timeout: Maximum wait time in seconds (default 5 min)
            poll_interval: Seconds between polls

        Returns:
            Video bytes on success, None on failure/timeout
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            status, video_bytes = await self.check_concatenation_status(
                at, operation_name
            )

            if status == "completed":
                return video_bytes

            if status == "failed":
                return None

            await asyncio.sleep(poll_interval)

        logger.error(
            f"Concatenation timed out after {timeout}s: {operation_name}"
        )
        return None


def resolve_model(
    user_model: str, mode: str, aspect_ratio: str
) -> Optional[str]:
    """
    Resolve user model name to Google model key.

    Args:
        user_model: User-facing model name
        mode: Generation mode (t2v, i2v, i2v_dual, r2v)
        aspect_ratio: Aspect ratio (16:9 or 9:16)

    Returns:
        Google model key or None if not found
    """
    # Resolve alias
    resolved_model = MODEL_ALIASES.get(user_model, user_model)

    # Look up in mapping
    key = (resolved_model, mode, aspect_ratio)
    return MODEL_MAPPING.get(key)


def supports_feature(user_model: str, feature: str) -> bool:
    """Check if a model supports a feature."""
    resolved_model = MODEL_ALIASES.get(user_model, user_model)

    if feature == "dual_mode":
        return resolved_model in ("veo31_fast", "veo31_pro")

    if feature == "reference_images":
        return resolved_model in ("veo31_fast", "omni_flash")

    if feature == "portrait":
        return True  # All models support portrait
    
    if feature == "upscale":
        return resolved_model in ("veo31_fast", "veo31_pro")

    return False


def get_upscale_config(resolution: str) -> Optional[dict]:
    """Get upscaling configuration for target resolution."""
    return UPSCALE_MODELS.get(resolution.lower())

