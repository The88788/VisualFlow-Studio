"""Automated Google account login for labs.google/fx/tools/flow.

Python/Playwright port of setup_puppeteer.js. Designed to be driven over
Chrome CDP with ``BrowserManager.launch_for_login(Platform.FLOW)`` — that is,
against a real Chrome process (not Playwright's bundled Chromium) with
``--disable-blink-features=AutomationControlled`` and no ``--enable-automation``
flag, so Google's sign-in doesn't show the "browser may not be secure" block.

Flow mirrors the original JS:
  1. Warmup on google.com (accumulate cookies, look human)
  2. Navigate to labs.google/fx/tools/flow
  3. Click "Create with Flow" (may open a new tab)
  4. If already authenticated → extract ST cookie and return
  5. Email → #identifierNext
  6. Password (4 fallback strategies) → #passwordNext
  7. TOTP 2FA if account.otp_secret is set
  8. Handle Welcome / Change-Password / Disabled screens
  9. Wait for redirect back to labs.google
 10. Extract __Secure-next-auth.session-token from cookies

Does NOT solve CAPTCHAs. The original JS also doesn't — Google does not
typically present them for real-Chrome + residential-proxy + credentialed
login. If a CAPTCHA appears, ``CaptchaRequiredError`` is raised.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout

from ...utils.totp import generate_totp
from .exceptions import (
    AccountDisabledError,
    CaptchaRequiredError,
    LoginFailedError,
    SessionTokenNotFoundError,
    TwoFactorRequiredError,
)

logger = logging.getLogger(__name__)


async def login_and_get_session_token(
    account: dict[str, Any],
    browser_manager,  # BrowserManager — avoid circular import
    debug_dir: Path | None = None,
    try_reauth_first: bool = True,
) -> str:
    """One-shot convenience: open a headed Chrome for the account, run login,
    return the ``__Secure-next-auth.session-token`` value.

    The caller owns ``browser_manager``; this function does NOT close it
    (so the same Chrome process can be reused for generation afterwards).

    Parameters
    ----------
    account : dict
        ``{email, password, otp_secret?, profile_path?, name?}``
    browser_manager : BrowserManager
        Pre-configured with the account's ``data_dir`` and ``proxy_url``.
    debug_dir : Path | None
        Optional directory for per-step screenshots/HTML.
    try_reauth_first : bool
        If True (default), first navigate to Flow with existing cookies;
        if already authenticated, skip the UI flow entirely.
    """
    from ...types import Platform  # local import to avoid circular refs

    context, page = await browser_manager.launch_for_login(Platform.FLOW)
    authenticator = FlowAuthenticator(account, debug_dir=debug_dir)

    if try_reauth_first:
        token = await authenticator.check_and_extract(context, page)
        if token:
            logger.info("[FlowAuth] Reauth path succeeded (cookies still valid)")
            return token

    return await authenticator.authenticate(page, context)


# -------- constants ported from setup_puppeteer.js --------

FLOW_URL = "https://labs.google/fx/tools/flow"
GOOGLE_WARMUP_URL = "https://www.google.com"
SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"

# Keywords used to detect disabled/suspended account pages (case-insensitive)
DISABLED_KEYWORDS = (
    "account has been disabled",
    "account is disabled",
    "account disabled",
    "google workspace policy",
    "аккаунт отключен",
    "аккаунт заблокирован",
    "отключён",
)

# Keywords that identify Welcome / ToS screens we just need to dismiss
WELCOME_BUTTON_KEYWORDS = (
    "i understand",
    "understand",
    "accept",
    "agree",
    "confirm",
    "continue",
    "next",
    "got it",
    "понятно",
    "принять",
    "продолжить",
)

# CAPTCHA heuristics
CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[title*='reCAPTCHA']",
    "div#captcha-form",
    "div[id*='captcha']",
)


class FlowAuthenticator:
    """Automated Google login driver for Google Flow.

    Parameters
    ----------
    account : dict
        Must contain ``email``. Optional: ``password``, ``otp_secret`` (base32),
        ``name`` (for logs), ``profile_path`` (where to write session_token.txt).
    debug_dir : Path | None
        If set, screenshots + HTML snapshots are written here at each step.
    """

    def __init__(self, account: dict[str, Any], debug_dir: Path | None = None):
        self.account = account
        self.email: str = account["email"]
        self.password: str | None = account.get("password")
        self.otp_secret: str | None = account.get("otp_secret")
        self.name: str = account.get("name") or self.email
        self.profile_path: Path | None = (
            Path(account["profile_path"]) if account.get("profile_path") else None
        )
        self.debug_dir = debug_dir
        self._step_counter = 0

    # ---------------- public entry point ----------------

    async def authenticate(self, page: Page, context: BrowserContext) -> str:
        """Drive the full login flow. Returns the extracted session token.

        Raises:
            AccountDisabledError: Google shows a disabled-account screen.
            LoginFailedError: Any other failure during the flow.
            TwoFactorRequiredError: 2FA prompt without otp_secret.
            CaptchaRequiredError: CAPTCHA detected.
            SessionTokenNotFoundError: Flow finished but ST cookie is missing.
        """
        logger.info("[FlowAuth:%s] Starting login flow", self.name)
        await self._warmup(page)
        await self._goto_flow(page)
        page = await self._click_create_button(page, context)  # may swap to a new tab

        # Early-exit: already authenticated (ST cookie present in context)
        if await self._is_authenticated(context, page):
            logger.info("[FlowAuth:%s] Already authenticated — extracting ST", self.name)
            await self._debug_capture(page, "already_authed")
            return await self._extract_session_token(context)

        await self._check_captcha(page)
        await self._handle_disabled_screen(page)

        await self._login_email(page)
        await self._check_captcha(page)
        await self._login_password(page)
        await self._check_captcha(page)

        # 2FA prompt may or may not appear depending on account risk signals
        await self._maybe_handle_2fa(page)
        await self._handle_disabled_screen(page)

        # Post-auth interstitials
        await self._handle_welcome_screen(page)
        await self._handle_change_password_screen(page)

        # Google typically redirects back to labs.google/fx/tools/flow after success
        await self._wait_for_labs_redirect(page)
        await self._handle_disabled_screen(page)

        token = await self._extract_session_token(context)
        self._persist_session_token(token)
        logger.info("[FlowAuth:%s] Login complete", self.name)
        return token

    # ---------------- step: warmup ----------------

    async def _warmup(self, page: Page) -> None:
        try:
            await page.goto(GOOGLE_WARMUP_URL, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            logger.warning("[FlowAuth:%s] Warmup goto timed out — continuing", self.name)
        await self._human_sleep(2.0, 4.0)
        await self._debug_capture(page, "00_warmup")

    # ---------------- step: go to Flow ----------------

    async def _goto_flow(self, page: Page) -> None:
        try:
            await page.goto(FLOW_URL, wait_until="networkidle", timeout=60_000)
        except PWTimeout:
            logger.warning("[FlowAuth:%s] Flow goto networkidle timed out — using DOM state", self.name)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except PWTimeout:
                pass
        await asyncio.sleep(4)
        await self._debug_capture(page, "01_flow_page")

    # ---------------- step: click "Create with Flow" ----------------

    async def _click_create_button(self, page: Page, context: BrowserContext) -> Page:
        """Click the landing-page CTA. Returns the active page (may be a new tab)."""
        for attempt in range(3):
            clicked = await page.evaluate(
                """() => {
                    const candidates = ['a', 'button'];
                    const priorities = [];
                    for (const tag of candidates) {
                      for (const el of document.querySelectorAll(tag)) {
                        const text = (el.textContent || '').trim().toLowerCase();
                        if (!text) continue;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        if (
                          text === 'create with flow'
                          || text === 'create with google flow'
                          || text === 'try flow'
                          || text === 'open flow'
                        ) {
                          priorities.unshift(el);
                        } else if (
                          text.includes('create with flow')
                          || text.includes('create with google flow')
                          || text.includes('try flow')
                        ) {
                          priorities.push(el);
                        } else if (text === 'flow') {
                          priorities.push(el);
                        }
                      }
                    }
                    if (priorities.length) { priorities[0].click(); return true; }
                    return false;
                }"""
            )
            if clicked:
                logger.info("[FlowAuth:%s] Clicked Flow CTA", self.name)
                break
            await asyncio.sleep(5 if attempt == 0 else 3)
            if attempt == 1:
                try:
                    await page.evaluate("window.scrollTo(0, 0)")
                except Exception:
                    pass

        # A new tab may open — attach to it if so. Skip blank/chrome:// pages
        # (Chrome's startup "New Tab" page has URL chrome://new-tab-page/ which
        # is NOT an OAuth popup and must be ignored).
        await asyncio.sleep(2)
        pages = context.pages
        if len(pages) > 1:
            def _is_blank(u: str) -> bool:
                return (
                    u == ""
                    or u == "about:blank"
                    or u.startswith("chrome://")
                    or u.startswith("devtools://")
                )

            candidates = [p for p in pages if p != page and not _is_blank(p.url or "")]
            if candidates:
                new_page = candidates[-1]
                logger.info("[FlowAuth:%s] Switched to new tab: %s", self.name, new_page.url[:120])
                try:
                    await page.close()
                except Exception:
                    pass
                page = new_page
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
            else:
                logger.info(
                    "[FlowAuth:%s] No OAuth popup detected; staying on current page (url=%s)",
                    self.name, (page.url or "")[:120],
                )

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PWTimeout:
            pass
        await asyncio.sleep(2)
        await self._debug_capture(page, "02_after_create_click")
        return page

    # ---------------- auth-state check ----------------

    async def _is_authenticated(self, context: BrowserContext, page: Page) -> bool:
        cookies = await context.cookies()
        has_st = any(c.get("name") == SESSION_COOKIE_NAME and c.get("value") for c in cookies)
        on_login = "accounts.google.com" in (page.url or "")
        return has_st and not on_login

    # ---------------- step: email ----------------

    async def _login_email(self, page: Page) -> None:
        logger.info("[FlowAuth:%s] Entering email", self.name)
        try:
            await page.wait_for_selector('input[type="email"]', timeout=20_000)
        except PWTimeout as e:
            await self._debug_capture(page, "err_no_email_input")
            raise LoginFailedError("Email input never appeared") from e

        await page.fill('input[type="email"]', "")
        await page.type('input[type="email"]', self.email, delay=random.randint(60, 120))
        await self._debug_capture(page, "03_email_filled")

        clicked = await self._click_any(page, ["#identifierNext button", "#identifierNext"])
        if not clicked:
            # Fallback: submit via Enter
            await page.press('input[type="email"]', "Enter")

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PWTimeout:
            pass
        await asyncio.sleep(2)
        await self._debug_capture(page, "04_after_email")

    # ---------------- step: password ----------------

    async def _login_password(self, page: Page) -> None:
        if not self.password:
            raise LoginFailedError("Account has no password configured")

        logger.info("[FlowAuth:%s] Entering password", self.name)

        # Wait for password field (also covers "Welcome" pre-screen with name preview)
        try:
            await page.wait_for_selector('input[type="password"]:not([aria-hidden="true"])', timeout=25_000)
        except PWTimeout as e:
            # Could be a recovery/phone-verify screen
            await self._debug_capture(page, "err_no_password_input")
            await self._handle_disabled_screen(page)
            if await self._is_phone_verify(page):
                raise LoginFailedError("Account requires phone verification — unsupported") from e
            raise LoginFailedError("Password input never appeared") from e

        strategies = (
            ("humanlike", self._type_humanlike),
            ("keyboard_type", self._type_keyboard),
            ("page_type", self._type_simple),
            ("native_setter", self._type_native),
        )
        last_err: Exception | None = None
        for label, fn in strategies:
            try:
                ok = await fn(page, 'input[type="password"]:not([aria-hidden="true"])', self.password)
                if ok:
                    logger.info("[FlowAuth:%s] Password entered via strategy=%s", self.name, label)
                    break
            except Exception as e:
                last_err = e
                logger.debug("[FlowAuth:%s] Password strategy %s failed: %s", self.name, label, e)
        else:
            raise LoginFailedError(f"All password input strategies failed: {last_err}")

        await self._debug_capture(page, "05_password_filled")
        clicked = await self._click_any(page, ["#passwordNext button", "#passwordNext"])
        if not clicked:
            await page.press('input[type="password"]', "Enter")

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except PWTimeout:
            pass
        await asyncio.sleep(3)
        await self._debug_capture(page, "06_after_password")

    # ---------- password typing strategies ----------

    async def _type_humanlike(self, page: Page, selector: str, text: str) -> bool:
        """Focus + triple-click + Backspace, then key-press each char with jitter."""
        el = await page.query_selector(selector)
        if not el:
            return False
        await el.click(click_count=3)
        await page.keyboard.press("Backspace")
        await el.focus()
        for ch in text:
            await page.keyboard.type(ch, delay=random.randint(70, 160))
        await asyncio.sleep(0.2)
        value = await el.input_value()
        return value == text

    async def _type_keyboard(self, page: Page, selector: str, text: str) -> bool:
        await page.click(selector)
        await page.keyboard.type(text, delay=120)
        await asyncio.sleep(0.2)
        el = await page.query_selector(selector)
        return bool(el and (await el.input_value()) == text)

    async def _type_simple(self, page: Page, selector: str, text: str) -> bool:
        await page.fill(selector, "")
        await page.type(selector, text, delay=100)
        await asyncio.sleep(0.2)
        el = await page.query_selector(selector)
        return bool(el and (await el.input_value()) == text)

    async def _type_native(self, page: Page, selector: str, text: str) -> bool:
        """Last-resort: use the native value setter + dispatch input/change."""
        ok = await page.evaluate(
            """({sel, value}) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return el.value === value;
            }""",
            {"sel": selector, "value": text},
        )
        return bool(ok)

    # ---------------- step: 2FA ----------------

    async def _maybe_handle_2fa(self, page: Page) -> None:
        otp_selectors = [
            'input#totpPin',
            'input[name="totpPin"]',
            'input[type="tel"][maxlength="6"]',
            'input[autocomplete="one-time-code"]',
        ]
        found_selector: str | None = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for sel in otp_selectors:
                try:
                    if await page.is_visible(sel, timeout=500):
                        found_selector = sel
                        break
                except Exception:
                    continue
            if found_selector:
                break
            await asyncio.sleep(1)

        if not found_selector:
            return  # No 2FA requested

        if not self.otp_secret:
            await self._debug_capture(page, "err_2fa_no_secret")
            raise TwoFactorRequiredError(
                f"2FA prompt shown for {self.name} but no otp_secret configured"
            )

        code = generate_totp(self.otp_secret)
        logger.info("[FlowAuth:%s] 2FA prompted — submitting TOTP", self.name)
        await page.fill(found_selector, "")
        await page.type(found_selector, code, delay=80)
        await self._debug_capture(page, "07_totp_filled")

        clicked = await self._click_any(page, ["#totpNext button", "#totpNext"])
        if not clicked:
            await page.press(found_selector, "Enter")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except PWTimeout:
            pass
        await asyncio.sleep(3)
        await self._debug_capture(page, "08_after_totp")

    # ---------------- step: welcome / ToS ----------------

    async def _handle_welcome_screen(self, page: Page) -> None:
        clicked = await page.evaluate(
            """(keywords) => {
                for (const el of document.querySelectorAll('button, [role="button"]')) {
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (!text) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    for (const kw of keywords) {
                        if (text === kw || text.includes(kw)) { el.click(); return text; }
                    }
                }
                return null;
            }""",
            list(WELCOME_BUTTON_KEYWORDS),
        )
        if clicked:
            logger.info("[FlowAuth:%s] Dismissed welcome/ToS screen: %r", self.name, clicked)
            await asyncio.sleep(2)
            await self._debug_capture(page, "09_after_welcome")

    # ---------------- step: change password ----------------

    async def _handle_change_password_screen(self, page: Page) -> str | None:
        """If Google demands a password change, set a fresh one and persist it.

        Returns the new password if changed, None otherwise.
        """
        count = await page.evaluate(
            """() => document.querySelectorAll('input[type="password"]:not([aria-hidden=\"true\"])').length"""
        )
        if count < 2:
            return None

        new_password = f"Matkhau@{random.randint(1000, 9999)}@"
        logger.warning("[FlowAuth:%s] Password change requested — setting new one", self.name)
        try:
            inputs = await page.query_selector_all('input[type="password"]:not([aria-hidden="true"])')
            for inp in inputs[:2]:
                await inp.click(click_count=3)
                await page.keyboard.press("Backspace")
                await inp.type(new_password, delay=random.randint(70, 140))
            await self._debug_capture(page, "10_change_password_filled")
            clicked = await self._click_any(page, [
                "button[jsname]:has-text('Change password')",
                "button:has-text('Change password')",
                "button:has-text('Save')",
                "button:has-text('Update')",
                "button:has-text('Continue')",
                "#passwordNext button",
                "#passwordNext",
            ])
            if not clicked and inputs:
                await inputs[-1].press("Enter")
            await asyncio.sleep(4)
            self.account["password"] = new_password
            self.password = new_password
            await self._debug_capture(page, "11_after_change_password")
            return new_password
        except Exception as e:
            await self._debug_capture(page, "err_change_password")
            raise LoginFailedError(f"Failed to change password: {e}") from e

    # ---------------- step: disabled / banned ----------------

    async def _handle_disabled_screen(self, page: Page) -> None:
        try:
            body_text = (await page.evaluate("() => document.body ? document.body.innerText : ''")) or ""
        except Exception:
            return
        lowered = body_text.lower()
        for kw in DISABLED_KEYWORDS:
            if kw in lowered:
                await self._debug_capture(page, "err_disabled")
                raise AccountDisabledError(
                    f"Account {self.email} appears disabled (matched '{kw}')"
                )

    async def _is_phone_verify(self, page: Page) -> bool:
        try:
            text = (await page.evaluate("() => document.body ? document.body.innerText : ''")) or ""
        except Exception:
            return False
        t = text.lower()
        return (
            "confirm your phone" in t
            or "verify it's you" in t
            or "verify its you" in t
            or "enter the phone" in t
            or "verify your phone" in t
        )

    # ---------------- step: CAPTCHA check ----------------

    async def _check_captcha(self, page: Page) -> None:
        for sel in CAPTCHA_SELECTORS:
            try:
                if await page.is_visible(sel, timeout=500):
                    await self._debug_capture(page, "err_captcha")
                    raise CaptchaRequiredError(f"CAPTCHA element visible: {sel}")
            except CaptchaRequiredError:
                raise
            except Exception:
                continue

    # ---------------- step: wait for redirect back to labs ----------------

    async def _wait_for_labs_redirect(self, page: Page) -> None:
        """Poll up to 60s for the page to come back to labs.google (post-auth)."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            url = page.url or ""
            if "labs.google" in url and "accounts.google.com" not in url:
                logger.info("[FlowAuth:%s] Redirected back to labs: %s", self.name, url[:120])
                await asyncio.sleep(2)
                return
            await asyncio.sleep(1)
        logger.warning("[FlowAuth:%s] No redirect to labs.google after 60s (url=%s)", self.name, page.url)

    # ---------------- session token extraction ----------------

    async def _extract_session_token(self, context: BrowserContext) -> str:
        cookies = await context.cookies()
        for c in cookies:
            if c.get("name") == SESSION_COOKIE_NAME and c.get("value"):
                return c["value"]
        raise SessionTokenNotFoundError(
            f"{SESSION_COOKIE_NAME} cookie not found after login for {self.email}"
        )

    def _persist_session_token(self, token: str) -> None:
        if not self.profile_path:
            return
        try:
            self.profile_path.mkdir(parents=True, exist_ok=True)
            out = self.profile_path / "session_token.txt"
            out.write_text(token, encoding="utf-8")
            logger.info("[FlowAuth:%s] Wrote session token → %s", self.name, out)
        except Exception as e:
            logger.warning("[FlowAuth:%s] Could not persist session token: %s", self.name, e)

    # ---------------- helpers ----------------

    async def _click_any(self, page: Page, selectors: list[str]) -> bool:
        for sel in selectors:
            try:
                if await page.is_visible(sel, timeout=1500):
                    await page.click(sel, timeout=5000)
                    return True
            except Exception:
                continue
        return False

    async def _human_sleep(self, lo: float, hi: float) -> None:
        await asyncio.sleep(random.uniform(lo, hi))

    # ---------------- reauth shortcut ----------------

    async def check_and_extract(self, context: BrowserContext, page: Page) -> str | None:
        """Return the session token if the context is already authenticated, else None.

        Useful for fast reauth paths: open the persistent profile, navigate to
        Flow, and if cookies are still valid just grab the token without
        touching the login UI.
        """
        try:
            await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            pass
        await asyncio.sleep(2)
        if await self._is_authenticated(context, page):
            try:
                token = await self._extract_session_token(context)
            except SessionTokenNotFoundError:
                return None
            self._persist_session_token(token)
            return token
        return None

    async def _debug_capture(self, page: Page, name: str) -> None:
        if not self.debug_dir:
            return
        self._step_counter += 1
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%H%M%S")
            prefix = f"{self._step_counter:02d}_{stamp}_{name}"
            await page.screenshot(path=str(self.debug_dir / f"{prefix}.png"), full_page=False)
            html = await page.content()
            (self.debug_dir / f"{prefix}.html").write_text(html, encoding="utf-8")
            (self.debug_dir / f"{prefix}.url.txt").write_text(page.url or "", encoding="utf-8")
        except Exception as e:
            logger.debug("[FlowAuth:%s] debug capture failed (%s): %s", self.name, name, e)
