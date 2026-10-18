"""Per-account Chrome session pool for the browser2api provider.

Invariants
----------
* **One Chrome process per account.** Chrome holds an exclusive lock on
  the ``--user-data-dir``, so two Chrome instances cannot share a profile.
* **N tabs per process.** ``BrowserManager.new_page()`` opens an additional
  page inside the persistent CDP context; each page hosts its own
  ``FlowVideoClient`` for one generation.
* **RW-style guard.** Concurrent generations on an account hold a read-style
  lease (just the per-account semaphore). A full reauth needs an exclusive
  lease — it waits for all in-flight generations to finish, then evicts.

The pool is *lazy*: a Chrome is launched on first ``acquire_tab`` for an
account, and can be evicted on idle or on explicit ``evict``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

from ...core.config import Config
from ...core.models import AccountDB

# Imports from the vendored fork (path injection in src/services/providers/__init__.py)
from browser2api import BrowserManager  # type: ignore
from browser2api.types import Platform  # type: ignore
from playwright.async_api import BrowserContext, Page

# CloakBrowser — stealth Chromium binary used when
# ``config.use_cloakbrowser`` is on. Imported lazily so the pool still
# loads when the dep is not installed yet (e.g. on first deploy).
try:
    from cloakbrowser import launch_persistent_context_async  # type: ignore
    _CLOAK_AVAILABLE = True
except Exception:  # pragma: no cover — optional dep
    launch_persistent_context_async = None  # type: ignore
    _CLOAK_AVAILABLE = False

logger = logging.getLogger(__name__)


@dataclass
class _AccountSession:
    account_id: int
    email: str
    profile_path: Path
    proxy_url: Optional[str]
    manager: BrowserManager
    context: BrowserContext
    semaphore: asyncio.Semaphore
    # Reauth lock: exclusive access while refreshing cookies. Held in "shared" mode
    # by active generations (via _active_count) and in "exclusive" mode for reauth.
    reauth_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_count: int = 0
    active_count_zero: asyncio.Event = field(default_factory=asyncio.Event)
    last_used_monotonic: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        self.active_count_zero.set()


class _CloakManagerAdapter:
    """Tiny shim that mimics the slice of ``BrowserManager`` that ``_TabLease``
    and ``BrowserSessionPool`` rely on, so swapping the launch backend doesn't
    leak into the rest of the provider.

    Surface used elsewhere:
      * ``await manager.new_page(Platform.FLOW)`` → new tab in the persistent context
      * ``await manager.close()``                  → tear down the Chromium process

    No JS-injection ``_apply_stealth`` here — the whole point of CloakBrowser
    is that fingerprint patches live in the binary. Injecting playwright-style
    stealth scripts on top would add fresh detectable signals.
    """

    def __init__(self, context: BrowserContext):
        self._context = context

    async def new_page(self, platform: Platform) -> Page:  # noqa: ARG002 — kept for parity
        return await self._context.new_page()

    async def close(self) -> None:
        try:
            await self._context.close()
        except Exception as e:  # pragma: no cover
            logger.warning("[CloakBrowser] context.close() raised: %s", e)


class BrowserSessionPool:
    """Account-keyed pool of ``BrowserManager`` instances, each running one Chrome.

    Use ``acquire_tab`` for generation, ``evict`` before reauth, and
    ``shutdown_all`` on app shutdown.
    """

    def __init__(
        self,
        concurrency_per_account: int = 1,
        idle_shutdown_minutes: int = 15,
        profile_suffix: str = "_puppeteer",
        profile_root: Optional[str] = None,
    ):
        self._sessions: dict[int, _AccountSession] = {}
        self._global_lock = asyncio.Lock()  # guards dict mutation + session init
        self._init_locks: dict[int, asyncio.Lock] = {}
        self._concurrency = max(1, int(concurrency_per_account))
        self._idle_seconds = max(60, int(idle_shutdown_minutes) * 60)
        self._profile_suffix = profile_suffix
        self._profile_root = Path(profile_root) if profile_root else None
        self._closing = False

    # ---------- profile path resolution ----------

    # Default root used when ``browser_profile_root`` is not set in config.
    # Must match the hardcoded fallback in ``scripts/browser_login.py`` so
    # both tools point at the same directory tree.
    _DEFAULT_PROFILE_ROOT = Path("/opt/veoapi_browser_profiles")

    def _resolve_profile_path(self, account: AccountDB) -> Path:
        """Pick the ``--user-data-dir`` for this account.

        Preference order:
        1. ``config.browser_profile_root`` (from setting.toml / env) →
           ``{root}/{account_id}_puppeteer``
        2. ``/opt/veoapi_browser_profiles/{account_id}_puppeteer`` — the
           same default that ``scripts/browser_login.py`` uses, so both
           tools always agree on the profile location even when the config
           key is absent.

        The old legacy path (``browser_profiles/<email>_puppeteer`` under
        the repo root) is intentionally dropped: it differed from the login
        script's path, causing ``has_session_token=False`` on every cold
        start because the pool was looking in the wrong directory.
        """
        root = self._profile_root or self._DEFAULT_PROFILE_ROOT
        return Path(root) / f"{account.id}{self._profile_suffix}"

    # ---------- lifecycle ----------

    async def acquire_tab(
        self,
        account: AccountDB,
    ) -> "_TabLease":
        """Return a context manager yielding ``(page, context)`` for one generation.

        Usage::

            async with pool.acquire_tab(account) as (page, context):
                client = FlowVideoClient(page, context, ...)
                result = await client.generate_video(...)
        """
        if self._closing:
            raise RuntimeError("BrowserSessionPool is closing")
        session = await self._get_or_create_session(account)
        return _TabLease(session, pool=self)

    # Chrome's cookie-monster flushes to disk on an async ~5s timer. If we
    # SIGTERM Chrome before that timer fires, rotated/refreshed session
    # cookies never reach Cookies.sqlite and the next launch opens a
    # logged-out profile. ``scripts/browser_login.py`` learned this the
    # hard way — match its 10s grace here on every close path.
    _COOKIE_FLUSH_SECONDS = 10

    async def evict(self, account_id: int, timeout: float = 60.0) -> None:
        """Close the Chrome session for an account. Waits for active generations
        to finish (up to ``timeout`` seconds) before shutting down, then
        gives Chrome time to flush cookies to disk before closing."""
        async with self._global_lock:
            session = self._sessions.pop(account_id, None)
        if not session:
            return
        logger.info("[BrowserPool] Evicting session for account_id=%s", account_id)
        async with session.reauth_lock:
            # Wait for active generations to drain
            if session.active_count > 0:
                try:
                    await asyncio.wait_for(session.active_count_zero.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[BrowserPool] Eviction timeout: %d active generations remain",
                        session.active_count,
                    )
            await self._close_session(session)

    async def shutdown_all(self) -> None:
        """Close all sessions. Called on app shutdown."""
        self._closing = True
        async with self._global_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        # Flush once for the batch rather than 10s * N serially; the flush
        # timer inside Chrome runs in parallel across all processes.
        if sessions:
            logger.info(
                "[BrowserPool] Flushing cookies for %d session(s) before shutdown...",
                len(sessions),
            )
            try:
                await asyncio.sleep(self._COOKIE_FLUSH_SECONDS)
            except asyncio.CancelledError:
                pass
        for session in sessions:
            try:
                await session.manager.close()
            except Exception as e:
                logger.warning("[BrowserPool] Error closing %s: %s", session.email, e)

    async def _close_session(self, session: _AccountSession) -> None:
        """Flush cookies to disk, then terminate Chrome."""
        try:
            await asyncio.sleep(self._COOKIE_FLUSH_SECONDS)
        except asyncio.CancelledError:
            pass
        try:
            await session.manager.close()
        except Exception as e:
            logger.warning("[BrowserPool] Error closing manager: %s", e)

    async def sweep_idle(self) -> int:
        """Close sessions that have been idle beyond the threshold. Returns count closed."""
        now = time.monotonic()
        to_evict: list[int] = []
        async with self._global_lock:
            for aid, session in self._sessions.items():
                if session.active_count > 0:
                    continue
                if now - session.last_used_monotonic > self._idle_seconds:
                    to_evict.append(aid)
        for aid in to_evict:
            await self.evict(aid, timeout=5.0)
        if to_evict:
            logger.info("[BrowserPool] Swept %d idle sessions", len(to_evict))
        return len(to_evict)

    # ---------- internals ----------

    async def _get_or_create_session(self, account: AccountDB) -> _AccountSession:
        # Fast path
        session = self._sessions.get(account.id)
        if session is not None:
            return session

        # Per-account init lock to avoid launching two Chromes for the same account
        async with self._global_lock:
            init_lock = self._init_locks.setdefault(account.id, asyncio.Lock())

        async with init_lock:
            session = self._sessions.get(account.id)
            if session is not None:
                return session
            session = await self._create_session(account)
            async with self._global_lock:
                self._sessions[account.id] = session
            return session

    async def _create_session(self, account: AccountDB) -> _AccountSession:
        profile_path = self._resolve_profile_path(account)
        profile_path.mkdir(parents=True, exist_ok=True)
        proxy_url = _normalize_proxy(account.residential_proxy or account.proxy)

        # Lazy import to avoid Config singleton init at module load
        from ...core.config import config as _cfg

        use_cloak = _cfg.use_cloakbrowser and _CLOAK_AVAILABLE
        if _cfg.use_cloakbrowser and not _CLOAK_AVAILABLE:
            logger.warning(
                "[BrowserPool] use_cloakbrowser=true but cloakbrowser is not "
                "installed — falling back to BrowserManager. Run: "
                "pip install 'cloakbrowser[geoip]>=0.3.31'"
            )

        logger.info(
            "[BrowserPool] Launching %s for %s (profile=%s, proxy=%s)",
            "CloakBrowser" if use_cloak else "Chrome",
            account.email, profile_path, _mask_proxy(proxy_url),
        )

        if use_cloak:
            manager, context, _page = await self._launch_cloakbrowser(
                profile_path=profile_path,
                proxy_url=proxy_url,
                humanize=_cfg.cloak_humanize,
                geoip=_cfg.cloak_geoip,
            )
        else:
            manager = BrowserManager(
                data_dir=profile_path.parent,
                proxy_url=proxy_url,
                use_cdp=True,
                user_data_dir_override=profile_path,
            )

            # Intentionally use launch_for_login, NOT launch_for_crawl:
            # launch_for_crawl starts Chrome with headless=True + the full set of
            # anti-detection flags (AutomationControlled etc.). Google fingerprints
            # the session using those, so the __Secure-next-auth.session-token
            # created during the HEADED login gets silently rejected and
            # labs.google/fx/tools/flow serves the anonymous landing page.
            # launch_for_login matches the login fingerprint (headed + minimal
            # flags) — under Xvfb that still runs fine on a headless server.
            context, _page = await manager.launch_for_login(Platform.FLOW)

        # --- Purge zombie tabs from previous processes ------------------
        # When veoapi is stopped with SIGTERM/systemctl stop, the Chrome
        # process stays alive (the user-data-dir is persistent, Chrome is
        # not a child of the python worker). All tabs that were generating
        # at that moment are left open. On reconnect via CDP we see them in
        # ``context.pages`` — they still hold open Flow projects, are still
        # tied to the account's websocket, and cause cross-tab pollution:
        # Flow syncs "current project" across tabs, so our fresh tab lands
        # on a project that already has those zombie generations running.
        # Close every existing page before handing the session out.
        #
        # Skip the purge for the CloakBrowser path: it launches its own
        # Chromium subprocess (pipe-based CDP), so by definition there are
        # no pre-existing tabs from a previous Python process. The only
        # page present is the initial ``about:blank`` Playwright opens with
        # the persistent context — closing it caused the pipe to drop and
        # next ``new_page()`` to raise "Target ... has been closed".
        if not use_cloak:
            stale_pages = list(context.pages)
            closed_zombies = 0
            for p in stale_pages:
                # Keep the page we just got from launch_for_login — closing it
                # may confuse the BrowserManager's internal state.
                if p is _page:
                    continue
                try:
                    await p.close()
                    closed_zombies += 1
                except Exception as e:
                    logger.debug(
                        "[BrowserPool] Could not close stale page for %s: %s",
                        account.email, e,
                    )
            if closed_zombies:
                logger.info(
                    "[BrowserPool] Closed %d zombie tab(s) left over from previous "
                    "process for %s",
                    closed_zombies, account.email,
                )

        # Inject session token from file — Chrome's cookie-monster flushes to
        # SQLite on an async ~5s timer, but the session-token cookie may not
        # have been written before the previous Chrome process exited. The auth
        # module reliably writes session_token.txt on every successful login, so
        # we inject it via CDP here to guarantee the context has it regardless of
        # whether Chrome persisted it to Default/Cookies.
        token_file = profile_path / "session_token.txt"
        if token_file.exists():
            try:
                token = token_file.read_text(encoding="utf-8").strip()
                if token:
                    await context.add_cookies([{
                        "name": "__Secure-next-auth.session-token",
                        "value": token,
                        "domain": "labs.google",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }])
                    logger.info(
                        "[BrowserPool] Injected session token from %s for %s",
                        token_file, account.email,
                    )
            except Exception as e:
                logger.warning(
                    "[BrowserPool] Could not inject session token for %s: %s",
                    account.email, e,
                )

        return _AccountSession(
            account_id=account.id,
            email=account.email,
            profile_path=profile_path,
            proxy_url=proxy_url,
            manager=manager,
            context=context,
            semaphore=asyncio.Semaphore(self._concurrency),
        )

    async def _launch_cloakbrowser(
        self,
        *,
        profile_path: Path,
        proxy_url: Optional[str],
        humanize: bool,
        geoip: bool,
    ) -> tuple["_CloakManagerAdapter", BrowserContext, Optional[Page]]:
        """Launch a CloakBrowser persistent context for one account.

        Headed mode is required for two reasons:
        1. Flow's login fingerprint expects a non-headless browser — the
           ``__Secure-next-auth.session-token`` written during HEADED login
           gets rejected by anonymous-landing logic if we reconnect headless.
        2. CloakBrowser's own README notes that some sites detect headless
           even with C++ patches. Under Xvfb on the prod server this still
           runs without a real display.
        """
        if launch_persistent_context_async is None:  # safety net
            raise RuntimeError("cloakbrowser is not installed")

        # CloakBrowser launches its own Chromium subprocess. If a previous
        # process from veoapi (or a stale browser2api launch) still holds the
        # ``--user-data-dir`` lock, the new process refuses to start with
        # "user data dir is already in use". browser2api kills the holder
        # before its own CDP launch; mirror that behaviour here so the swap
        # doesn't introduce a new failure mode.
        try:
            from browser2api.browser import find_chrome_process_for_user_data_dir  # type: ignore
            existing = find_chrome_process_for_user_data_dir(profile_path)
            if existing:
                pid, _ = existing
                logger.info(
                    "[BrowserPool] Killing stale Chrome (PID %d) holding %s",
                    pid, profile_path,
                )
                import os as _os
                try:
                    _os.kill(pid, 9)
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.warning("[BrowserPool] Could not kill PID %d: %s", pid, e)
        except Exception as e:  # pragma: no cover
            logger.debug("[BrowserPool] Stale Chrome check skipped: %s", e)

        kwargs: dict = {
            "headless": False,
            "humanize": humanize,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
            # geoip only makes sense with an actual proxy — it resolves the
            # proxy's exit IP to set timezone/locale and spoof WebRTC.
            kwargs["geoip"] = geoip

        # ``launch_persistent_context_async`` returns a Playwright
        # BrowserContext; the underlying Playwright instance and Chromium
        # subprocess are owned by the context and torn down on ``.close()``.
        context: BrowserContext = await launch_persistent_context_async(
            str(profile_path),
            **kwargs,
        )
        # The initial page (if any) is treated like any other tab; we don't
        # need to keep a reference. The zombie-purge loop will leave it alone
        # by default (``_page is None``), which is what we want for a fresh
        # CloakBrowser launch — no stale Flow tabs yet.
        manager = _CloakManagerAdapter(context)
        return manager, context, None


class _TabLease:
    """Async context manager that yields ``(page, context)`` for one generation.

    Takes a slot from the per-account semaphore and opens a fresh tab in the
    shared context. Closes the tab and releases the slot on exit.
    """

    # Substrings of TargetClosedError / similar Playwright errors that mean
    # the cached BrowserContext is dead and must be evicted before the next
    # acquire_tab — otherwise every subsequent request keeps getting the
    # same dead session.
    _DEAD_SESSION_MARKERS: tuple[str, ...] = (
        "target page, context or browser has been closed",
        "browser has been closed",
        "browser closed",
        "browsercontext.new_page",
        "context closed",
        "browser disconnected",
    )

    def __init__(self, session: _AccountSession, pool: "BrowserSessionPool | None" = None):
        self._session = session
        self._pool = pool
        self._page: Page | None = None
        self._slot_held = False

    async def __aenter__(self) -> tuple[Page, BrowserContext]:
        # Take concurrency slot (respects per-account parallelism limit)
        await self._session.semaphore.acquire()
        self._slot_held = True
        # Mark active before we await new_page so evict() knows to wait for us
        self._session.active_count += 1
        self._session.active_count_zero.clear()
        try:
            self._page = await self._session.manager.new_page(Platform.FLOW)
            return self._page, self._session.context
        except Exception as e:
            # Rollback on failure
            self._session.active_count -= 1
            if self._session.active_count <= 0:
                self._session.active_count = 0
                self._session.active_count_zero.set()
            self._session.semaphore.release()
            self._slot_held = False

            # If the underlying Chrome / context is dead, the cached
            # session in the pool is poisoned — every future
            # acquire_tab() on this account will raise the same error
            # until we evict and relaunch. Detect that here and trigger
            # an eviction so the *next* attempt (with a different
            # account, or even a retry on the same one) gets a fresh
            # Chrome instead of the same corpse.
            err_text = f"{type(e).__name__}: {e}".lower()
            if self._pool is not None and any(m in err_text for m in self._DEAD_SESSION_MARKERS):
                logger.warning(
                    "[BrowserPool] Dead Chrome session detected for %s "
                    "(account_id=%s) on new_page: %s — evicting so the "
                    "next acquire relaunches",
                    self._session.email, self._session.account_id, e,
                )
                try:
                    await self._pool.evict(self._session.account_id, timeout=2.0)
                except Exception as evict_exc:  # pragma: no cover
                    logger.debug(
                        "[BrowserPool] Eviction after dead session failed: %s",
                        evict_exc,
                    )
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self._page is not None:
                try:
                    await self._page.close()
                except Exception as e:
                    logger.debug("[BrowserPool] Error closing tab: %s", e)
        finally:
            self._session.active_count -= 1
            if self._session.active_count <= 0:
                self._session.active_count = 0
                self._session.active_count_zero.set()
            self._session.last_used_monotonic = time.monotonic()
            if self._slot_held:
                self._session.semaphore.release()
                self._slot_held = False


# ---------- helpers ----------


def _normalize_proxy(raw: Optional[str]) -> Optional[str]:
    """Convert Evomi-style ``host:port:user:pass`` to RFC ``http://user:pass@host:port``.
    Delegates to the existing helper used by VeoClient so both providers agree."""
    if not raw:
        return None
    try:
        from ..veo_client import normalize_proxy_url  # type: ignore
        return normalize_proxy_url(raw)
    except Exception:
        return raw


def _mask_proxy(url: Optional[str]) -> str:
    if not url:
        return "none"
    # Keep only host:port for logs
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = p.hostname or "?"
        port = p.port or "?"
        return f"{host}:{port}"
    except Exception:
        return "<set>"


# ---------- module-level singleton accessor ----------

_pool: Optional[BrowserSessionPool] = None


def get_pool() -> BrowserSessionPool:
    global _pool
    if _pool is None:
        cfg = Config()
        _pool = BrowserSessionPool(
            concurrency_per_account=cfg.browser_concurrency_per_account,
            idle_shutdown_minutes=cfg.browser_idle_shutdown_minutes,
            profile_suffix=cfg.browser_profile_suffix,
            profile_root=cfg.browser_profile_root,
        )
    return _pool


def reset_pool() -> None:
    """For tests / reinitialisation."""
    global _pool
    _pool = None
