"""
Google Flow image and video generation clients using browser automation.

Flow is a project-based generation UI at labs.google/fx/tools/flow.
The user lands on a project listing page, clicks "+ New project" to enter
the editor, then uses a bottom prompt bar with a contenteditable div,
a model selector button, and a submit (arrow_forward) button.

Images appear in the workspace and are served via signed GCS URLs.
Videos are served via storage.googleapis.com signed URLs.
"""

import asyncio
import datetime
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from playwright.async_api import BrowserContext, Page
from tenacity import retry, stop_after_attempt, wait_fixed

from ...base import AbstractImageClient
from ...config import DATA_DIR
from ...types import (
    GeneratedImage,
    GeneratedVideo,
    GenerationResult,
    GenerationStatus,
    Platform,
    VideoGenerationResult,
)
from .enums import (
    COUNT_UI_LABELS,
    MODEL_UI_LABELS,
    ORIENTATION_UI_LABELS,
    VIDEO_MODEL_UI_LABELS,
    FlowCount,
    FlowModel,
    FlowOrientation,
    FlowVideoModel,
)
from .selectors import DOMAIN, TOOL_URL

logger = logging.getLogger(__name__)

# Local download directory (created lazily on first download)
DOWNLOADS_DIR = DATA_DIR / "downloads" / "flow"

# JS snippet to collect image URLs currently visible in the DOM.
# Flow serves images via labs.google/fx/api/trpc/media redirect URLs
# and also googleusercontent.com CDN.
_COLLECT_IMAGES_JS = """() => {
    const imgs = document.querySelectorAll('img');
    const urls = [];
    for (const img of imgs) {
        const src = img.src || '';
        if (src.length > 30) {
            const rect = img.getBoundingClientRect();
            if (rect.width > 100 && rect.height > 100
                && (src.includes('labs.google/fx/api/')
                    || src.includes('googleusercontent.com')
                    || src.includes('lh3.google')
                    || src.includes('gstatic.com'))) {
                urls.push(src);
            }
        }
    }
    return urls;
}"""


class FlowAPIError(Exception):
    """Raised when Flow generation fails."""
    pass


class FlowBaseClient:
    """Shared functionality for Flow image and video clients.

    Provides common methods for page navigation, config panel interaction,
    prompt filling, submit button clicking, file downloading, and output
    directory management.
    """

    platform = Platform.FLOW

    # Directory for generation screenshots. Defaults to DATA_DIR/screenshots/flow;
    # override by setting FlowBaseClient.SCREENSHOTS_DIR before instantiation.
    SCREENSHOTS_DIR: Path = DATA_DIR / "screenshots" / "flow"

    def __init__(self, page: Page, context: BrowserContext, output_dir: Path | None = None):
        self.page = page
        self.context = context
        self.output_dir = output_dir or DOWNLOADS_DIR
        self._ss_seq: int = 0  # per-instance step counter for ordering

    async def _screenshot(self, step: str) -> None:
        """Capture a full-page screenshot labelled with step name.

        Directory: self._screenshots_dir if set (per-instance), else SCREENSHOTS_DIR.
        """
        try:
            ts = datetime.datetime.utcnow().strftime("%H%M%S")
            self._ss_seq += 1
            base = getattr(self, "_screenshots_dir", self.SCREENSHOTS_DIR)
            out = Path(base) / f"{ts}_{self._ss_seq:02d}_{step}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(out), full_page=True)
            logger.debug("[Flow] screenshot → %s", out.name)
        except Exception as e:
            logger.debug("[Flow] screenshot '%s' failed: %s", step, e)

    async def _dismiss_cookie_consent(self) -> bool:
        """Click Google's cookie-consent banner ("OK, got it") if present.

        The banner is a fixed overlay pinned to the bottom of the page that
        covers the prompt box and submit button, so generation silently fails
        to interact with the UI until it's gone. Matching is limited to
        "got it"-style buttons to avoid clicking unrelated controls. Safe to
        call repeatedly — it's a no-op once the banner is dismissed (Google
        persists the choice in a cookie).
        """
        try:
            clicked = await self.page.evaluate("""() => {
                const norm = s => (s || '').trim().toLowerCase();
                const sel = 'button, a[role="button"], div[role="button"]';
                for (const btn of document.querySelectorAll(sel)) {
                    const t = norm(btn.textContent);
                    if (t === 'ok, got it' || t === 'got it' || t.includes('got it')) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            btn.click();
                            return t;
                        }
                    }
                }
                return null;
            }""")
            if clicked:
                logger.info("[Flow] Dismissed cookie-consent banner (%r)", clicked)
                return True
        except Exception as e:
            logger.debug("[Flow] cookie-consent dismiss failed: %s", e)
        return False

    async def _ensure_generation_page(self) -> None:
        """Navigate to Flow and ensure we're inside a project editor.

        Flow has two states:
        1. Project listing (URL ends with /flow) — need to enter a project
        2. Project editor (URL contains /project/<uuid>) — ready to generate

        If on the listing page, dismisses any announcement banner, then opens
        the most recent existing project. Only creates a new project if none exist.
        """
        current = self.page.url

        # Diagnostic: log which auth cookies are present BEFORE we navigate,
        # so we can tell a "stale landing page" from "session cookie missing".
        try:
            cookies = await self.context.cookies("https://labs.google")
            cookie_names = sorted(
                c.get("name", "") for c in cookies if c.get("name")
            )
            has_st = any("next-auth.session-token" in n for n in cookie_names)
            logger.info(
                "[Flow] pre-nav cookies on labs.google: count=%d has_session_token=%s names=%s",
                len(cookie_names), has_st, cookie_names[:15],
            )
            if not has_st:
                logger.error(
                    "[Flow] labs.google session-token MISSING from profile cookies — "
                    "profile is logged out. Re-run scripts/browser_login.py for this "
                    "account before retrying generation."
                )
        except Exception as e:
            logger.debug("[Flow] cookie dump failed: %s", e)

        if "labs.google/fx/tools/flow" not in current:
            try:
                await self.page.goto(
                    TOOL_URL,
                    wait_until="commit",
                    timeout=60000,
                )
            except Exception as e:
                logger.warning("[Flow] Navigation note: %s", e)
            await asyncio.sleep(5)
            current = self.page.url
        logger.info("[Flow] _ensure_generation_page: after nav url=%s", current)

        # Google's cookie-consent banner overlays the prompt box / submit
        # button on first visit — dismiss it before doing anything else.
        await self._dismiss_cookie_consent()

        # Already inside a project
        if "/project/" in current:
            logger.info("[Flow] Already in project editor")
            return

        # On listing page — dismiss banner, then always create a new project
        logger.info("[Flow] On listing page, creating new project...")

        # Dismiss announcement banner if present
        await self.page.evaluate("""() => {
            for (const btn of document.querySelectorAll('button')) {
                if ((btn.textContent || '').trim() === 'close') {
                    btn.click();
                    return true;
                }
            }
            return false;
        }""")

        # Wait for "+ New project" button to appear (page may still be loading)
        clicked = None
        for _ in range(20):
            clicked = await self.page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button')) {
                    const text = (btn.textContent || '').trim();
                    const rect = btn.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    if (text.includes('add') || text.includes('+')) {
                        btn.click();
                        return 'new';
                    }
                }
                return null;
            }""")
            if clicked:
                break
            await asyncio.sleep(0.5)

        if clicked == 'new':
            await asyncio.sleep(5)
            logger.info("[Flow] Created new project, URL: %s", self.page.url)
        else:
            # DOM click failed — Flow's landing page markup drifts every
            # few weeks. Fall back to the project.createProject tRPC call
            # directly, then goto() the returned /project/<id>. This uses
            # the same auth cookies as the UI (same-origin fetch from
            # inside the page), so it's functionally identical to what a
            # real click would do.
            logger.warning(
                "[Flow] Could not find 'New project' button — "
                "falling back to project.createProject tRPC"
            )
            pid = await self._create_project_via_api()
            if pid:
                project_url = f"{DOMAIN}/fx/tools/flow/project/{pid}"
                logger.info("[Flow] API created project %s → navigating to %s", pid, project_url)
                try:
                    await self.page.goto(project_url, wait_until="commit", timeout=60_000)
                except Exception as e:
                    logger.warning("[Flow] goto(new project) note: %s", e)
                await asyncio.sleep(4)
            else:
                logger.error(
                    "[Flow] project.createProject fallback also failed — "
                    "downstream selectors will time out"
                )

        # Wait for the prompt bar to appear.
        # New projects can take 30s+ to fully load the editor UI.
        # We check for the contenteditable div first (needed for prompt input),
        # then separately wait for the config button.
        for i in range(40):
            ready = await self.page.evaluate("""() => {
                const ce = document.querySelector('[contenteditable="true"]');
                if (ce) {
                    const r = ce.getBoundingClientRect();
                    return r.width > 50;
                }
                return false;
            }""")
            if ready:
                logger.info("[Flow] Prompt input ready after %ds", i)
                break
            if i == 20:
                logger.info("[Flow] Still waiting for prompt bar... (%ds)", i)
            await asyncio.sleep(1)
        else:
            logger.warning(
                "[Flow] Prompt input did not appear within 40s (url=%s)", self.page.url
            )
            await self._dump_failure_state("prompt_input_timeout")
            return

        # Flow now opens an AI-assistant ("agent mode") side panel by default
        # on every new project. While the panel is open the bottom bar is
        # the agent prompt — there is no model/orientation/count config
        # button, so _select_video_model etc. all fail and generation falls
        # back to the agent endpoint (Gemini wrapper) with extra safety
        # filtering. Close the panel BEFORE waiting for the config button.
        await self._dismiss_agent_panel()

        # Now wait a bit more for the config button (may lag behind).
        # The config button is a haspopup="menu" button in the bottom prompt bar
        # with text like "Videocrop_16_9x1" or "Imagen 4crop_16_9x2".
        for i in range(15):
            has_config = await self.page.evaluate("""() => {
                const viewH = window.innerHeight;
                for (const b of document.querySelectorAll('button[aria-haspopup="menu"]')) {
                    const rect = b.getBoundingClientRect();
                    if (rect.y > viewH * 0.7 && rect.width > 30 && rect.height > 20) {
                        const text = (b.textContent || '').trim();
                        if (text.includes('crop_') || text.includes('Image')
                            || text.includes('Video') || text.includes('Banana')
                            || text.includes('Imagen') || text.includes('Veo')) {
                            return true;
                        }
                    }
                }
                return false;
            }""")
            if has_config:
                logger.info("[Flow] Config button ready")
                break
            await asyncio.sleep(1)
        else:
            logger.warning("[Flow] Config button did not appear within 15s")

    async def _dismiss_agent_panel(self) -> None:
        """Close the right-side AI assistant session panel and flip the
        bottom prompt bar out of agent mode.

        Robust version (2026-05-28)
        ---------------------------
        Earlier implementation was single-shot: click close, click toggle,
        verify once, give up on failure. In practice Flow re-renders the
        panel mid-click (styled-components hot-rehash on locale change,
        WebSocket reconnect, etc.), so a single click frequently lands on
        a detached element or hits the panel a frame before it animates
        back in. Symptom: ``_has_config_button()`` returns False, the
        generate flow proceeds with the agent pill still on, and the
        next ``locator.fill`` aims at the agent input instead of the Veo
        prompt — generation silently submits to Gemini-wrapper, never
        reaches Veo.

        New approach: a multi-round dismiss loop. Each round does:

        1. Spam Escape 2-3x with small gaps. Cheap, often dismisses popovers
           and the "+ media" picker without any selector work.
        2. Try to click the panel close button. Selector set is broadened
           (textContent ``^close``, aria-label, common Material Symbols
           glyphs, X/✕ unicode close icons). Each strategy tries DOM-handle
           click first, then page.mouse.click on the element's bbox center
           if Playwright's actionability check fails.
        3. Wait for the slide-out animation, then check ``_has_config_button``.
        4. If still in agent mode, click the agent toggle pill — same
           multi-strategy + bbox-fallback pattern.
        5. Verify. If config button finally visible → done. Otherwise
           next round.

        After ``_MAX_DISMISS_ROUNDS`` exhausted, last-resort: reload the
        page. Flow re-opens projects with the panel closed by default
        roughly 50% of the time; better than silently submitting an
        agent-mode generation.
        """
        _MAX_DISMISS_ROUNDS = 3

        for round_idx in range(1, _MAX_DISMISS_ROUNDS + 1):
            # Fast-path: if config button is already visible, we're done.
            if await self._has_config_button():
                if round_idx > 1:
                    logger.info(
                        "[Flow] agent panel dismissed by round %d (config button visible)",
                        round_idx,
                    )
                return

            logger.info(
                "[Flow] _dismiss_agent_panel round %d/%d",
                round_idx, _MAX_DISMISS_ROUNDS,
            )

            # ---- 1) Escape spam ----------------------------------------
            # Two presses with a tiny gap clears the "+" media picker
            # popover AND any nested modal that may have stolen focus.
            # Cheap; if there's nothing to dismiss, it's a no-op.
            for _ in range(2):
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(0.15)

            # Quick re-check — Escape alone sometimes closes the panel
            # on Flow's experimental builds.
            if await self._has_config_button():
                logger.info("[Flow] panel dismissed by Escape alone (round %d)", round_idx)
                return

            # ---- 2) Click the panel close button -----------------------
            panel_closed = await self._click_panel_close()
            if panel_closed:
                # Slide-out animation is ~600ms on Flow's default theme.
                await asyncio.sleep(1.0)
            else:
                logger.info(
                    "[Flow] panel close button not found in round %d "
                    "(panel may already be closed)",
                    round_idx,
                )

            # Another Escape after the panel goes — sometimes the close
            # button only collapses the panel into the agent mode pill.
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            await asyncio.sleep(0.2)

            # ---- 3) Verify after panel close ---------------------------
            if await self._has_config_button():
                logger.info(
                    "[Flow] config button visible after panel close (round %d) — done",
                    round_idx,
                )
                return

            # ---- 4) Click the agent toggle pill ------------------------
            logger.info(
                "[Flow] config button still missing — clicking agent toggle (round %d)",
                round_idx,
            )
            toggle_clicked = await self._click_agent_toggle()
            if not toggle_clicked:
                logger.warning(
                    "[Flow] agent toggle not found in round %d", round_idx,
                )

            # Give the prompt bar time to re-render in non-agent mode.
            await asyncio.sleep(1.5)

            # ---- 5) Final verify for this round ------------------------
            if await self._has_config_button():
                logger.info(
                    "[Flow] agent mode flipped off (round %d) — done",
                    round_idx,
                )
                return

            # Backoff between rounds. SPA re-renders can take ~1s; giving
            # the DOM time to settle dramatically improves second-round
            # success rate.
            await asyncio.sleep(0.8 + 0.4 * round_idx)

        # ---- Last resort: reload the page ------------------------------
        # We exhausted the dismiss loop. Reloading Flow with a fresh
        # navigation tends to come up with the panel closed (it's the
        # default for projects the user has visited at least once). The
        # caller waits for ``_has_config_button`` later anyway, so this
        # is safe to do unconditionally on the failure path.
        logger.warning(
            "[Flow] _dismiss_agent_panel: all %d rounds failed — "
            "reloading page as last resort",
            _MAX_DISMISS_ROUNDS,
        )
        await self._dump_agent_diag(missing_panel=False, missing_toggle=True)
        try:
            await self.page.reload(timeout=20000, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)
            if await self._has_config_button():
                logger.info("[Flow] reload recovered the config button")
                return
            # One more Escape + toggle attempt on the reloaded page —
            # if Flow chose to re-open the panel after reload, give it
            # one more shot before giving up entirely.
            for _ in range(3):
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            await self._click_panel_close()
            await asyncio.sleep(0.8)
            if not await self._has_config_button():
                await self._click_agent_toggle()
                await asyncio.sleep(1.5)
            if not await self._has_config_button():
                logger.error(
                    "[Flow] _dismiss_agent_panel: panel STILL up after "
                    "reload + retry — generation will likely submit in "
                    "agent mode"
                )
        except Exception as e:
            logger.error("[Flow] _dismiss_agent_panel reload failed: %s", e)

    async def _has_config_button(self) -> bool:
        """True if the regular Veo/Imagen prompt-bar config button is in
        the DOM. Mirrors the check inside ``_ensure_generation_page``."""
        try:
            return bool(await self.page.evaluate(
                """() => {
                    const viewH = window.innerHeight;
                    for (const b of document.querySelectorAll('button[aria-haspopup="menu"]')) {
                        const rect = b.getBoundingClientRect();
                        if (rect.y > viewH * 0.7 && rect.width > 30 && rect.height > 20) {
                            const text = (b.textContent || '').trim();
                            if (text.includes('crop_') || text.includes('Image')
                                || text.includes('Video') || text.includes('Banana')
                                || text.includes('Imagen') || text.includes('Veo')) {
                                return true;
                            }
                        }
                    }
                    return false;
                }"""
            ))
        except Exception:
            return False

    async def _click_panel_close(self) -> bool:
        """Click the close button on the agent panel.

        Multi-strategy match, each with a bbox-coords fallback so the
        click lands even when Playwright's actionability retry loses the
        element on re-render. Returns True on the FIRST successful
        attempt; caller decides whether to verify and retry.

        Strategies, in order:

        1. ``textContent`` starts with the Material Symbols ``close``
           ligature (locale-invariant). This is the historical match.
        2. ``aria-label`` matches a known localised "close" word.
        3. Pure-icon close buttons whose textContent is just ``close`` /
           ``cancel`` / ``×`` / ``✕`` / ``X``.
        4. Position-only fallback: any small (32x32-ish) button in the
           top-right sliver of the viewport that has no children of its
           own. Last resort — relies entirely on layout.

        For each match we try ``element.click(force=True)`` first; if
        that throws (detached, intercepted, off-screen), we fall back to
        ``page.mouse.click(bbox_center_x, bbox_center_y)`` which doesn't
        care about element identity.
        """
        strategies = [
            # 1) Material Symbols 'close' ligature prefix
            (
                "ms-close-prefix",
                """() => {
                    const vw = window.innerWidth;
                    for (const b of document.querySelectorAll('button')) {
                        const r = b.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        if (r.x < vw * 0.55 || r.y > 240) continue;
                        const text = (b.textContent || '').trim();
                        if (!/^close[^a-z]/.test(text)) continue;
                        return b;
                    }
                    return null;
                }""",
            ),
            # 2) aria-label localised match
            (
                "aria-label-close",
                """() => {
                    const vw = window.innerWidth;
                    const KEYWORDS = ['close','dismiss','schliessen','schließen',
                        'cerrar','fermer','chiudi','fechar','закрыть','закрити',
                        'kapat','tutup','dong','dóng','đóng','关闭','關閉','閉じる','닫기'];
                    for (const b of document.querySelectorAll('button[aria-label],button[title]')) {
                        const r = b.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        if (r.x < vw * 0.55 || r.y > 240) continue;
                        const label = ((b.getAttribute('aria-label') || '') + ' ' +
                                       (b.getAttribute('title') || '')).toLowerCase();
                        for (const k of KEYWORDS) {
                            if (label.includes(k)) return b;
                        }
                    }
                    return null;
                }""",
            ),
            # 3) Icon-only close glyphs
            (
                "icon-glyph",
                """() => {
                    const vw = window.innerWidth;
                    const GLYPHS = ['close','cancel','x','×','✕','✖','⨯'];
                    for (const b of document.querySelectorAll('button')) {
                        const r = b.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        if (r.x < vw * 0.55 || r.y > 240) continue;
                        const t = (b.textContent || '').trim().toLowerCase();
                        if (GLYPHS.includes(t)) return b;
                    }
                    return null;
                }""",
            ),
            # 4) Position-only: 24-40px square button in top-right area
            (
                "position-only",
                """() => {
                    const vw = window.innerWidth;
                    let best = null;
                    let bestX = 0;
                    for (const b of document.querySelectorAll('button')) {
                        const r = b.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        if (r.width < 20 || r.width > 56) continue;
                        if (r.height < 20 || r.height > 56) continue;
                        // Restrict to the panel header area: right 35% of
                        // viewport, top 240px.
                        if (r.x < vw * 0.65 || r.y > 240) continue;
                        // Prefer the right-most candidate (header close
                        // button sits flush against the panel edge).
                        if (r.x > bestX) {
                            bestX = r.x;
                            best = b;
                        }
                    }
                    return best;
                }""",
            ),
        ]

        for name, js in strategies:
            try:
                handle = await self.page.evaluate_handle(js)
                element = handle.as_element()
                if element is None:
                    continue
                # Snapshot bbox now in case the element gets detached
                # during the click attempt — we use it for the mouse
                # fallback below.
                try:
                    bbox = await element.bounding_box()
                except Exception:
                    bbox = None
                try:
                    await element.click(timeout=4000, force=True)
                    logger.info(
                        "[Flow] panel close clicked via %s", name,
                    )
                    await asyncio.sleep(0.4)
                    return True
                except Exception as e:
                    if bbox is not None:
                        try:
                            cx = bbox["x"] + bbox["width"] / 2
                            cy = bbox["y"] + bbox["height"] / 2
                            await self.page.mouse.click(cx, cy)
                            logger.info(
                                "[Flow] panel close clicked via mouse.click "
                                "@(%.0f,%.0f) — %s strategy (element.click "
                                "errored: %s)",
                                cx, cy, name, str(e)[:80],
                            )
                            await asyncio.sleep(0.4)
                            return True
                        except Exception as e2:
                            logger.debug(
                                "[Flow] mouse.click fallback failed for %s: %s",
                                name, e2,
                            )
                    else:
                        logger.debug(
                            "[Flow] panel close %s click failed and bbox unavailable: %s",
                            name, e,
                        )
            except Exception as e:
                logger.debug("[Flow] panel close strategy %s errored: %s", name, e)

        return False

    async def _click_agent_toggle(self) -> bool:
        """Click the agent-mode pill in the bottom prompt bar.

        Per the 2026-05-27 server-side DOM dump the toggle is a 66x32
        pill button at the left of the prompt bar with class
        ``sc-59223abb-3 bdRbOx`` and accessible name ``"Agent"`` (or its
        localized equivalent — ``"Tác nhân"`` on Vietnamese accounts,
        etc.). Earlier captures pointed at ``sc-8349e370-0`` but that
        leaf belongs to the adjacent ``+`` media-picker button — same
        wrapper, similar styled-components family, different leaf hash.

        Selector stack:
          1. Leaf class ``sc-59223abb-3`` — most stable across deploys.
          2. Width-based JS filter — picks the only pill-sized (>50px)
             button in the prompt-bar row at y~870 that has visible
             text. Locale-agnostic.
          3. ``get_by_role`` with a regex over a few known localizations
             of "agent" — last resort, only relevant if Flow ever drops
             the styled-components class entirely.
        """
        # 1. Leaf class with force=True (skip actionability checks — Flow
        # re-renders this button on focus and Playwright's "stable" retry
        # loop kept losing it with "element was detached from the DOM").
        try:
            loc = self.page.locator("button.sc-59223abb-3").first
            if await loc.count() > 0:
                await loc.click(timeout=5000, force=True)
                logger.info("[Flow] agent mode toggle clicked via button.sc-59223abb-3")
                await asyncio.sleep(0.4)
                return True
        except Exception as e:
            logger.debug("[Flow] agent toggle via leaf class failed: %s", e)

        # 1b. JS-click bypass — if Playwright still can't pin the element
        # because of re-renders, dispatch a synthetic click directly. We
        # lose Playwright's input-event fidelity but the toggle only cares
        # about the click event.
        try:
            clicked = await self.page.evaluate(
                """() => {
                    const btn = document.querySelector('button.sc-59223abb-3');
                    if (!btn) return false;
                    btn.click();
                    return true;
                }"""
            )
            if clicked:
                logger.info("[Flow] agent mode toggle clicked via JS .click()")
                await asyncio.sleep(0.4)
                return True
        except Exception as e:
            logger.debug("[Flow] agent toggle via JS click failed: %s", e)

        # 2. Width/position filter — the agent toggle is the only pill
        # (w>50px) button in the prompt-bar row, distinct from the 32x32
        # icon buttons around it. We resolve to a stable DOM handle in
        # JS, then click via Playwright for auto-wait/stability.
        try:
            handle = await self.page.evaluate_handle(
                """() => {
                    const viewH = window.innerHeight;
                    for (const b of document.querySelectorAll('button')) {
                        const r = b.getBoundingClientRect();
                        // Prompt bar row (bottom ~12% of viewport),
                        // pill width 50-200px, not full-width.
                        if (r.y < viewH * 0.85) continue;
                        if (r.width < 50 || r.width > 200) continue;
                        if (r.height < 20 || r.height > 60) continue;
                        // Must have actual visible text — pure icon
                        // buttons render as "icon_nameLabel" (no space),
                        // pills render as just "Agent" or localized
                        // equivalent. Reject anything that looks like
                        // a Material Symbols ligature.
                        const text = (b.textContent || '').trim();
                        if (!text || /^[a-z_]+[A-Z]/.test(text)) continue;
                        return b;
                    }
                    return null;
                }"""
            )
            element = handle.as_element()
            if element is not None:
                await element.click(timeout=5000)
                logger.info("[Flow] agent mode toggle clicked via width/position filter")
                await asyncio.sleep(0.4)
                return True
        except Exception as e:
            logger.debug("[Flow] agent toggle via width filter failed: %s", e)

        # 3. Role-based localization fallback
        try:
            loc = self.page.get_by_role(
                "button",
                name=re.compile(
                    # English / Vietnamese / Russian / Spanish / Portuguese
                    # / French / German / Italian / Indonesian
                    r"^(Agent|Tác nhân|Агент|Agente|Représentant|"
                    r"Agente|Agente|Agent)$",
                    re.IGNORECASE,
                ),
            ).first
            if await loc.count() > 0:
                await loc.click(timeout=5000)
                logger.info("[Flow] agent mode toggle clicked via role+regex")
                await asyncio.sleep(0.4)
                return True
        except Exception as e:
            logger.debug("[Flow] agent toggle via role regex failed: %s", e)

        return False

    async def _dump_agent_diag(
        self, *, missing_panel: bool, missing_toggle: bool
    ) -> None:
        """Log every visible button on the page so we can craft selectors
        for the agent panel close + agent-mode toggle from real DOM data.

        Triggered only when the configured selectors miss — this is meant
        as a one-time diagnostic that the operator reads after a failed
        generation, not as runtime telemetry.
        """
        try:
            buttons = await self.page.evaluate(
                """() => {
                    const out = [];
                    for (const b of document.querySelectorAll('button')) {
                        const r = b.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        out.push({
                            x: Math.round(r.x),
                            y: Math.round(r.y),
                            w: Math.round(r.width),
                            h: Math.round(r.height),
                            text: (b.textContent || '').trim().slice(0, 40),
                            aria: b.getAttribute('aria-label'),
                            title: b.getAttribute('title'),
                            cls: (b.className || '').toString(),
                        });
                    }
                    return {
                        viewport: {w: window.innerWidth, h: window.innerHeight},
                        buttons: out,
                    };
                }"""
            )
        except Exception as e:
            logger.warning("[Flow] agent diag evaluate failed: %s", e)
            return

        vp = buttons.get("viewport", {})
        bs = buttons.get("buttons", [])
        logger.info(
            "[Flow] agent diag: missing_panel=%s missing_toggle=%s "
            "viewport=%sx%s buttons=%d",
            missing_panel, missing_toggle,
            vp.get("w"), vp.get("h"), len(bs),
        )
        # Dump every button — quantity is bounded (Flow rarely renders >30)
        # and the operator needs to see the full set to pick the right one.
        for b in bs:
            logger.info(
                "[Flow] btn x=%s y=%s w=%s h=%s text=%r aria=%r title=%r cls=%r",
                b["x"], b["y"], b["w"], b["h"],
                b["text"], b["aria"], b["title"], b["cls"],
            )

    async def _create_project_via_api(self) -> str | None:
        """Create a new Flow project by POSTing to the tRPC endpoint.

        Used as a fallback when the "+ New project" DOM button can't be
        located (Flow's landing page markup drifts regularly). Runs
        inside the page context so the browser's authenticated cookies
        (__Secure-next-auth.session-token) are attached automatically —
        the request is indistinguishable from what Flow's own JS does
        when you click the button.

        Returns the new ``projectId`` on success, ``None`` on failure.
        """
        # JS runs inside the page, gets same-origin cookies for free.
        # Path to projectId mirrors src/services/veo_client.py::create_project.
        try:
            result = await self.page.evaluate(
                """async () => {
                    try {
                        const resp = await fetch(
                            '/fx/api/trpc/project.createProject',
                            {
                                method: 'POST',
                                credentials: 'include',
                                headers: {'content-type': 'application/json'},
                                body: JSON.stringify({
                                    json: {
                                        projectTitle: 'Veo Project',
                                        toolName: 'PINHOLE',
                                    },
                                }),
                            },
                        );
                        const text = await resp.text();
                        return {status: resp.status, body: text};
                    } catch (e) {
                        return {status: 0, body: String(e)};
                    }
                }"""
            )
        except Exception as e:
            logger.error("[Flow] createProject fetch threw: %s", e)
            return None

        if not result or result.get("status") != 200:
            logger.error(
                "[Flow] createProject returned status=%s body=%s",
                result.get("status") if result else "?",
                (result.get("body") or "")[:400] if result else "",
            )
            return None

        import json as _json
        try:
            data = _json.loads(result["body"])
        except Exception as e:
            logger.error("[Flow] createProject body not JSON: %s (body=%r)", e, result["body"][:200])
            return None

        try:
            pid = (
                data.get("result", {})
                    .get("data", {})
                    .get("json", {})
                    .get("result", {})
                    .get("projectId")
            )
        except Exception:
            pid = None

        if not pid:
            logger.error(
                "[Flow] createProject: no projectId in response — keys=%s",
                list((data or {}).keys()),
            )
            return None
        return pid

    async def _open_config_panel(self) -> bool:
        """Click the main config button in the prompt bar to open the panel.

        The config button is the wide ``aria-haspopup="menu"`` button in
        the bottom prompt bar whose textContent contains the current
        model name (``Banana`` / ``Imagen`` / ``Veo``) and a ``crop_``
        Material Symbol icon for the aspect ratio.

        Find by ``textContent`` via a JS handle so locale + aria-label
        weirdness doesn't break us. Click via ``ElementHandle.click(force=True)``
        so Playwright's actionability retries don't drop the node during
        the panel's open animation. Verify by counting ``[role="tab"]``
        descendants after the click.

        When the loaded model is **Nano Banana** (Flow's default for new
        projects on the May-27 build) the panel opens on the Image tab —
        ``_setup_video_config`` will then click Video, but it relies on
        the tab actually being live. So if we detect Banana in the
        opened panel header we click Video here too as a safety net.
        """
        # --- 1. Locate config button via textContent (locale-safe) ---
        try:
            handle = await self.page.evaluate_handle(
                """() => {
                    const viewH = window.innerHeight;
                    for (const b of document.querySelectorAll('button[aria-haspopup="menu"]')) {
                        const r = b.getBoundingClientRect();
                        if (r.y < viewH * 0.7) continue;
                        if (r.width < 30 || r.height < 20) continue;
                        const text = (b.textContent || '').trim();
                        if (/crop_|Veo|Imagen|Image|Video|Banana/.test(text)) {
                            return b;
                        }
                    }
                    return null;
                }"""
            )
        except Exception as e:
            logger.warning("[Flow] Config button lookup failed: %s", e)
            return False

        elem = handle.as_element()
        if elem is None:
            logger.warning("[Flow] Could not find config panel button")
            return False

        # Capture the button's textContent BEFORE click so we know what
        # model the project loaded with — needed for the Banana branch.
        try:
            btn_text = (await elem.text_content() or "").strip()
        except Exception:
            btn_text = ""

        # --- 2. Click with force, give panel time to render ---
        try:
            await elem.click(timeout=5000, force=True)
        except Exception as e:
            logger.warning("[Flow] Config button click failed: %s", e)
            return False

        # Panel animates open; verify by tab count with a short retry loop.
        tab_count = 0
        for _ in range(10):
            await asyncio.sleep(0.3)
            try:
                tab_count = await self.page.evaluate(
                    """() => document.querySelectorAll('[role="tab"]').length"""
                )
            except Exception:
                tab_count = 0
            if tab_count >= 4:
                break

        if tab_count < 4:
            logger.warning("[Flow] Config panel may not have opened (tabs=%d)", tab_count)
            return False

        # --- 3. Nano Banana default: pre-switch to Video tab ---
        # Flow's new-project default is Nano Banana 2, which opens the
        # panel on the Image tab. _setup_video_config will request Video
        # next, but doing the switch right here makes the subsequent
        # _select_count / _select_video_model resolve against the Video
        # tab content rather than racing the tab repaint.
        if "Banana" in btn_text:
            try:
                video_tab = self.page.get_by_role(
                    "tab",
                    name=re.compile(r"(play_circle|Video)", re.IGNORECASE),
                )
                if await video_tab.count() > 0:
                    await video_tab.first.click(timeout=3000, force=True)
                    await asyncio.sleep(0.6)
                    logger.info(
                        "[Flow] Switched to Video tab from Nano Banana default"
                    )
            except Exception as e:
                logger.debug("[Flow] Video tab pre-switch failed: %s", e)

        return True

    async def _click_tab(self, target_text: str) -> str | None:
        """Find a visible [role="tab"] matching target_text and click it.

        Primary: Playwright ``get_by_role("tab")`` locator.
        Fallback: JS evaluate + mouse.click at coordinates.

        Returns 'already' if already selected, 'clicked' if newly clicked, None if not found.
        """
        # ── Primary: Playwright locator ──
        try:
            tab = self.page.get_by_role("tab", name=re.compile(re.escape(target_text)))
            if await tab.count() > 0:
                first = tab.first
                is_selected = await first.get_attribute("aria-selected")
                if is_selected == "true":
                    return 'already'
                await first.click()
                await asyncio.sleep(0.5)
                return 'clicked'
        except Exception as e:
            logger.debug("[Flow] _click_tab locator failed for '%s': %s", target_text, e)

        # ── Fallback: JS evaluate + mouse.click ──
        tab_info = await self.page.evaluate("""(target) => {
            for (const tab of document.querySelectorAll('[role="tab"]')) {
                const text = (tab.textContent || '').trim();
                if (text.includes(target)) {
                    const rect = tab.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        return {
                            selected: tab.getAttribute('aria-selected') === 'true',
                            x: rect.x + rect.width / 2,
                            y: rect.y + rect.height / 2,
                        };
                    }
                }
            }
            return null;
        }""", target_text)

        if not tab_info:
            return None
        if tab_info['selected']:
            return 'already'
        await self.page.mouse.click(tab_info['x'], tab_info['y'])
        await asyncio.sleep(0.5)
        return 'clicked'

    async def _fill_prompt(self, prompt: str) -> bool:
        """Fill the prompt contenteditable div.

        Strategy 1 (primary): native ``keyboard.type()`` — fires real
        keydown/keypress/input/keyup events that Flow's Lit framework
        actually listens for. ``execCommand('insertText')`` inserts text
        visually but Lit never picks it up → "You must provide a prompt".

        Strategy 2 (fallback): ``execCommand`` + synthetic keyboard nudge
        to force the framework to rescan the DOM.

        After filling, blurs and refocuses the element to trigger any
        ``change``/``focusout`` handlers the framework may rely on.

        Returns True if the prompt was filled successfully.
        """
        logger.info("[Flow] _fill_prompt: url=%s prompt=%r", self.page.url, prompt[:60])

        try:
            editable = self.page.locator('[contenteditable="true"]').first
            ce_count = await editable.count()
            logger.info("[Flow] _fill_prompt: contenteditable count=%d", ce_count)
            if ce_count > 0:
                await editable.click()
                await asyncio.sleep(0.2)

                # ── Strategy 1: keyboard.type (native events Lit listens for) ──
                ok = await self._fill_prompt_keyboard(editable, prompt)
                if ok:
                    return True

                # ── Strategy 2: execCommand + keyboard nudge ──
                ok = await self._fill_prompt_exec_command(editable, prompt)
                if ok:
                    return True

                logger.warning(
                    "[Flow] Both contenteditable strategies failed for prompt"
                )
        except Exception as e:
            logger.warning("[Flow] Contenteditable fill failed: %s", e)

        # Fallback: try textarea
        try:
            textareas = self.page.locator('textarea')
            ta_count = await textareas.count()
            logger.info("[Flow] _fill_prompt fallback: textarea count=%d", ta_count)
            for i in range(ta_count):
                ta = textareas.nth(i)
                box = await ta.bounding_box()
                if box and box['width'] > 100 and box['height'] > 10:
                    try:
                        await ta.fill(prompt)
                        logger.info("[Flow] Filled prompt via textarea .fill()")
                        return True
                    except Exception:
                        await ta.click()
                        await self.page.keyboard.press("Control+a")
                        await self.page.keyboard.press("Delete")
                        await self.page.keyboard.type(prompt, delay=10)
                        logger.info("[Flow] Filled prompt via textarea typing")
                        return True
        except Exception as e:
            logger.warning("[Flow] Textarea typing failed: %s", e)

        logger.warning(
            "[Flow] Could not find any prompt input element (url=%s)", self.page.url
        )
        await self._dump_failure_state("fill_prompt_failed")
        return False

    def _verify_prompt_text(self, got_text: str, wanted_prompt: str) -> bool:
        """Check that *got_text* contains the beginning of *wanted_prompt*.

        Normalises newlines because contenteditable renders ``\\n`` as
        ``<div>`` / ``<br>`` elements whose boundaries ``text_content()``
        concatenates **without** any whitespace or newline character.
        A probe that contains a literal ``\\n`` would never match.
        """
        norm_got = re.sub(r'\s+', ' ', (got_text or "")).strip()
        norm_probe = re.sub(r'\s+', ' ', wanted_prompt.strip()[:40])
        return bool(norm_probe and norm_probe in norm_got)

    async def _fill_prompt_keyboard(self, editable, prompt: str) -> bool:
        """Primary: native keyboard events that Lit actually tracks.

        Uses ``Ctrl+A`` **immediately** before inserting text so the first
        insertion replaces the selection — no time-window for the framework
        to restore a draft from a previous session via websocket sync.

        Attempt A — ``keyboard.insert_text`` (instant, fires a single
        ``input`` event).
        Attempt B — ``keyboard.type`` char-by-char with adaptive delay
        (fires full keydown/keypress/input/keyup per character).

        After insertion, blurs and refocuses to trigger any change/focusout
        handlers the framework may rely on.
        """
        try:
            # ── Attempt A: insert_text (instant, single input event) ──
            await editable.click()
            await asyncio.sleep(0.15)
            await self.page.keyboard.press("Control+a")
            await asyncio.sleep(0.1)

            await self.page.keyboard.insert_text(prompt)
            await asyncio.sleep(0.3)

            # Blur → refocus cycle
            await self.page.evaluate("document.activeElement?.blur()")
            await asyncio.sleep(0.2)
            await editable.click()
            await asyncio.sleep(0.3)

            text = (await editable.text_content()) or ""
            if self._verify_prompt_text(text, prompt):
                logger.info(
                    "[Flow] Filled prompt via keyboard.insert_text (len=%d)",
                    len(text),
                )
                return True
            logger.debug(
                "[Flow] insert_text verify failed (got=%r), trying keyboard.type",
                text[:80],
            )

            # ── Attempt B: keyboard.type (per-character, slower) ──
            await editable.click()
            await asyncio.sleep(0.1)
            await self.page.keyboard.press("Control+a")
            await asyncio.sleep(0.1)

            # Adaptive delay: cap total typing time at ~8 s
            delay = max(3, min(18, int(8000 / max(len(prompt), 1))))
            await self.page.keyboard.type(prompt, delay=delay)
            await asyncio.sleep(0.3)

            await self.page.evaluate("document.activeElement?.blur()")
            await asyncio.sleep(0.2)
            await editable.click()
            await asyncio.sleep(0.3)

            text = (await editable.text_content()) or ""
            if self._verify_prompt_text(text, prompt):
                logger.info(
                    "[Flow] Filled prompt via keyboard.type (len=%d)", len(text),
                )
                return True
            logger.warning(
                "[Flow] keyboard strategy: verify failed (got=%r, wanted=%r)",
                text[:80], prompt.strip()[:40],
            )
            return False
        except Exception as e:
            logger.debug("[Flow] keyboard strategy failed: %s", e)
            return False

    async def _fill_prompt_exec_command(self, editable, prompt: str) -> bool:
        """Fallback: ``execCommand('insertText')`` + keyboard nudge.

        execCommand alone inserts text but Lit may not see it. After
        inserting, we type and delete a space to trigger a real keyboard
        event cycle that prods the framework into re-reading the DOM.
        """
        try:
            # Select-all + delete to clear any pre-existing draft text
            await editable.evaluate("""el => {
                el.focus();
                const sel = window.getSelection();
                sel.selectAllChildren(el);
                document.execCommand('delete', false);
            }""")
            await asyncio.sleep(0.1)

            # Insert the new text via execCommand
            inserted = await editable.evaluate("""(el, text) => {
                el.focus();
                return document.execCommand('insertText', false, text);
            }""", prompt)

            if not inserted:
                logger.debug("[Flow] execCommand('insertText') returned false")
                return False

            await asyncio.sleep(0.2)

            # Nudge: type a space then backspace it — the real keydown/input
            # events wake Lit up and make it re-read the contenteditable
            await self.page.keyboard.press("End")
            await self.page.keyboard.type(" ", delay=30)
            await asyncio.sleep(0.1)
            await self.page.keyboard.press("Backspace")
            await asyncio.sleep(0.2)

            # Blur → refocus to trigger change/focusout handlers
            await self.page.evaluate("document.activeElement?.blur()")
            await asyncio.sleep(0.2)
            await editable.click()
            await asyncio.sleep(0.3)

            text = (await editable.text_content()) or ""
            if self._verify_prompt_text(text, prompt):
                logger.info(
                    "[Flow] Filled prompt via execCommand+nudge (len=%d)",
                    len(text),
                )
                return True
            logger.warning(
                "[Flow] execCommand+nudge verify failed "
                "(got=%r, wanted prefix=%r)", text[:80], prompt.strip()[:40],
            )
            return False
        except Exception as e:
            logger.debug("[Flow] execCommand strategy failed: %s", e)
            return False

    async def _dump_failure_state(self, tag: str) -> None:
        """Save a screenshot + HTML + URL of the current page to output_dir
        so we can see what Flow actually rendered when something failed.
        Never raises — pure best-effort diagnostics.
        """
        try:
            out = getattr(self, "output_dir", None)
            if out is None:
                return
            from pathlib import Path as _P
            base = _P(out) / "_failures"
            base.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            png = base / f"{stamp}_{tag}.png"
            html_path = base / f"{stamp}_{tag}.html"
            url_path = base / f"{stamp}_{tag}.url.txt"
            try:
                await self.page.screenshot(path=str(png), full_page=True)
            except Exception as e:
                logger.debug("[Flow] failure screenshot failed: %s", e)
            try:
                html = await self.page.content()
                html_path.write_text(html, encoding="utf-8")
            except Exception as e:
                logger.debug("[Flow] failure html dump failed: %s", e)
            try:
                url_path.write_text(self.page.url or "", encoding="utf-8")
            except Exception:
                pass
            logger.error(
                "[Flow] FAILURE STATE DUMPED → screenshot=%s html=%s url=%s",
                png, html_path, self.page.url,
            )
        except Exception as e:
            logger.debug("[Flow] _dump_failure_state error: %s", e)

    async def _click_submit(self) -> bool:
        """Find and click the submit (arrow_forward) button in the prompt bar.

        Primary: Playwright locator matching ``arrow_forward`` in button name.
        Fallback: JS evaluate + mouse.click at coordinates.
        Polls for up to 5 seconds for the button to become available.

        Returns True if the button was found and clicked.
        """
        # ── Primary: Playwright locator ──
        try:
            btn = self.page.get_by_role("button", name=re.compile(r"arrow_forward"))
            for _ in range(10):
                if await btn.count() > 0:
                    await btn.first.click()
                    logger.info("[Flow] Clicked submit (arrow_forward) via locator")
                    return True
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.debug("[Flow] Submit locator failed, trying JS: %s", e)

        # ── Fallback: JS evaluate + mouse.click ──
        for _ in range(10):
            btn_pos = await self.page.evaluate('''() => {
                const viewH = window.innerHeight;
                for (const btn of document.querySelectorAll('button')) {
                    const text = (btn.textContent || '').trim();
                    const rect = btn.getBoundingClientRect();
                    if (text.includes('arrow_forward')
                        && rect.top > viewH * 0.7
                        && rect.width > 0 && !btn.disabled) {
                        return {
                            x: Math.round(rect.x + rect.width / 2),
                            y: Math.round(rect.y + rect.height / 2),
                        };
                    }
                }
                return null;
            }''')
            if btn_pos:
                await self.page.mouse.click(btn_pos['x'], btn_pos['y'])
                logger.info("[Flow] Clicked submit (arrow_forward) button")
                return True
            await asyncio.sleep(0.5)

        logger.warning("[Flow] Could not find or click submit button")
        return False

    def _make_prompt_dir(self, prompt: str) -> Path:
        """Create an output subdirectory named after the prompt and timestamp."""
        safe = re.sub(r'[^\w\s-]', '', prompt)
        safe = re.sub(r'\s+', '_', safe.strip())
        safe = safe[:60] if len(safe) > 60 else safe
        ts = time.strftime('%Y%m%d_%H%M%S')
        dirname = f"{safe}_{ts}" if safe else ts
        return self.output_dir / dirname

    async def download_file(
        self,
        url: str,
        output_dir: Path | None = None,
        extension: str = ".png",
        timeout: float = 60.0,
    ) -> tuple[Path | None, str | None]:
        """Download a file from a URL and save it locally.

        Returns (local_path, filename) or (None, None) on failure.
        """
        dest = output_dir or self.output_dir
        dest.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}{extension}"
        local_path = dest / filename

        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True,
            ) as http:
                resp = await http.get(url)
                resp.raise_for_status()
                local_path.write_bytes(resp.content)
                logger.info(
                    "[Flow] Downloaded file to %s (%d bytes)",
                    local_path, len(resp.content),
                )
                return local_path, filename
        except Exception as e:
            logger.error("[Flow] Failed to download file: %s", e)
            return None, None

    async def _download_with_cookies(
        self,
        url: str,
        output_dir: Path | None = None,
        extension: str = ".mp4",
        timeout: float = 120.0,
    ) -> tuple[Path | None, str | None]:
        """Download a file using the browser context's session cookies.

        Used for authenticated URLs like labs.google/fx/api/trpc/media
        redirect URLs that require login cookies to access.

        Returns (local_path, filename) or (None, None) on failure.
        """
        dest = output_dir or self.output_dir
        dest.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}{extension}"
        local_path = dest / filename

        try:
            # Extract cookies from browser context
            browser_cookies = await self.context.cookies()
            cookies = httpx.Cookies()
            for c in browser_cookies:
                cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, cookies=cookies,
            ) as http:
                async with http.stream("GET", url) as resp:
                    resp.raise_for_status()
                    size = 0
                    with local_path.open("wb") as f:
                        async for chunk in resp.aiter_bytes(65536):
                            f.write(chunk)
                            size += len(chunk)
                logger.info(
                    "[Flow] Downloaded file (authenticated) to %s (%d bytes)",
                    local_path, size,
                )
                return local_path, filename
        except Exception as e:
            logger.error("[Flow] Failed authenticated download: %s", e)
            return None, None

    @staticmethod
    def _read_image_dimensions(path: Path) -> tuple[int | None, int | None]:
        """Read actual pixel dimensions from an image file header.

        Supports WebP, PNG, and JPEG formats.
        """
        try:
            data = path.read_bytes()
            # WebP
            if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
                if data[12:16] == b'VP8 ':
                    w = int.from_bytes(data[26:28], 'little') & 0x3FFF
                    h = int.from_bytes(data[28:30], 'little') & 0x3FFF
                    return w, h
                elif data[12:16] == b'VP8L':
                    bits = int.from_bytes(data[21:25], 'little')
                    w = (bits & 0x3FFF) + 1
                    h = ((bits >> 14) & 0x3FFF) + 1
                    return w, h
                elif data[12:16] == b'VP8X':
                    w = int.from_bytes(data[24:27], 'little') + 1
                    h = int.from_bytes(data[27:30], 'little') + 1
                    return w, h
            # PNG
            if data[:8] == b'\x89PNG\r\n\x1a\n':
                w = int.from_bytes(data[16:20], 'big')
                h = int.from_bytes(data[20:24], 'big')
                return w, h
            # JPEG
            if data[:2] == b'\xff\xd8':
                i = 2
                while i < len(data) - 1:
                    if data[i] != 0xFF:
                        break
                    marker = data[i + 1]
                    if marker in (0xC0, 0xC1, 0xC2):
                        h = int.from_bytes(data[i + 5:i + 7], 'big')
                        w = int.from_bytes(data[i + 7:i + 9], 'big')
                        return w, h
                    length = int.from_bytes(data[i + 2:i + 4], 'big')
                    i += 2 + length
        except Exception:
            pass
        return None, None

    async def _detect_error_card(self) -> Optional[str]:
        """Check DOM for a generation error card (warning icon in content area).

        Flow displays failures as cards with a ``warning`` Material Icon,
        a bold header and a description.  The check is locale-independent:
        we look for a *leaf* text-node whose content is exactly ``warning``
        (the Material Icon ligature) rendered at icon size inside the main
        content area — not in the toolbar or footer.

        **Visibility gate**: Flow pre-renders error-state templates inside
        video cards that are hidden (``display:none``, ``visibility:hidden``,
        or ``opacity:0``).  We use ``checkVisibility()`` (with opacity +
        visibility CSS checks) to skip those hidden elements and avoid
        false positives on normal in-progress generations.

        Returns the card's text content (up to 500 chars) or ``None``.
        """
        try:
            return await self.page.evaluate("""() => {
                const viewH = window.innerHeight;
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT,
                );
                while (walker.nextNode()) {
                    const raw = walker.currentNode.textContent.trim().toLowerCase();
                    if (raw !== 'warning' && raw !== 'error'
                        && raw !== 'report_problem') continue;
                    const el = walker.currentNode.parentElement;
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    // Icon-sized element in the content area
                    if (r.width <= 0 || r.width > 60 || r.height > 60) continue;
                    if (r.top < 70 || r.top > viewH * 0.65) continue;

                    // ── Visibility gate ──
                    // Skip hidden template elements (pre-rendered error states
                    // inside video cards that are invisible during normal gen).
                    try {
                        if (typeof el.checkVisibility === 'function'
                            && !el.checkVisibility({
                                   opacityProperty: true,
                                   visibilityProperty: true,
                               })) continue;
                    } catch(_) {}
                    // Fallback: manual style check on element + ancestors
                    let hidden = false;
                    let anc = el;
                    for (let j = 0; j < 8 && anc && anc !== document.body; j++) {
                        const s = window.getComputedStyle(anc);
                        if (s.display === 'none'
                            || s.visibility === 'hidden'
                            || parseFloat(s.opacity) < 0.1) {
                            hidden = true; break;
                        }
                        anc = anc.parentElement;
                    }
                    if (hidden) continue;

                    // Walk up to find the card container with substantial text
                    let parent = el.parentElement;
                    for (let i = 0; i < 12 && parent; i++) {
                        const ct = (parent.textContent || '').trim();
                        if (ct.length > 40 && ct.length < 3000) {
                            return ct.substring(0, 500);
                        }
                        parent = parent.parentElement;
                    }
                    return 'error_card_detected';
                }
                return null;
            }""")
        except Exception:
            return None

    async def _click_error_retry(self) -> bool:
        """Click the Retry button on an error card.

        The error card has three action buttons: ``refresh Retry`` /
        ``undo Regenerate`` / ``delete_forever Delete`` (Material Symbol
        ligature + localized label). We want the first one — Retry —
        anchored on the locale-invariant ``refresh`` token in the
        accessible name. Fallback is the operator-supplied wrapper CSS
        ``div.sc-101009f9-5 > button:first-child``.
        """
        # 1. role-based, locale-agnostic
        try:
            loc = self.page.get_by_role(
                "button",
                name=re.compile(r"^refresh\b", re.IGNORECASE),
            ).first
            if await loc.count() > 0:
                await loc.click(timeout=5000)
                logger.info(
                    "[FlowVideoClient] Retry clicked (role=button, name=~/^refresh/)"
                )
                return True
        except Exception as e:
            logger.debug("[Flow] retry click via role failed: %s", e)

        # 2. CSS wrapper fallback
        css_fallbacks = [
            "div.sc-101009f9-5 button:first-child",
            # If the leaf wrapper rehashes, target by any visible button
            # whose textContent starts with the 'refresh' icon ligature.
            "button:has-text('refresh')",
        ]
        for sel in css_fallbacks:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() == 0:
                    continue
                await loc.click(timeout=5000)
                logger.info("[FlowVideoClient] Retry clicked via %s", sel)
                return True
            except Exception as e:
                logger.debug("[Flow] retry via %s failed: %s", sel, e)
        return False

    async def close(self) -> None:
        """Clean up resources. Page and context managed by BrowserManager."""
        pass


class FlowClient(FlowBaseClient, AbstractImageClient):
    """Google Flow image generation client via browser automation.

    Automates the Google Flow web UI to generate images from text prompts.
    """

    def __init__(
        self,
        page: Page,
        context: BrowserContext,
        output_dir: Path | None = None,
        model: FlowModel = FlowModel.NANO_BANANA_2,
        orientation: FlowOrientation = FlowOrientation.LANDSCAPE,
        count: FlowCount = FlowCount.X2,
    ):
        FlowBaseClient.__init__(self, page, context, output_dir)
        AbstractImageClient.__init__(self, page, context)
        self.model = model
        self.orientation = orientation
        self.count = count
        self._config_applied = False

    async def _setup_generation_config(self) -> None:
        """Configure model, orientation, and count via the config panel (one-time).

        The config panel is opened by clicking the main config button in the
        prompt bar (e.g. "Nano Banana 2 crop_16_9 x2"). Inside the panel:
        - Row 1: Image / Video tabs
        - Row 2: Landscape / Portrait tabs
        - Row 3: x1 / x2 / x3 / x4 tabs
        - Row 4: Model dropdown (e.g. "Nano Banana 2")
        """
        if self._config_applied:
            return

        panel_opened = await self._open_config_panel()
        if not panel_opened:
            logger.warning("[FlowClient] Config panel failed to open, will retry next call")
            return

        # Ensure Image tab is selected
        result = await self._click_tab("Image")
        if result == 'clicked':
            logger.info("[FlowClient] Switched to Image tab")
        await self._select_orientation()
        await self._select_count()
        await self._select_model()
        # Close the config panel by clicking outside it
        await self.page.mouse.click(10, 10)
        await asyncio.sleep(0.5)

        # Verify by reading the config button text
        btn_text = await self.page.evaluate("""() => {
            const viewH = window.innerHeight;
            for (const btn of document.querySelectorAll('button[aria-haspopup="menu"]')) {
                const rect = btn.getBoundingClientRect();
                if (rect.y > viewH * 0.7 && rect.width > 30) {
                    return (btn.textContent || '').trim();
                }
            }
            return null;
        }""")
        logger.info("[FlowClient] Config button now shows: %s", btn_text)

        self._config_applied = True
        logger.info(
            "[FlowClient] Config applied: model=%s, orientation=%s, count=%s",
            self.model.name, self.orientation.name, self.count.name,
        )

    async def _select_orientation(self) -> bool:
        """Select Landscape or Portrait tab in the open config panel."""
        target = ORIENTATION_UI_LABELS[self.orientation]
        result = await self._click_tab(target)

        if result == 'already':
            logger.info("[FlowClient] Orientation '%s' already selected", target)
        elif result == 'clicked':
            logger.info("[FlowClient] Selected orientation: %s", target)
        else:
            logger.warning("[FlowClient] Could not find orientation tab '%s'", target)
            return False
        return True

    async def _select_count(self) -> bool:
        """Select x1/x2/x3/x4 tab in the open config panel."""
        target = COUNT_UI_LABELS[self.count]
        # Count tabs use exact match (x1, x2, x3, x4) — find by exact text
        tab_info = await self.page.evaluate("""(target) => {
            for (const tab of document.querySelectorAll('[role="tab"]')) {
                const text = (tab.textContent || '').trim();
                if (text === target) {
                    const rect = tab.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        return {
                            selected: tab.getAttribute('aria-selected') === 'true',
                            x: rect.x + rect.width / 2,
                            y: rect.y + rect.height / 2,
                        };
                    }
                }
            }
            return null;
        }""", target)

        if not tab_info:
            logger.warning("[FlowClient] Could not find count tab '%s'", target)
            return False
        if tab_info['selected']:
            logger.info("[FlowClient] Count '%s' already selected", target)
        else:
            await self.page.mouse.click(tab_info['x'], tab_info['y'])
            await asyncio.sleep(0.5)
            logger.info("[FlowClient] Selected count: %s", target)
        return True

    async def _select_model(self) -> bool:
        """Select image model from the dropdown inside the open config panel.

        The model dropdown is the button with 'arrow_drop_down' in the panel.
        """
        target_label = MODEL_UI_LABELS[self.model]

        # Find the model dropdown button inside the panel (has arrow_drop_down text)
        model_btn = await self.page.evaluate("""() => {
            for (const btn of document.querySelectorAll('button')) {
                const text = (btn.textContent || '').trim();
                const rect = btn.getBoundingClientRect();
                if (text.includes('arrow_drop_down') && rect.width > 100
                    && rect.y > 700 && rect.y < 900) {
                    return {
                        text: text,
                        x: rect.x + rect.width / 2,
                        y: rect.y + rect.height / 2,
                    };
                }
            }
            return null;
        }""")

        if not model_btn:
            logger.warning("[FlowClient] Could not find model dropdown button")
            return False

        # Check if already the right model
        if target_label in model_btn['text']:
            logger.info("[FlowClient] Model '%s' already selected", target_label)
            return True

        # Open the model dropdown
        await self.page.mouse.click(model_btn['x'], model_btn['y'])
        await asyncio.sleep(0.5)

        # Click the target model from the dropdown menu
        clicked = await self.page.evaluate("""(target) => {
            for (const item of document.querySelectorAll('[role="menuitem"]')) {
                const text = (item.textContent || '').trim();
                if (text.includes(target)) {
                    item.click();
                    return text;
                }
            }
            return null;
        }""", target_label)

        if clicked:
            logger.info("[FlowClient] Selected model: %s", clicked)
            await asyncio.sleep(0.3)
        else:
            logger.warning("[FlowClient] Model '%s' not found in dropdown", target_label)
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.2)

        return clicked is not None

    def _setup_response_listener(self) -> tuple[list[str], object]:
        """Set up a network response listener to capture signed GCS image URLs.

        Captures URLs from two sources:
        1. The batchGenerateImages API response (JSON containing signed URLs)
        2. Individual storage.googleapis.com image responses

        Returns (captured_urls_list, listener_fn) — caller must remove listener.
        """
        captured: list[str] = []

        async def _extract_batch_urls(response):
            """Parse the batchGenerateImages API response for signed URLs."""
            try:
                body = await response.json()
                # Walk the response looking for signed GCS URLs
                def find_urls(obj):
                    if isinstance(obj, str):
                        if 'storage.googleapis.com' in obj and 'image' in obj:
                            captured.append(obj)
                    elif isinstance(obj, dict):
                        for v in obj.values():
                            find_urls(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            find_urls(item)
                find_urls(body)
                logger.info(
                    "[FlowClient] Extracted %d GCS URLs from batch API response",
                    len(captured),
                )
            except Exception as e:
                logger.warning("[FlowClient] Failed to parse batch response: %s", e)

        def on_response(response):
            url = response.url
            # Capture individual GCS image responses
            if ('storage.googleapis.com' in url
                    and response.status == 200
                    and 'image' in response.headers.get('content-type', '')):
                if url not in captured:
                    captured.append(url)
                    logger.info("[FlowClient] Captured GCS image URL: %s", url[:120])
            # Capture batch generation API response
            elif ('batchGenerateImages' in url and response.status == 200):
                asyncio.ensure_future(_extract_batch_urls(response))

        self.page.on('response', on_response)
        return captured, on_response

    async def _collect_existing_images(self) -> set[str]:
        """Snapshot all image URLs currently in the DOM."""
        urls = await self.page.evaluate(_COLLECT_IMAGES_JS)
        return set(urls)

    async def _wait_for_new_images(
        self,
        existing: set[str],
        expected_count: int = 4,
        timeout_seconds: int = 120,
    ) -> list[str]:
        """Wait for new images to appear. Returns as many as found within timeout."""
        last_count = 0
        stable_ticks = 0

        for i in range(timeout_seconds):
            await asyncio.sleep(1)
            current_urls = await self.page.evaluate(_COLLECT_IMAGES_JS)
            new_urls = [u for u in current_urls if u not in existing]

            if len(new_urls) >= expected_count:
                return new_urls[:expected_count]

            if len(new_urls) == last_count:
                stable_ticks += 1
            else:
                stable_ticks = 0
                last_count = len(new_urls)

            if new_urls and stable_ticks >= 15:
                logger.info(
                    "[FlowClient] Stable at %d images after %ds, returning",
                    len(new_urls), i,
                )
                return new_urls

            if i > 0 and i % 15 == 0:
                logger.info(
                    "[FlowClient] Waiting for images... %d/%d (%ds/%ds)",
                    len(new_urls), expected_count, i, timeout_seconds,
                )

        current_urls = await self.page.evaluate(_COLLECT_IMAGES_JS)
        return [u for u in current_urls if u not in existing]

    async def download_image(
        self,
        url: str,
        output_dir: Path | None = None,
    ) -> tuple[Path | None, str | None]:
        """Download an image from a signed GCS URL.

        Returns (local_path, filename) or (None, None) on failure.
        """
        return await self.download_file(url, output_dir=output_dir, extension=".png")

    async def _download_via_canvas(
        self,
        img_src: str,
        output_dir: Path | None = None,
    ) -> tuple[Path | None, str | None]:
        """Fallback: extract image data by drawing to canvas in the browser.

        Used when direct download of the URL fails (e.g. redirect URLs).
        Returns (local_path, filename) or (None, None) on failure.
        """
        import base64

        dest = output_dir or self.output_dir
        dest.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}.png"
        local_path = dest / filename

        try:
            data_url = await self.page.evaluate('''(src) => {
                for (const img of document.querySelectorAll('img')) {
                    if (img.src === src && img.naturalWidth > 0) {
                        const canvas = document.createElement('canvas');
                        canvas.width = img.naturalWidth;
                        canvas.height = img.naturalHeight;
                        const ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0);
                        return canvas.toDataURL('image/png');
                    }
                }
                return null;
            }''', img_src)

            if not data_url:
                logger.warning("[FlowClient] Canvas extraction: image not found in DOM")
                return None, None

            # Parse data URL: "data:image/png;base64,..."
            header, b64data = data_url.split(',', 1)
            img_bytes = base64.b64decode(b64data)
            local_path.write_bytes(img_bytes)
            logger.info(
                "[FlowClient] Extracted image via canvas to %s (%d bytes)",
                local_path, len(img_bytes),
            )
            return local_path, filename
        except Exception as e:
            logger.error("[FlowClient] Canvas extraction failed: %s", e)
            return None, None

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(3))
    async def generate_images(
        self,
        prompt: str,
        count: int | None = None,
        timeout_seconds: int = 120,
    ) -> GenerationResult:
        """Generate images from a text prompt.

        Navigates to Flow, enters a project, fills the prompt via keyboard
        typing, clicks the submit arrow, waits for new images to appear
        in the DOM, then downloads them.

        Args:
            prompt: Text description of the image to generate.
            count: Expected number of images. If None, uses self.count
                   (set via FlowCount in constructor).
            timeout_seconds: Maximum wait time for image generation.
        """
        if count is None:
            count = self.count.value
        logger.info("[FlowClient] Generating %d images: %s", count, prompt[:80])
        start_time = time.monotonic()

        await self._ensure_generation_page()
        await self._setup_generation_config()
        existing_images = await self._collect_existing_images()

        filled = await self._fill_prompt(prompt)
        if not filled:
            return GenerationResult(
                platform=Platform.FLOW,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                error="Could not find prompt input",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )
        await asyncio.sleep(0.5)

        # Start listening for signed GCS URLs BEFORE submit so we capture
        # the batchGenerateImages API response containing all signed URLs.
        gcs_urls, listener = self._setup_response_listener()

        clicked = await self._click_submit()
        if not clicked:
            try:
                self.page.remove_listener('response', listener)
            except Exception:
                pass
            return GenerationResult(
                platform=Platform.FLOW,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                error="Could not find or click Create button",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        logger.info("[FlowClient] Prompt submitted, waiting for %d images...", count)

        all_new = await self._wait_for_new_images(
            existing_images,
            expected_count=count,
            timeout_seconds=timeout_seconds,
        )

        # Brief delay to let async batch URL extraction complete
        await asyncio.sleep(2)

        # Stop listening
        try:
            self.page.remove_listener('response', listener)
        except Exception:
            pass

        if not all_new:
            return GenerationResult(
                platform=Platform.FLOW,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                error="No images generated (timeout)",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        # Deduplicate GCS URLs by image UUID (path before query string).
        # The same image can appear with different signed URLs (different
        # Expires/Signature) from batch responses and individual fetches.
        seen_paths: dict[str, str] = {}
        unique_gcs: list[str] = []
        for url in gcs_urls:
            path = url.split('?')[0]
            if path not in seen_paths:
                seen_paths[path] = url
                unique_gcs.append(url)

        logger.info(
            "[FlowClient] Got %d new image URLs, %d unique GCS URLs (from %d captured)",
            len(all_new), len(unique_gcs), len(gcs_urls),
        )

        prompt_dir = self._make_prompt_dir(prompt)

        images: list[GeneratedImage] = []
        for i, dom_url in enumerate(all_new):
            local_path = None
            filename = None

            # Strategy 1: download from captured signed GCS URL
            if i < len(unique_gcs):
                local_path, filename = await self.download_image(
                    unique_gcs[i], output_dir=prompt_dir,
                )

            # Strategy 2: extract via canvas (fallback)
            if local_path is None:
                logger.info("[FlowClient] Falling back to canvas extraction for image %d", i)
                local_path, filename = await self._download_via_canvas(
                    dom_url, output_dir=prompt_dir,
                )

            w, h = None, None
            if local_path:
                w, h = self._read_image_dimensions(local_path)
            images.append(GeneratedImage(
                url=unique_gcs[i] if i < len(unique_gcs) else dom_url,
                local_path=str(local_path) if local_path else None,
                filename=filename,
                width=w,
                height=h,
                is_highres=local_path is not None,
            ))

        duration_ms = int((time.monotonic() - start_time) * 1000)
        logger.info(
            "[FlowClient] Downloaded %d images in %dms",
            len(images), duration_ms,
        )

        return GenerationResult(
            platform=Platform.FLOW,
            prompt=prompt,
            images=images,
            status=GenerationStatus.COMPLETED,
            duration_ms=duration_ms,
            model=self.model.value,
        )


class FlowVideoClient(FlowBaseClient):
    """Google Flow video generation client via browser automation.

    Automates the Google Flow web UI to generate videos from text prompts.
    Flow:
    1. Navigate to the project editor
    2. Switch to Video mode via the config panel
    3. Select video model, orientation, and count
    4. Fill the prompt
    5. Click submit
    6. Wait for the video to appear (intercept network responses for GCS video URLs)
    7. Download the video
    """

    def __init__(
        self,
        page: Page,
        context: BrowserContext,
        output_dir: Path | None = None,
        model: FlowVideoModel = FlowVideoModel.VEO_3_1_FAST,
        orientation: FlowOrientation = FlowOrientation.LANDSCAPE,
        count: FlowCount = FlowCount.X2,
        duration: float | None = None,
    ):
        super().__init__(page, context, output_dir)
        self.model = model
        self.orientation = orientation
        self.count = count
        self.duration = duration
        self._config_applied = False
        self._frames_mode_active = False

    async def _setup_video_config(self) -> None:
        """Configure video mode, model, orientation, and count via the config panel.

        Opens the config panel, clicks the Video tab, then sets orientation,
        count, and video model.
        """
        if self._config_applied:
            return

        panel_opened = await self._open_config_panel()
        if not panel_opened:
            logger.warning("[FlowVideoClient] Config panel failed to open, will retry next call")
            return

        # Switch to Video tab
        result = await self._click_tab("Video")
        if result == 'clicked':
            logger.info("[FlowVideoClient] Switched to Video tab")
            await asyncio.sleep(0.5)
        elif result is None:
            logger.warning("[FlowVideoClient] Could not find Video tab")

        # Select orientation (Landscape / Portrait)
        await self._select_orientation()

        # Select count (x1 / x2 / x3 / x4)
        await self._select_count()

        # Select video model from dropdown
        await self._select_video_model()

        # Select duration if provided
        if self.duration:
            await self._select_duration()

        # Close the config panel by clicking outside it
        await self.page.mouse.click(10, 10)
        await asyncio.sleep(0.5)

        # Verify by reading the config button text
        btn_text = await self.page.evaluate("""() => {
            const viewH = window.innerHeight;
            for (const btn of document.querySelectorAll('button[aria-haspopup="menu"]')) {
                const rect = btn.getBoundingClientRect();
                if (rect.y > viewH * 0.7 && rect.width > 30) {
                    return (btn.textContent || '').trim();
                }
            }
            return null;
        }""")
        logger.info("[FlowVideoClient] Config button now shows: %s", btn_text)

        # Verify orientation was actually applied by checking the button text
        expected_icon = "crop_16_9" if self.orientation == FlowOrientation.LANDSCAPE else "crop_9_16"
        if btn_text and expected_icon not in btn_text:
            logger.warning(
                "[FlowVideoClient] Orientation mismatch! Expected '%s' in button text '%s'. "
                "Re-opening config panel to fix orientation...",
                expected_icon, btn_text,
            )
            panel_opened = await self._open_config_panel()
            if panel_opened:
                await self._select_orientation()
                await self.page.mouse.click(10, 10)
                await asyncio.sleep(0.5)

        # Verify count was actually applied. The config button text
        # contains the count glyph in either "Nx" (button accessible
        # name: "Video crop_16_9 1x") or "xN" (older textContent form
        # like 'Videocrop_16_9x2'). Match either; require it to equal
        # the requested count. If x1 was requested but the button still
        # shows x2/x3/x4, Flow will spawn multiple videos — don't ship
        # that, fail loudly so the caller can retry / surface an error.
        expected_count = COUNT_UI_LABELS[self.count]   # e.g. '1x'
        expected_n = expected_count[:-1] if expected_count.endswith("x") else expected_count

        def _extract_count_n(text: str | None) -> str | None:
            """Return the bare digit ('1' / '2' / …) found in btn text."""
            if not text:
                return None
            m = re.search(r"\b([1-4])\s*x\b", text)
            if m:
                return m.group(1)
            m = re.search(r"\bx\s*([1-4])\b", text)
            if m:
                return m.group(1)
            return None

        if btn_text:
            actual_n = _extract_count_n(btn_text)
            if actual_n and actual_n != expected_n:
                logger.warning(
                    "[FlowVideoClient] Count mismatch! Expected %s (n=%s) but "
                    "config button shows n=%s (full text=%r). Re-opening config to fix...",
                    expected_count, expected_n, actual_n, btn_text,
                )
                panel_opened = await self._open_config_panel()
                if panel_opened:
                    await self._select_count()
                    await self.page.mouse.click(10, 10)
                    await asyncio.sleep(0.5)
                # Re-read button text and verify; raise if still wrong.
                btn_text = await self.page.evaluate("""() => {
                    const viewH = window.innerHeight;
                    for (const btn of document.querySelectorAll('button[aria-haspopup="menu"]')) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.y > viewH * 0.7 && rect.width > 30) {
                            return (btn.textContent || '').trim();
                        }
                    }
                    return null;
                }""")
                actual_n = _extract_count_n(btn_text)
                if actual_n != expected_n:
                    raise RuntimeError(
                        f"Count selection failed: expected {expected_count} (n={expected_n}) "
                        f"but config button shows n={actual_n!r} (text={btn_text!r}). "
                        f"Refusing to submit — would generate {actual_n}x videos "
                        f"instead of {expected_n}."
                    )
                logger.info("[FlowVideoClient] Count corrected to %s after retry", expected_count)

        self._config_applied = True
        logger.info(
            "[FlowVideoClient] Config applied: model=%s, orientation=%s, count=%s",
            self.model.name, self.orientation.name, self.count.name,
        )

    async def _select_orientation(self) -> bool:
        """Select Landscape or Portrait tab in the open config panel.

        Uses crop_16_9 / crop_9_16 Material Icons as locale-independent identifiers.

        Primary approach: Playwright ``get_by_role("tab")`` locator — more
        reliable than raw ``mouse.click(x, y)`` because Playwright waits for
        the element to be actionable and dispatches events through the
        framework's event system.

        Falls back to JS evaluate + mouse.click, then to text label.
        """
        icon = "crop_16_9" if self.orientation == FlowOrientation.LANDSCAPE else "crop_9_16"

        # ── Primary: Playwright locator (matches accessible name) ──
        try:
            tab = self.page.get_by_role("tab", name=re.compile(icon))
            if await tab.count() > 0:
                first = tab.first
                is_selected = await first.get_attribute("aria-selected")
                if is_selected == "true":
                    logger.info("[FlowVideoClient] Orientation '%s' already selected (locator)", icon)
                else:
                    await first.click()
                    await asyncio.sleep(0.5)
                    logger.info("[FlowVideoClient] Selected orientation via locator: %s", icon)
                return True
        except Exception as e:
            logger.debug("[FlowVideoClient] Locator orientation failed, trying JS: %s", e)

        # ── Fallback 1: JS evaluate + mouse.click ──
        tab_info = await self.page.evaluate("""(icon) => {
            for (const tab of document.querySelectorAll('[role="tab"]')) {
                const text = tab.textContent || '';
                if (text.includes(icon)) {
                    const rect = tab.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        return {
                            selected: tab.getAttribute('aria-selected') === 'true',
                            x: rect.x + rect.width / 2,
                            y: rect.y + rect.height / 2,
                        };
                    }
                }
            }
            return null;
        }""", icon)

        if not tab_info:
            # Fallback 2: text label
            label = ORIENTATION_UI_LABELS[self.orientation]
            result = await self._click_tab(label)
            if result is None:
                logger.warning("[FlowVideoClient] Could not find orientation tab (icon=%s, label=%s)", icon, label)
                return False
            logger.info("[FlowVideoClient] Selected orientation via label: %s", label)
            return True

        if tab_info['selected']:
            logger.info("[FlowVideoClient] Orientation '%s' already selected", icon)
        else:
            await self.page.mouse.click(tab_info['x'], tab_info['y'])
            await asyncio.sleep(0.5)
            logger.info("[FlowVideoClient] Selected orientation: %s", icon)
        return True

    async def _verify_orientation(self) -> None:
        """Read the config button text and re-apply orientation if it drifted.

        Called right before prompt submission as a safety net — any prior
        config-panel interaction (frames mode, ingredients tab, image upload)
        could have changed the orientation back to the default.
        """
        expected_icon = "crop_16_9" if self.orientation == FlowOrientation.LANDSCAPE else "crop_9_16"
        btn_text = await self.page.evaluate("""() => {
            const viewH = window.innerHeight;
            for (const btn of document.querySelectorAll('button[aria-haspopup="menu"]')) {
                const rect = btn.getBoundingClientRect();
                if (rect.y > viewH * 0.7 && rect.width > 30) {
                    return (btn.textContent || '').trim();
                }
            }
            return null;
        }""")

        if not btn_text:
            logger.debug("[FlowVideoClient] _verify_orientation: config button not found")
            return

        if expected_icon in btn_text:
            logger.debug("[FlowVideoClient] Orientation OK: '%s' in '%s'", expected_icon, btn_text)
            return

        logger.warning(
            "[FlowVideoClient] Orientation drift detected! Expected '%s' in '%s'. Fixing...",
            expected_icon, btn_text,
        )
        panel_opened = await self._open_config_panel()
        if panel_opened:
            await self._select_orientation()
            await self.page.mouse.click(10, 10)
            await asyncio.sleep(0.5)
            logger.info("[FlowVideoClient] Orientation re-applied after drift")

    async def _select_count(self) -> bool:
        """Select x1/x2/x3/x4 in the open config panel.

        Flow's count selector is not always a ``role="tab"`` and the
        accessible name varies between builds (sometimes ``x1``,
        sometimes ``Generate 1 video``, sometimes the visible glyph
        ``x1`` is just inner text on a button). We try multiple
        strategies so a UI tweak doesn't silently leave us on the
        default ``x2`` (which silently doubles billing and produces
        two videos when the API contract is one).
        """
        target = COUNT_UI_LABELS[self.count]  # '1x' / '2x' / '3x' / '4x'
        # Bare number for fuzzy matching ("1x" -> "1", "Generate 1 video")
        target_n = target[:-1] if target.endswith("x") else target

        # ── Strategy 1: role=tab exact name ──
        try:
            tab = self.page.get_by_role("tab", name=target, exact=True)
            if await tab.count() > 0:
                first = tab.first
                is_selected = await first.get_attribute("aria-selected")
                if is_selected == "true":
                    logger.info("[FlowVideoClient] Count '%s' already selected (tab locator)", target)
                else:
                    await first.click()
                    await asyncio.sleep(0.5)
                    logger.info("[FlowVideoClient] Selected count via tab locator: %s", target)
                return True
        except Exception as e:
            logger.debug("[FlowVideoClient] Count tab locator failed for '%s': %s", target, e)

        # ── Strategy 2: role=tab/button/radio with regex name (handles
        # accessible names like 'Generate 1 video' / '1 video' / 'x1') ──
        for role in ("tab", "button", "radio"):
            try:
                pat = re.compile(rf"(^|[^\w]){re.escape(target)}([^\w]|$)|\b{re.escape(target_n)}\s*videos?\b", re.I)
                el = self.page.get_by_role(role, name=pat)
                if await el.count() > 0:
                    first = el.first
                    is_selected = await first.get_attribute("aria-selected")
                    if is_selected != "true":
                        await first.click()
                        await asyncio.sleep(0.5)
                    logger.info("[FlowVideoClient] Selected count via %s locator: %s", role, target)
                    return True
            except Exception as e:
                logger.debug("[FlowVideoClient] Count %s locator failed for '%s': %s", role, target, e)

        # ── Strategy 3: JS scan — look for tab/button/radio whose
        # textContent contains exactly the target glyph (e.g. 'x1') as a
        # standalone token. Also accept aria-label match. ──
        tab_info = await self.page.evaluate("""({target, targetN}) => {
            const sel = '[role="tab"], [role="button"], [role="radio"], button';
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            for (const el of document.querySelectorAll(sel)) {
                const text = norm(el.textContent);
                const aria = norm(el.getAttribute('aria-label') || '');
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                // Must be a short label so we don't click the prompt area
                if (text.length > 30 && aria.length > 30) continue;
                const has = (s) => {
                    if (!s) return false;
                    if (s === target) return true;
                    // standalone 'x1' token
                    if (new RegExp('(^|[^\\\\w])' + target + '([^\\\\w]|$)').test(s)) return true;
                    // 'Generate 1 video' / '1 video'
                    if (new RegExp('\\\\b' + targetN + '\\\\s*videos?\\\\b', 'i').test(s)) return true;
                    return false;
                };
                if (has(text) || has(aria)) {
                    return {
                        selected: el.getAttribute('aria-selected') === 'true' || el.getAttribute('aria-checked') === 'true',
                        x: rect.x + rect.width / 2,
                        y: rect.y + rect.height / 2,
                        text, aria,
                    };
                }
            }
            return null;
        }""", {"target": target, "targetN": target_n})

        if not tab_info:
            logger.warning(
                "[FlowVideoClient] Could not find count selector for '%s' (tried tab/button/radio + JS)",
                target,
            )
            return False
        if tab_info['selected']:
            logger.info(
                "[FlowVideoClient] Count '%s' already selected (JS, text=%r, aria=%r)",
                target, tab_info.get('text'), tab_info.get('aria'),
            )
        else:
            await self.page.mouse.click(tab_info['x'], tab_info['y'])
            await asyncio.sleep(0.5)
            logger.info(
                "[FlowVideoClient] Selected count via JS: %s (text=%r, aria=%r)",
                target, tab_info.get('text'), tab_info.get('aria'),
            )
        return True

    async def _select_video_model(self) -> bool:
        """Select video model from the dropdown inside the open config panel.

        Primary: Playwright locators for dropdown button and menu items.
        Fallback: JS evaluate + mouse.click.
        """
        target_label = VIDEO_MODEL_UI_LABELS[self.model]

        # ── Primary: Playwright locator ──
        try:
            # Dropdown button contains model name + arrow_drop_down icon
            dd_btn = self.page.get_by_role("button", name=re.compile(r"arrow_drop_down"))
            if await dd_btn.count() > 0:
                btn_name = await dd_btn.first.get_attribute("aria-label") or \
                           await dd_btn.first.inner_text()
                if target_label in (btn_name or ""):
                    logger.info("[FlowVideoClient] Model '%s' already selected (locator)", target_label)
                    return True

                await dd_btn.first.click()
                await asyncio.sleep(0.5)

                # Menu items: match by model label
                # Recording shows role="button" for menu items (e.g. "volume_up Veo 3.1 - Lite [")
                menu_item = self.page.get_by_role("button", name=re.compile(re.escape(target_label)))
                if await menu_item.count() > 0:
                    await menu_item.first.click()
                    await asyncio.sleep(0.3)
                    logger.info("[FlowVideoClient] Selected model via locator: %s", target_label)
                    return True

                # Also try menuitem role as fallback within locator path
                menu_item2 = self.page.get_by_role("menuitem", name=re.compile(re.escape(target_label)))
                if await menu_item2.count() > 0:
                    await menu_item2.first.click()
                    await asyncio.sleep(0.3)
                    logger.info("[FlowVideoClient] Selected model via menuitem locator: %s", target_label)
                    return True

                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
                logger.debug("[FlowVideoClient] Model locator: '%s' not found in menu", target_label)
        except Exception as e:
            logger.debug("[FlowVideoClient] Model locator failed: %s", e)

        # ── Fallback: JS evaluate + mouse.click ──
        model_btn = await self.page.evaluate("""() => {
            for (const btn of document.querySelectorAll('button')) {
                const text = (btn.textContent || '').trim();
                const rect = btn.getBoundingClientRect();
                if (text.includes('arrow_drop_down') && rect.width > 80 && rect.height > 0) {
                    return {
                        text: text,
                        x: rect.x + rect.width / 2,
                        y: rect.y + rect.height / 2,
                    };
                }
            }
            return null;
        }""")

        if not model_btn:
            logger.warning("[FlowVideoClient] Could not find model dropdown button")
            return False

        # Check if already the right model
        if target_label in model_btn['text']:
            logger.info("[FlowVideoClient] Model '%s' already selected", target_label)
            return True

        # Open the model dropdown
        await self.page.mouse.click(model_btn['x'], model_btn['y'])
        await asyncio.sleep(0.5)

        # Click the target model from the dropdown menu
        clicked = await self.page.evaluate("""(target) => {
            for (const item of document.querySelectorAll('[role="menuitem"]')) {
                const text = (item.textContent || '').trim();
                if (text.includes(target)) {
                    item.click();
                    return text;
                }
            }
            return null;
        }""", target_label)

        if clicked:
            logger.info("[FlowVideoClient] Selected model: %s", clicked)
            await asyncio.sleep(0.3)
        else:
            logger.warning("[FlowVideoClient] Model '%s' not found in dropdown", target_label)
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.2)

        return clicked is not None

    async def _select_duration(self) -> bool:
        """Select requested duration for Omni Flash (4s, 6s, 8s, 10s) via tabs in the config panel."""
        if not self.duration:
            return False

        dur_str = f"{int(self.duration)}s"
        is_omni = self.model == FlowVideoModel.OMNI_FLASH
        allowed = ("4s", "6s", "8s", "10s") if is_omni else ("4s", "6s", "8s")
        
        if dur_str not in allowed:
            logger.warning("[FlowVideoClient] Unsupported duration requested for model %s: %s", self.model, self.duration)
            return False

        logger.info("[FlowVideoClient] Selecting duration: %s", dur_str)
        try:
            tab = self.page.get_by_role("tab", name=dur_str)
            if await tab.count() > 0:
                await tab.click()
                await asyncio.sleep(0.5)
                logger.info("[FlowVideoClient] Duration %s selected successfully", dur_str)
                return True
            else:
                # Try locating by text if role is different
                fallback = self.page.get_by_text(dur_str, exact=True)
                if await fallback.count() > 0:
                    await fallback.click()
                    await asyncio.sleep(0.5)
                    logger.info("[FlowVideoClient] Duration %s selected via text fallback", dur_str)
                    return True
                logger.warning("[FlowVideoClient] Duration tab '%s' not found in DOM", dur_str)
                return False
        except Exception as e:
            logger.warning("[FlowVideoClient] Failed to select duration %s: %s", dur_str, e)
            return False

    def _setup_video_listener(self) -> tuple[list[str], object]:
        """Set up a network response listener to capture signed GCS video URLs.

        Captures URLs from two sources:
        1. storage.googleapis.com responses with video content-type
        2. Batch API responses containing signed video download URLs

        Returns (captured_urls_list, listener_fn) — caller must remove listener.
        """
        captured: list[str] = []

        async def _extract_batch_urls(response):
            """Parse batch API response for signed video URLs."""
            try:
                body = await response.json()
                def find_urls(obj):
                    if isinstance(obj, str):
                        if ('storage.googleapis.com' in obj
                                and ('video' in obj or '.mp4' in obj)):
                            if obj not in captured:
                                captured.append(obj)
                    elif isinstance(obj, dict):
                        for v in obj.values():
                            find_urls(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            find_urls(item)
                find_urls(body)
                if captured:
                    logger.info(
                        "[FlowVideoClient] Extracted %d video URLs from API response",
                        len(captured),
                    )
            except Exception as e:
                logger.warning("[FlowVideoClient] Failed to parse batch response: %s", e)

        def on_response(response):
            url = response.url
            ct = response.headers.get('content-type', '')
            # Capture individual GCS video responses
            if ('storage.googleapis.com' in url
                    and response.status == 200
                    and ('video' in ct or '.mp4' in url)):
                if url not in captured:
                    captured.append(url)
                    logger.info("[FlowVideoClient] Captured GCS video URL: %s", url[:120])
            # Capture batch generation API responses (may contain video URLs)
            elif (response.status == 200
                  and ('generateVideos' in url or 'batchGenerate' in url)):
                asyncio.ensure_future(_extract_batch_urls(response))

        self.page.on('response', on_response)
        return captured, on_response

    async def _collect_existing_videos(self) -> set[str]:
        """Snapshot all video URLs currently in the DOM (before submitting)."""
        urls = await self.page.evaluate("""() => {
            const urls = [];
            for (const v of document.querySelectorAll('video')) {
                const src = v.src || v.getAttribute('src') || '';
                if (src && src.length > 50) urls.push(src);
                for (const s of v.querySelectorAll('source')) {
                    const ssrc = s.src || s.getAttribute('src') || '';
                    if (ssrc && ssrc.length > 50) urls.push(ssrc);
                }
            }
            return urls;
        }""")
        logger.info("[FlowVideoClient] Existing videos in DOM: %d", len(urls))
        return set(urls)

    async def _wait_for_video(
        self,
        captured_urls: list[str],
        existing_videos: set[str],
        timeout_seconds: int = 300,
    ) -> list[str]:
        """Wait for videos to appear via network capture or DOM.

        Also checks the DOM periodically for error cards (warning icons)
        that signal a generation failure (policy violation, safety filter,
        etc.). When one is detected the method returns early so the caller
        can inspect the error instead of waiting for the full timeout.

        Sets ``self._dom_error`` to the card text when an error card is
        found, or ``None`` otherwise.

        Args:
            captured_urls: Mutable list populated by the response listener.
            existing_videos: URLs already present before submission (to exclude).
            timeout_seconds: Maximum wait time (default 300s for video generation).

        Returns list of NEW video URLs found.
        """
        expected_count = self.count.value
        self._dom_error: Optional[str] = None
        # Allow ONE in-place retry via the error card's Retry button before
        # we surface PUBLIC_ERROR_SAFETY_FILTER to the caller. Flow's
        # safety-filter rejections are often transient (it sometimes rejects
        # an identical prompt that succeeded a second earlier), so this
        # absorbs a chunk of false-positive failures without changing the
        # caller contract on persistent errors.
        retries_used = 0
        max_retries = 1
        error_check_skip_until = 0

        for i in range(timeout_seconds):
            # Check captured URLs from network listener (always new, post-listener)
            if len(captured_urls) >= expected_count:
                logger.info(
                    "[FlowVideoClient] Got %d video URLs from network capture",
                    len(captured_urls),
                )
                return captured_urls[:expected_count]

            await asyncio.sleep(1)

            # ── DOM error card detection (every 3 s, starting at 5 s) ──
            # Skip detection for a few seconds after a retry click so the
            # stale error card has time to disappear from the DOM.
            if i >= 5 and i % 3 == 0 and i >= error_check_skip_until:
                error_text = await self._detect_error_card()
                if error_text:
                    logger.warning(
                        "[FlowVideoClient] Error card detected in DOM after %ds: %s",
                        i, error_text[:200],
                    )
                    await self._screenshot(f"err_card_{i:04d}s")

                    if retries_used < max_retries:
                        retries_used += 1
                        logger.info(
                            "[FlowVideoClient] Clicking Retry (attempt %d/%d)",
                            retries_used, max_retries,
                        )
                        if await self._click_error_retry():
                            # Old error card may linger a moment; suppress
                            # detection until i+10 so we don't immediately
                            # re-trigger on the same DOM.
                            error_check_skip_until = i + 10
                            continue
                        logger.warning(
                            "[FlowVideoClient] Retry button click failed — "
                            "surfacing original error"
                        )

                    self._dom_error = error_text
                    return []  # early exit — caller will read self._dom_error

            # Periodically check DOM for <video> elements as fallback
            if i > 15 and i % 10 == 0:
                video_srcs = await self.page.evaluate("""() => {
                    const urls = [];
                    for (const v of document.querySelectorAll('video')) {
                        const src = v.src || v.getAttribute('src') || '';
                        if (src && src.length > 50
                            && !src.includes('loading')
                            && !src.includes('animation')
                            && !src.includes('static')) {
                            urls.push(src);
                        }
                        // Check <source> children
                        for (const s of v.querySelectorAll('source')) {
                            const ssrc = s.src || s.getAttribute('src') || '';
                            if (ssrc && ssrc.length > 50
                                && !ssrc.includes('loading')
                                && !ssrc.includes('animation')) {
                                urls.push(ssrc);
                            }
                        }
                    }
                    return urls;
                }""")
                if video_srcs:
                    for src in video_srcs:
                        # Only add genuinely NEW videos (not in existing set or already captured)
                        if src not in existing_videos and src not in captured_urls:
                            captured_urls.append(src)
                            logger.info("[FlowVideoClient] Found NEW video in DOM: %s", src[:120])
                    if len(captured_urls) >= expected_count:
                        return captured_urls[:expected_count]

            if i > 0 and i % 30 == 0:
                logger.info(
                    "[FlowVideoClient] Waiting for video... %d/%d captured (%ds/%ds)",
                    len(captured_urls), expected_count, i, timeout_seconds,
                )
                await self._screenshot(f"wait_{i:04d}s")

        # Return whatever we have at timeout
        return captured_urls

    async def download_video(
        self,
        url: str,
        output_dir: Path | None = None,
    ) -> tuple[Path | None, str | None]:
        """Download a video file. Returns (local_path, filename) or (None, None).

        Tries authenticated download first (needed for labs.google redirect URLs),
        then falls back to plain download (for direct GCS signed URLs).
        """
        # Authenticated URLs (labs.google/fx/api/trpc/media redirect)
        if 'labs.google' in url or 'getMediaUrlRedirect' in url:
            result = await self._download_with_cookies(
                url, output_dir=output_dir, extension=".mp4", timeout=120.0,
            )
            if result[0] is not None:
                return result
            logger.warning("[FlowVideoClient] Authenticated download failed, trying plain...")

        return await self.download_file(url, output_dir=output_dir, extension=".mp4", timeout=120.0)

    async def _cleanup_uploaded_items(self, count: int) -> None:
        """Delete uploaded frame/reference images from the virtuoso item list.

        After generation, uploaded images remain in the project list and must
        be removed to keep the project clean. Uses hover → more_vert → delete
        (all locale-independent Material Icons).
        """
        deleted = 0
        for i in range(count):
            # Find an image item in the list (not video — images have img tags with small thumbnails).
            # Hover over it to reveal the more_vert button, then click it.
            item_coords = await self.page.evaluate("""() => {
                // Look in virtuoso list or entire sidebar for image items
                const candidates = [...document.querySelectorAll('img')].filter(img => {
                    const rect = img.getBoundingClientRect();
                    // Small thumbnail: roughly square, not huge
                    return rect.width > 20 && rect.width < 200 && rect.height > 20
                        && Math.abs(rect.width - rect.height) < rect.width * 0.5;
                });
                if (!candidates.length) return null;
                const img = candidates[0];
                const rect = img.getBoundingClientRect();
                return { x: Math.round(rect.x + rect.width / 2), y: Math.round(rect.y + rect.height / 2) };
            }""")

            if not item_coords:
                logger.warning("[FlowVideoClient] Cleanup: no image thumbnail found for item %d/%d", i + 1, count)
                break

            # Hover to reveal more_vert button
            await self.page.mouse.move(item_coords['x'], item_coords['y'])
            await asyncio.sleep(0.5)

            # Click more_vert button that became visible
            found = await self.page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button')) {
                    if ((btn.textContent || '').includes('more_vert')) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            btn.click();
                            return true;
                        }
                    }
                }
                return false;
            }""")

            if not found:
                logger.warning("[FlowVideoClient] Cleanup: could not find more_vert for item %d/%d", i + 1, count)
                continue

            await asyncio.sleep(0.3)

            await self.page.evaluate("""() => {
                for (const item of document.querySelectorAll('[role="menuitem"]')) {
                    if ((item.textContent || '').includes('delete')) {
                        item.click();
                        return true;
                    }
                }
            }""")

            await asyncio.sleep(0.5)
            deleted += 1

        if deleted > 0:
            logger.info("[FlowVideoClient] Cleaned up %d/%d uploaded item(s)", deleted, count)
        elif count > 0:
            logger.warning("[FlowVideoClient] Cleanup: deleted 0/%d items", count)

    async def _activate_frames_mode(self) -> bool:
        """Open config panel, click the Frames (crop_free) tab, then close panel.

        Makes the start/end frame slot elements visible in the main content area.

        IMPORTANT: this used to short-circuit on a per-instance
        ``_frames_mode_active`` cache. That cache is unsafe — if any other
        flow path (e.g. R2V) selects a different tab (Ingredients /
        chrome_extension) on the same FlowVideoClient between two I2V
        generations, the cache lies and the upload step silently fails
        because the frame slots aren't on screen. We now verify the tab
        state on every call via ``_click_tab`` (which checks
        ``aria-selected``) and only mark the cache when the selection is
        confirmed.
        """
        # Fast path: cache says active AND we can confirm on the DOM that the
        # Frames tab is actually selected right now. Otherwise re-activate.
        if self._frames_mode_active:
            still_active = await self.page.evaluate("""() => {
                for (const tab of document.querySelectorAll('[role="tab"]')) {
                    const text = (tab.textContent || '').trim();
                    if (text.includes('crop_free') || /frames?/i.test(text)) {
                        if (tab.getAttribute('aria-selected') === 'true') return true;
                    }
                }
                return false;
            }""")
            if still_active:
                return True
            logger.info(
                "[FlowVideoClient] Frames mode cache stale (a different tab is "
                "selected) — re-activating"
            )
            self._frames_mode_active = False

        # Try up to 3 cycles of (open panel → click Frames tab → verify).
        for attempt in range(3):
            panel_opened = await self._open_config_panel()
            if not panel_opened:
                logger.warning(
                    "[FlowVideoClient] Could not open config panel for frames mode "
                    "(attempt %d/3)", attempt + 1,
                )
                await asyncio.sleep(0.5)
                continue

            selected = False
            for label in ("crop_free", "Frames"):
                result = await self._click_tab(label)
                if result is None:
                    continue
                # Verify aria-selected — old evaluate-only path could land on
                # a non-tab element with matching text and report success.
                selected = await self.page.evaluate("""(target) => {
                    for (const tab of document.querySelectorAll('[role="tab"]')) {
                        const text = (tab.textContent || '').trim();
                        if (text.includes(target)) {
                            return tab.getAttribute('aria-selected') === 'true';
                        }
                    }
                    return false;
                }""", label)
                if selected:
                    logger.info(
                        "[FlowVideoClient] Frames mode activated (%s, %s)",
                        label, result,
                    )
                    break
                logger.debug(
                    "[FlowVideoClient] Frames tab click reported '%s' "
                    "but aria-selected=false (label=%s)", result, label,
                )

            # Close the config panel so the slot elements are visible.
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                # Last-resort fallback: click in the top-left corner to dismiss.
                try:
                    await self.page.mouse.click(10, 10)
                except Exception:
                    pass
            await asyncio.sleep(0.5)

            if selected:
                self._frames_mode_active = True
                return True

            logger.warning(
                "[FlowVideoClient] Frames tab not selected after attempt %d/3",
                attempt + 1,
            )
            await asyncio.sleep(0.5)

        logger.warning(
            "[FlowVideoClient] Could not activate Frames mode (crop_free) after 3 attempts"
        )
        return False

    async def _upload_file_to_project(self, path: Path) -> bool:
        """Upload a file directly to the project's media library via the Add Media menu.

        Args:
            path: Local path to the file.
        """
        abs_path = str(path.resolve())
        filename = path.name

        logger.info("[FlowVideoClient] Uploading file %s to project media library...", filename)

        # 1. Click "add Thêm nội dung nghe nhìn" button.
        # Try different languages and text variations.
        add_clicked = False
        for label in [
            re.compile(r"add\s+Thêm nội dung nghe nhìn", re.I),
            re.compile(r"add\s+Add media", re.I),
            re.compile(r"add\s+Добавить медиа", re.I),
            re.compile(r"Thêm nội dung nghe nhìn", re.I),
            re.compile(r"Add media", re.I),
            re.compile(r"Добавить медиа", re.I),
            re.compile(r"add", re.I),
        ]:
            try:
                loc = self.page.locator('button, [role="button"]').filter(has_text=label).first
                if await loc.is_visible(timeout=1000):
                    await loc.click()
                    add_clicked = True
                    break
            except Exception:
                continue

        if not add_clicked:
            # Fallback: try raw evaluate clicking any button with 'add' or similar
            add_clicked = await self.page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button, [role="button"]')) {
                    const text = (btn.textContent || '').trim();
                    if (text.includes('add') || text.includes('Thêm') || text.includes('media')) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 0) { btn.click(); return true; }
                    }
                }
                return false;
            }""")

        if not add_clicked:
            logger.warning("[FlowVideoClient] Could not find or click 'Add media' button")
            return False

        await asyncio.sleep(0.5)

        # 2. Click "upload Tải nội dung nghe nhìn" menu item.
        upload_clicked = False
        for label in [
            re.compile(r"upload\s+Tải nội dung nghe nhìn", re.I),
            re.compile(r"upload\s+Upload media", re.I),
            re.compile(r"upload\s+Загрузить медиа", re.I),
            re.compile(r"Tải nội dung nghe nhìn", re.I),
            re.compile(r"Upload media", re.I),
            re.compile(r"Загрузить медиа", re.I),
            re.compile(r"upload", re.I),
        ]:
            try:
                loc = self.page.locator('[role="menuitem"], [role="option"], button').filter(has_text=label).first
                if await loc.is_visible(timeout=1000):
                    async with self.page.expect_file_chooser() as fc_info:
                        await loc.click()
                    file_chooser = await fc_info.value
                    
                    timeout = 180.0 if path.suffix.lower() == ".mp4" else 90.0
                    logger.info("[FlowVideoClient] Setting file %s and expecting upload response...", filename)
                    try:
                        async with self.page.expect_response(
                            lambda r: "/v1/flow/upload" in r.url.lower(),
                            timeout=timeout * 1000
                        ) as resp_info:
                            await file_chooser.set_files(abs_path)
                        response = await resp_info.value
                        logger.info(
                            "[FlowVideoClient] File upload network request completed successfully: url=%s, status=%s",
                            response.url, response.status
                        )
                        await self._dismiss_rights_notice()
                        await asyncio.sleep(1.5)
                        upload_clicked = True
                        return True
                    except Exception as ne:
                        logger.warning(
                            "[FlowVideoClient] Network expectation failed or timed out: %s. "
                            "Setting files and falling back to spinner/time polling.", ne
                        )
                        await file_chooser.set_files(abs_path)
                        upload_clicked = True
                        break
            except Exception:
                continue

        if not upload_clicked:
            # Fallback: try raw evaluate
            try:
                async with self.page.expect_file_chooser() as fc_info:
                    await self.page.evaluate("""() => {
                        for (const item of document.querySelectorAll('[role="menuitem"], li, button')) {
                            const text = (item.textContent || '').trim();
                            if (text.includes('upload') || text.includes('Tải') || text.includes('Загрузить')) {
                                const rect = item.getBoundingClientRect();
                                if (rect.width > 0) { item.click(); return true; }
                            }
                        }
                        return false;
                    }""")
                file_chooser = await fc_info.value
                
                timeout = 180.0 if path.suffix.lower() == ".mp4" else 90.0
                logger.info("[FlowVideoClient] Setting file %s and expecting upload response (fallback)...", filename)
                try:
                    async with self.page.expect_response(
                        lambda r: "/v1/flow/upload" in r.url.lower(),
                        timeout=timeout * 1000
                    ) as resp_info:
                        await file_chooser.set_files(abs_path)
                    response = await resp_info.value
                    logger.info(
                        "[FlowVideoClient] File upload network request completed successfully (fallback): url=%s, status=%s",
                        response.url, response.status
                    )
                    await self._dismiss_rights_notice()
                    await asyncio.sleep(1.5)
                    upload_clicked = True
                    return True
                except Exception as ne:
                    logger.warning(
                        "[FlowVideoClient] Network expectation failed or timed out (fallback): %s. "
                        "Setting files and falling back to spinner/time polling.", ne
                    )
                    await file_chooser.set_files(abs_path)
                    upload_clicked = True
            except Exception as e:
                logger.warning("[FlowVideoClient] Fallback upload click failed: %s", e)

        if not upload_clicked:
            logger.warning("[FlowVideoClient] Could not find or click 'Upload media' menuitem")
            return False

        logger.info("[FlowVideoClient] File %s set in Playwright file chooser, waiting for upload to finish...", filename)
        
        # Wait up to 90 seconds (180 seconds for videos) for the upload to complete
        timeout = 180.0 if path.suffix.lower() == ".mp4" else 90.0
        deadline = time.monotonic() + timeout
        
        # Give the UI a small beat (1-2 seconds) to start the upload and show the spinner
        await asyncio.sleep(2.0)
        
        last_log = time.monotonic()
        while time.monotonic() < deadline:
            # Dismiss rights notice if shown during upload
            await self._dismiss_rights_notice()
            
            spinner = await self._has_upload_spinner()
            if not spinner:
                logger.info("[FlowVideoClient] Upload finished: spinner disappeared")
                break
                
            now = time.monotonic()
            if now - last_log > 10.0:
                logger.info("[FlowVideoClient] Still uploading %s to project media library...", filename)
                last_log = now
                
            await asyncio.sleep(0.5)
            
        # Settle time
        await asyncio.sleep(1.5)
        return True

    async def _upload_frame_image(self, slot: str, path: Path) -> bool:
        """Upload an image to the start or end frame slot using the new media library path.

        Args:
            slot: 'start' for the first frame, 'end' for the last frame.
            path: Local path to the image file.
        """
        # First, check if the model is OMNI_FLASH. If it is, start/end frames are prohibited!
        if self.model == FlowVideoModel.OMNI_FLASH:
            raise ValueError("Omni Flash model does not support start/end frame images, only references!")

        # Step 1: Upload the file directly to the project's media library
        uploaded = await self._upload_file_to_project(path)
        if not uploaded:
            logger.warning("[FlowVideoClient] Direct file upload to project failed for %s slot", slot)
            return False

        # Step 2: Activate frames mode so the frame slots appear in the UI
        ok = await self._activate_frames_mode()
        if not ok:
            logger.warning("[FlowVideoClient] Frames mode activation failed")
            return False

        # Step 3: Click the appropriate frame slot (using the swap button anchor)
        slot_result = await self.page.evaluate("""(isStart) => {
            // Find swap button (has swap_horiz in text, confirmed visible in DOM)
            let swapBtn = null;
            for (const btn of document.querySelectorAll('button')) {
                if ((btn.textContent || '').includes('swap_horiz')) {
                    const r = btn.getBoundingClientRect();
                    if (r.width > 0) { swapBtn = btn; break; }
                }
            }
            if (!swapBtn) return { ok: false, debug: 'swap button not found' };

            const sr = swapBtn.getBoundingClientRect();

            // Find all visible elements near swap button's y-level (±40px)
            // that are NOT the swap button or its descendants
            const nearby = [...document.querySelectorAll('*')].filter(el => {
                if (el === swapBtn || swapBtn.contains(el)) return false;
                const r = el.getBoundingClientRect();
                return r.width > 20 && r.height > 10
                    && Math.abs((r.y + r.height / 2) - (sr.y + sr.height / 2)) < 40;
            });

            // Sort by x; pick leftmost (start) or rightmost (end) relative to swap
            const left  = nearby.filter(el => el.getBoundingClientRect().x + el.getBoundingClientRect().width / 2 < sr.x);
            const right = nearby.filter(el => el.getBoundingClientRect().x > sr.x + sr.width / 2);

            left.sort((a, b)  => b.getBoundingClientRect().x - a.getBoundingClientRect().x);  // rightmost of left candidates
            right.sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);  // leftmost of right candidates

            const target = isStart ? left[0] : right[0];
            if (target) {
                const tr = target.getBoundingClientRect();
                target.click();
                return { ok: true, tag: target.tagName, text: (target.textContent||'').trim().slice(0,30), x: Math.round(tr.x), y: Math.round(tr.y) };
            }

            // Coordinate fallback: click fixed offset from swap center
            return { ok: 'coords', x: Math.round(isStart ? sr.x - 90 : sr.x + sr.width + 60), y: Math.round(sr.y + sr.height / 2) };
        }""", slot == "start")

        if not slot_result or (not slot_result.get('ok')):
            logger.warning("[FlowVideoClient] Could not find %s frame slot", slot)
            return False

        if slot_result.get('ok') == 'coords':
            logger.info("[FlowVideoClient] Using coordinate fallback for %s slot: %s", slot, slot_result)
            await self.page.mouse.click(slot_result['x'], slot_result['y'])
        else:
            logger.info("[FlowVideoClient] Clicked %s slot: tag=%s text=%r pos=[%s,%s]",
                        slot, slot_result.get('tag'), slot_result.get('text'),
                        slot_result.get('x'), slot_result.get('y'))

        await asyncio.sleep(1.0)

        # Step 4: Google Picker opens. Click the option corresponding to the uploaded photo.
        option_regex = re.compile(re.escape(path.stem), re.I)
        logger.info("[FlowVideoClient] Selecting %s from Google media picker (matching stem: %s)...", path.name, path.stem)
        
        option_clicked = False
        for attempt in range(15):
            try:
                # Try role="option" first
                loc = self.page.get_by_role("option", name=option_regex)
                if await loc.count() > 0:
                    await loc.first.click()
                    option_clicked = True
                    break

                # Try generic element matching
                loc = self.page.locator('[role="option"], button, [role="button"], div').filter(has_text=option_regex)
                if await loc.count() > 0:
                    count = await loc.count()
                    for i in range(count):
                        candidate = loc.nth(i)
                        if await candidate.is_visible():
                            await candidate.click()
                            option_clicked = True
                            break
                    if option_clicked:
                        break
            except Exception:
                pass
            await asyncio.sleep(0.5)

        if not option_clicked:
            logger.warning("[FlowVideoClient] Could not find or click media picker option for %s", path.name)
            # Try keypress Escape to close picker so we don't get stuck
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

        logger.info("[FlowVideoClient] Successfully selected %s in Google media picker for %s slot", path.name, slot)
        await asyncio.sleep(1.0)
        return True

    async def _ensure_frame_attached(self, slot: str, path: Path) -> None:
        """Make sure the just-uploaded image is actually attached to the
        start/end slot, not merely dangling in the project's asset library.

        Flow's behaviour after ``set_input_files`` is inconsistent: sometimes
        the file is both uploaded to the project AND bound to the slot, but
        more often the asset-picker panel stays open showing the file as a
        clickable tile — we have to click that tile for the slot to pick it
        up. If we press Submit while the picker is still open, Flow drops
        the reference frame silently (the video downgrades to T2V or fails).

        Detection is locale-independent: we look for any visible DOM node
        whose trimmed text equals ``path.name`` (the exact filename). If
        such a node exists, the picker is still open → click it. If we
        don't see the filename anywhere, the slot is already attached (the
        picker closed itself) and we return quietly.

        If the picker did not auto-open (rare — usually the slot click at
        the top of _upload_frame_image opens it), we re-click the slot once
        to bring it up before searching.
        """
        filename = path.name

        async def _find_library_tile() -> Optional[dict]:
            # Prefer the *leaf* element with matching text (depth-first,
            # innermost wins) — outer containers like the whole panel also
            # contain the filename text but clicking them doesn't trigger
            # selection on Flow's tile grid.
            return await self.page.evaluate("""(fname) => {
                const hits = [];
                for (const el of document.querySelectorAll('*')) {
                    const text = (el.textContent || '').trim();
                    if (text !== fname) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 10 || r.height < 10) continue;
                    let depth = 0, p = el;
                    while (p.parentElement) { depth++; p = p.parentElement; }
                    hits.push({
                        x: Math.round(r.x + r.width / 2),
                        y: Math.round(r.y + r.height / 2),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        tag: el.tagName,
                        depth,
                    });
                }
                hits.sort((a, b) => b.depth - a.depth);
                return hits[0] || null;
            }""", filename)

        async def _slot_has_cdn_image(is_start: bool) -> bool:
            # Locale-independent slot-filled check: does the slot region
            # (the element we would have clicked in _upload_frame_image)
            # contain a CDN <img>? Uses the same swap-button anchor.
            return await self.page.evaluate("""(isStart) => {
                let swapBtn = null;
                for (const btn of document.querySelectorAll('button')) {
                    if ((btn.textContent || '').includes('swap_horiz')) {
                        const r = btn.getBoundingClientRect();
                        if (r.width > 0) { swapBtn = btn; break; }
                    }
                }
                if (!swapBtn) return false;
                const sr = swapBtn.getBoundingClientRect();
                const candidates = [...document.querySelectorAll('*')].filter(el => {
                    if (el === swapBtn || swapBtn.contains(el)) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 20 && r.height > 20
                        && Math.abs((r.y + r.height/2) - (sr.y + sr.height/2)) < 80;
                });
                const side = isStart
                    ? candidates.filter(el => el.getBoundingClientRect().x + el.getBoundingClientRect().width/2 < sr.x)
                    : candidates.filter(el => el.getBoundingClientRect().x > sr.x + sr.width/2);
                side.sort((a, b) =>
                    Math.abs(a.getBoundingClientRect().x - sr.x)
                    - Math.abs(b.getBoundingClientRect().x - sr.x)
                );
                for (const el of side.slice(0, 5)) {
                    for (const img of el.querySelectorAll('img')) {
                        const s = img.currentSrc || img.src || '';
                        if (s.startsWith('https://')
                            && !s.startsWith('data:')
                            && !s.startsWith('blob:')) {
                            return true;
                        }
                    }
                }
                return false;
            }""", is_start)

        is_start = slot == "start"

        # Fast-path: if the slot already shows a CDN image AND no library
        # tile with the filename is visible, we're done.
        if await _slot_has_cdn_image(is_start):
            tile = await _find_library_tile()
            if not tile:
                return

        for attempt in range(3):
            tile = await _find_library_tile()
            if not tile:
                # Picker closed / never opened. If slot now has an image,
                # we're done. Otherwise re-click the slot to bring the
                # picker back and try once more.
                if await _slot_has_cdn_image(is_start):
                    return
                if attempt == 0:
                    logger.info(
                        "[FlowVideoClient] %s slot empty after upload, re-opening picker",
                        slot,
                    )
                    await self._reopen_frame_slot(slot)
                    await asyncio.sleep(0.5)
                    continue
                logger.warning(
                    "[FlowVideoClient] %s slot: library tile for %r not found "
                    "and slot has no CDN image — proceeding anyway",
                    slot, filename,
                )
                return

            logger.info(
                "[FlowVideoClient] %s slot: attaching %r from library tile at (%d, %d)",
                slot, filename, tile['x'], tile['y'],
            )
            await self.page.mouse.click(tile['x'], tile['y'])
            await asyncio.sleep(0.8)

            # Success when the tile is gone AND the slot has a CDN image.
            tile_after = await _find_library_tile()
            if not tile_after and await _slot_has_cdn_image(is_start):
                logger.info("[FlowVideoClient] %s slot attached successfully", slot)
                return

        logger.warning(
            "[FlowVideoClient] %s slot: still could not confirm attachment of %r "
            "after 3 tries",
            slot, filename,
        )

    async def _reopen_frame_slot(self, slot: str) -> None:
        """Click the start/end slot again to re-open its asset-picker panel.

        Uses the same swap-button anchor as ``_upload_frame_image`` so the
        logic is locale-independent. Swallows all errors — this is a
        best-effort recovery step.
        """
        try:
            await self.page.evaluate("""(isStart) => {
                let swapBtn = null;
                for (const btn of document.querySelectorAll('button')) {
                    if ((btn.textContent || '').includes('swap_horiz')) {
                        const r = btn.getBoundingClientRect();
                        if (r.width > 0) { swapBtn = btn; break; }
                    }
                }
                if (!swapBtn) return;
                const sr = swapBtn.getBoundingClientRect();
                const nearby = [...document.querySelectorAll('*')].filter(el => {
                    if (el === swapBtn || swapBtn.contains(el)) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 20 && r.height > 10
                        && Math.abs((r.y + r.height/2) - (sr.y + sr.height/2)) < 40;
                });
                const left  = nearby.filter(el => el.getBoundingClientRect().x + el.getBoundingClientRect().width/2 < sr.x);
                const right = nearby.filter(el => el.getBoundingClientRect().x > sr.x + sr.width/2);
                left.sort((a, b)  => b.getBoundingClientRect().x - a.getBoundingClientRect().x);
                right.sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);
                const target = isStart ? left[0] : right[0];
                if (target) target.click();
            }""", slot == "start")
        except Exception as e:
            logger.debug("[FlowVideoClient] _reopen_frame_slot failed: %s", e)

    async def _count_cdn_thumbnails(self) -> int:
        """Count <img> elements whose src points at a Google CDN.

        ``blob:`` / ``data:`` previews render instantly in the browser as soon
        as the file is selected, so they are NOT a signal of upload success.
        The ``https://...googleusercontent.com`` / ``lh*.googleusercontent`` /
        ``storage.googleapis.com`` src, on the other hand, only appears AFTER
        Flow's backend has ingested the file and produced a CDN thumbnail —
        that's the real "upload finished" indicator.
        """
        try:
            return await self.page.evaluate("""() => {
                let n = 0;
                for (const img of document.querySelectorAll('img')) {
                    const s = img.currentSrc || img.src || '';
                    if (s.startsWith('https://') && !s.startsWith('data:') && !s.startsWith('blob:')) {
                        n++;
                    }
                }
                return n;
            }""")
        except Exception:
            return 0

    async def _wait_for_frame_upload(
        self, slot: str, baseline_cdn_imgs: int, timeout_seconds: float = 90.0,
    ) -> bool:
        """Block until Google finishes ingesting the just-selected frame image.

        ``set_input_files`` only attaches the file to the <input>; the real
        upload happens asynchronously in the background and takes anywhere
        from a couple of seconds (small JPEG, fast link) up to 30-60s (large
        PNG, cold Chrome, slow proxy). Clicking Submit before the upload
        finishes tells Flow to generate with an empty/broken reference frame
        and produces a garbage / silently-T2V video.

        Completion is detected when the number of CDN-hosted <img> elements
        grows (new thumbnail rendered by Flow). As a fallback we also watch
        for a loading spinner (``progress_activity`` icon) to disappear.
        Returns True on success, False on timeout.
        """
        deadline = time.monotonic() + max(5.0, float(timeout_seconds))
        last_log = 0.0
        last_notice_check = 0.0
        while time.monotonic() < deadline:
            # The rights Notice can pop up a beat after set_input_files —
            # while we're already in this wait loop. Poll for it every
            # couple of seconds and dismiss; otherwise the upload stalls
            # and we'd time out for nothing.
            now_t = time.monotonic()
            if now_t - last_notice_check >= 2.0:
                last_notice_check = now_t
                if await self._dismiss_rights_notice():
                    logger.info("[FlowVideoClient] Dismissed rights Notice during %s upload wait", slot)
            count = await self._count_cdn_thumbnails()
            if count > baseline_cdn_imgs:
                logger.info(
                    "[FlowVideoClient] %s frame upload finished (cdn imgs %d → %d)",
                    slot, baseline_cdn_imgs, count,
                )
                # Small settle time for the DOM to fully reflect the uploaded state
                await asyncio.sleep(0.8)
                return True

            # Fallback: if no progress_activity spinner is visible anymore AND
            # at least 3s elapsed, assume the upload completed silently.
            now = time.monotonic()
            if deadline - now < timeout_seconds - 3.0:
                spinner = await self._has_upload_spinner()
                if spinner is False:
                    # Give the CDN img a little more chance; only bail if we
                    # also don't see a new preview after another second.
                    await asyncio.sleep(1.0)
                    count2 = await self._count_cdn_thumbnails()
                    if count2 > baseline_cdn_imgs:
                        logger.info(
                            "[FlowVideoClient] %s frame upload finished (late, cdn imgs %d → %d)",
                            slot, baseline_cdn_imgs, count2,
                        )
                        return True

            if now - last_log > 10.0:
                logger.info(
                    "[FlowVideoClient] Still waiting for %s frame upload... "
                    "(cdn imgs=%d, baseline=%d)",
                    slot, count, baseline_cdn_imgs,
                )
                last_log = now
            await asyncio.sleep(0.5)

        logger.warning(
            "[FlowVideoClient] %s frame upload did not complete within %.0fs "
            "(cdn imgs still == baseline %d) — proceeding anyway, Flow may "
            "reject the generation",
            slot, timeout_seconds, baseline_cdn_imgs,
        )
        return True  # don't fail the whole generation — Flow will error out if truly broken

    async def _has_upload_spinner(self) -> bool | None:
        """Return True if an upload progress spinner is currently visible.

        Flow uses the ``progress_activity`` Material icon for in-flight
        uploads. Returns None when detection itself fails (we can't decide).
        """
        try:
            return await self.page.evaluate("""() => {
                for (const el of document.querySelectorAll('*')) {
                    const t = (el.textContent || '').trim();
                    if (t === 'progress_activity' || t.startsWith('progress_activity')) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                    }
                }
                return false;
            }""")
        except Exception:
            return None

    async def _dismiss_rights_notice(self) -> bool:
        """Click "I agree" on Flow's "necessary rights" Notice dialog if it
        is visible.

        Flow throws up a modal during image upload reminding the user they
        must have rights to the content ("Make sure you have the necessary
        rights... I agree / Cancel"). If we don't dismiss it, the upload
        stalls forever — set_input_files succeeds but Flow never starts
        ingesting the bytes, and _wait_for_frame_upload eventually times
        out. Call this before/after each upload and inside the wait loop.

        Returns True if the dialog was found and dismissed, False otherwise.
        Safe to call when the dialog isn't present — it's a no-op then.
        """
        try:
            return await self.page.evaluate("""() => {
                // Find a visible "Notice"-style modal. We don't rely on
                // class names (Flow ships obfuscated CSS); instead we
                // detect a visible button whose trimmed text matches an
                // accept keyword, AND whose dialog contains a "Notice"-ish
                // heading or "rights"/"Prohibited Use Policy" body text.
                const acceptWords = [
                    "i agree", "agree", "i understand", "accept",
                    "я согласен", "согласен", "принять", "понятно",
                    "tôi đồng ý", "đồng ý"
                ];
                const bodyHints = [
                    "necessary rights", "prohibited use policy",
                    "необходимые права", "rights", "права", "quyền",
                    "video", "media", "tải", "upload", "agree", "do not show"
                ];

                const isVisible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) return false;
                    const st = getComputedStyle(el);
                    if (st.visibility === "hidden" || st.display === "none"
                        || parseFloat(st.opacity || "1") < 0.1) return false;
                    return true;
                };

                // Look for an "I agree"-style button inside a visible dialog
                // that mentions the rights/Notice copy. We don't grab any
                // global "agree" button (would mis-click on welcome screens).
                const buttons = [...document.querySelectorAll(
                    'button, [role="button"]'
                )];
                for (const btn of buttons) {
                    if (!isVisible(btn)) continue;
                    const txt = (btn.textContent || "").trim().toLowerCase();
                    if (!acceptWords.some(w => txt === w || txt.includes(w))) continue;

                    // Walk up looking for an ancestor with the rights copy.
                    let host = btn.parentElement;
                    let matched = false;
                    for (let i = 0; i < 12 && host; i++) {
                        const t = (host.textContent || "").toLowerCase();
                        if (bodyHints.some(h => t.includes(h))) {
                            matched = true; break;
                        }
                        host = host.parentElement;
                    }
                    if (!matched) continue;

                    btn.click();
                    return true;
                }
                return false;
            }""")
        except Exception as e:
            logger.debug("[FlowVideoClient] _dismiss_rights_notice failed: %s", e)
            return False

    async def _switch_to_ingredients_tab(self) -> bool:
        """Open config panel and switch to Ingredients (R2V) tab.

        Panel stays OPEN after this call — add_2 button lives inside the panel.
        Call _close_config_panel() after all reference images are uploaded.

        Reuses ``_click_tab`` which navigates by role+name and verifies
        ``aria-selected`` — a previous evaluate-only implementation could
        report success while clicking a non-tab element, leaving the panel
        closed (observed in the field as "tab clicked, panel empty").
        """
        # The Ingredients tab is the "chrome_extension" material-icon tab.
        # Some Flow builds render the tab label as plain "Ingredients" text
        # instead of the icon glyph, so we try both.
        for attempt in range(3):
            panel_opened = await self._open_config_panel()
            if not panel_opened:
                logger.warning(
                    "[FlowVideoClient] Could not open config panel for "
                    "Ingredients tab (attempt %d/3)", attempt + 1,
                )
                await asyncio.sleep(0.5)
                continue

            for label in ("chrome_extension", "Ingredients"):
                result = await self._click_tab(label)
                if result is None:
                    continue
                # Verify the tab is now actually selected. If the click
                # missed (panel closed before processing, race with re-render),
                # aria-selected won't be true and we retry.
                selected = await self.page.evaluate("""(target) => {
                    for (const tab of document.querySelectorAll('[role="tab"]')) {
                        const text = (tab.textContent || '').trim();
                        if (text.includes(target)) {
                            return tab.getAttribute('aria-selected') === 'true';
                        }
                    }
                    return false;
                }""", label)
                if selected:
                    logger.info(
                        "[FlowVideoClient] Switched to Ingredients tab (%s, %s)",
                        label, result,
                    )
                    # Frames cache is stale now — Ingredients is the active
                    # tab. Next I2V generation must re-activate Frames or
                    # the slot elements won't be visible.
                    self._frames_mode_active = False
                    # Give the panel content time to render before the
                    # caller starts looking for add_2.
                    await asyncio.sleep(0.8)
                    return True
                logger.debug(
                    "[FlowVideoClient] Ingredients tab click reported '%s' "
                    "but aria-selected=false (label=%s)", result, label,
                )

            logger.warning(
                "[FlowVideoClient] Ingredients tab not selected after attempt %d/3",
                attempt + 1,
            )
            await asyncio.sleep(0.5)

        logger.warning(
            "[FlowVideoClient] Could not switch to Ingredients tab after 3 attempts"
        )
        return False

    async def _add_reference_image(self, path: Path) -> bool:
        """Add one reference image in Ingredients (R2V) mode using the new media library path.

        Clicks the add button, triggers file chooser via the upload icon,
        then selects the uploaded file from the picker option.

        Args:
            path: Local path to the reference image.
        """
        # Step 1: Upload the file directly to the project's media library
        uploaded = await self._upload_file_to_project(path)
        if not uploaded:
            logger.warning("[FlowVideoClient] Direct file upload to project failed for reference image: %s", path.name)
            return False

        # Step 2: Click the add reference button ("add_2" button inside Ingredients panel)
        add_clicked = False
        for attempt in range(20):
            add_clicked = await self.page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button, [role="button"]')) {
                    if ((btn.textContent || '').includes('add_2')) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 0) { btn.click(); return true; }
                    }
                }
                return false;
            }""")
            if add_clicked:
                break
            await asyncio.sleep(0.25)

        if not add_clicked:
            # Fallback: check if we can click role="button" with name="add_2 Tạo" or similar
            for label in [
                re.compile(r"add_2\s+Tạo", re.I),
                re.compile(r"add_2\s+Create", re.I),
                re.compile(r"Tạo", re.I),
                re.compile(r"add_2", re.I),
            ]:
                try:
                    loc = self.page.locator('button, [role="button"]').filter(has_text=label).first
                    if await loc.is_visible(timeout=1000):
                        await loc.click()
                        add_clicked = True
                        break
                except Exception:
                    continue

        if not add_clicked:
            logger.warning("[FlowVideoClient] Could not find add reference image button (add_2)")
            return False

        await asyncio.sleep(1.0)

        # Step 3: Google picker opens. Select the uploaded file from the picker
        option_regex = re.compile(re.escape(path.stem), re.I)
        logger.info("[FlowVideoClient] Selecting %s from Google media picker (matching stem: %s) for reference...", path.name, path.stem)
        
        option_clicked = False
        for attempt in range(15):
            try:
                # Try role="option" first
                loc = self.page.get_by_role("option", name=option_regex)
                if await loc.count() > 0:
                    await loc.first.click()
                    option_clicked = True
                    break

                # Try generic elements
                loc = self.page.locator('[role="option"], button, [role="button"], div').filter(has_text=option_regex)
                if await loc.count() > 0:
                    count = await loc.count()
                    for i in range(count):
                        candidate = loc.nth(i)
                        if await candidate.is_visible():
                            await candidate.click()
                            option_clicked = True
                            break
                    if option_clicked:
                        break
            except Exception:
                pass
            await asyncio.sleep(0.5)

        if not option_clicked:
            logger.warning("[FlowVideoClient] Could not find or click media picker option for reference image %s", path.name)
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

        logger.info("[FlowVideoClient] Added reference image: %s", path.name)
        await asyncio.sleep(1.0)
        return True

    async def _click_more_vert(self, video_url: str | None = None) -> bool:
        """Hover over a specific video card to reveal more_vert, then click it.

        Resilient to slow card rendering and to lookups by ``video_url``
        (used by upscale, where the target card is not necessarily the
        first one in the list).
        """
        list_loc = self.page.locator('[data-testid="virtuoso-item-list"]')

        # 1) Wait until the virtuoso list has at least one rendered item.
        try:
            await list_loc.locator('> *').first.wait_for(state="attached", timeout=15000)
        except Exception as e:
            logger.warning("[FlowVideoClient] virtuoso list never populated: %s", e)
            return False

        # 2) Find target card coordinates. If video_url is given, prefer
        #    the card whose <video>/<source> src contains the GCS filename
        #    fragment of that URL. Otherwise hover the most recent (first)
        #    card. We use mouse.move to (cx, cy) instead of locator.hover
        #    so we control exactly which card surface receives the hover
        #    event (Flow only reveals more_vert on direct card hover).
        coords = await self.page.evaluate(
            """(urlHint) => {
                const list = document.querySelector('[data-testid="virtuoso-item-list"]');
                if (!list) return null;
                const items = [...list.children].filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                if (items.length === 0) return null;

                let target = items[0];
                if (urlHint) {
                    const fragment = (urlHint.split('/').pop() || '').split('?')[0];
                    if (fragment) {
                        for (const it of items) {
                            const srcs = [...it.querySelectorAll('video, source')]
                                .map(v => v.src || v.getAttribute('src') || '');
                            if (srcs.some(s => s.includes(fragment))) {
                                target = it;
                                break;
                            }
                        }
                    }
                }
                target.scrollIntoView({block: 'center'});
                const r = target.getBoundingClientRect();
                return { x: r.x + r.width / 2, y: r.y + Math.min(r.height / 2, 120) };
            }""",
            video_url,
        )
        if not coords:
            logger.warning("[FlowVideoClient] No card found in virtuoso list to hover")
            return False

        # 3) Move mouse onto the card and give the UI time to reveal
        #    the more_vert button. Try a few times — Flow occasionally
        #    re-renders right after navigation and the first hover gets
        #    swallowed.
        for attempt in range(3):
            try:
                await self.page.mouse.move(coords['x'], coords['y'])
                await asyncio.sleep(0.2)
                # Nudge by 1px so React notices the hover state change.
                await self.page.mouse.move(coords['x'] + 1, coords['y'] + 1)
            except Exception:
                pass
            await asyncio.sleep(0.8)

            btn = list_loc.locator('button').filter(has_text="more_vert").first
            try:
                await btn.wait_for(state="visible", timeout=2000)
                await btn.click(timeout=3000)
                await asyncio.sleep(0.5)
                return True
            except Exception:
                # Fallback: locate via raw DOM (some cards render the icon
                # inside a span instead of as button text).
                clicked = await self.page.evaluate(
                    """() => {
                        for (const b of document.querySelectorAll('[data-testid="virtuoso-item-list"] button')) {
                            const txt = (b.textContent || '').trim();
                            const rect = b.getBoundingClientRect();
                            if (rect.width === 0 || rect.height === 0) continue;
                            if (txt.includes('more_vert') || b.getAttribute('aria-label') === 'more_vert') {
                                b.click();
                                return true;
                            }
                        }
                        return false;
                    }"""
                )
                if clicked:
                    await asyncio.sleep(0.5)
                    return True

            logger.debug(
                "[FlowVideoClient] more_vert hover attempt %d/3 failed, retrying", attempt + 1
            )

        logger.warning("[FlowVideoClient] Could not find more_vert button after 3 attempts")
        return False

    async def _click_video_card(self, video_url: str | None = None) -> bool:
        """Click a video card to open its inline editor.

        If video_url is given, tries to match the card by URL fragment.
        Falls back to the most recent (first) video card in the list.
        """
        coords = await self.page.evaluate("""(urlHint) => {
            const list = document.querySelector('[data-testid="virtuoso-item-list"]');
            const root = list || document.body;

            // Try to match by video URL fragment (GCS filenames are unique enough)
            if (urlHint) {
                const fragment = urlHint.split('/').pop().split('?')[0];
                for (const v of root.querySelectorAll('video, source')) {
                    const src = v.src || v.getAttribute('src') || '';
                    if (src.includes(fragment)) {
                        let el = v;
                        while (el && el !== document.body) {
                            if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') {
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0) return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
                            }
                            el = el.parentElement;
                        }
                    }
                }
            }

            // Fallback: find all buttons in the list that wrap a video or have no text
            // (video card thumbnails are typically empty-text buttons)
            const btns = [...root.querySelectorAll('button')].filter(btn => {
                const text = (btn.textContent || '').trim();
                const rect = btn.getBoundingClientRect();
                return rect.width > 100 && rect.height > 60 && text === '';
            });
            if (btns.length > 0) {
                const rect = btns[0].getBoundingClientRect();
                return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
            }
            return null;
        }""", video_url)

        if not coords:
            logger.warning("[FlowVideoClient] Could not find video card to click")
            return False

        await self.page.mouse.click(coords['x'], coords['y'])
        await asyncio.sleep(1.0)
        return True

    async def _click_extend_button(self) -> bool:
        """Click the Extend button on the open video card panel.

        Identified by the keyboard_double_arrow_right Material Icon — locale-independent.
        """
        clicked = await self.page.evaluate("""() => {
            for (const btn of document.querySelectorAll('button, [role="button"]')) {
                if ((btn.textContent || '').includes('keyboard_double_arrow_right')) {
                    const rect = btn.getBoundingClientRect();
                    if (rect.width > 0) { btn.click(); return true; }
                }
            }
            return false;
        }""")
        if not clicked:
            logger.warning("[FlowVideoClient] Could not find Extend button (keyboard_double_arrow_right)")
        return bool(clicked)

    async def _select_model_in_panel(self, target_label: str) -> bool:
        """Select a model from any visible arrow_drop_down dropdown on screen.

        Unlike _select_video_model, not restricted to a specific y-range —
        works for both the config panel and the extend card panel.
        """
        model_btn = await self.page.evaluate("""() => {
            for (const btn of document.querySelectorAll('button')) {
                const text = (btn.textContent || '').trim();
                const rect = btn.getBoundingClientRect();
                if (text.includes('arrow_drop_down') && rect.width > 80 && rect.width < 400
                        && rect.height > 0) {
                    return { text, x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
                }
            }
            return null;
        }""")

        if not model_btn:
            return False

        if target_label in model_btn['text']:
            logger.info("[FlowVideoClient] Extend model '%s' already selected", target_label)
            return True

        await self.page.mouse.click(model_btn['x'], model_btn['y'])
        await asyncio.sleep(0.5)

        clicked = await self.page.evaluate("""(target) => {
            for (const item of document.querySelectorAll('[role="menuitem"]')) {
                if ((item.textContent || '').includes(target)) {
                    item.click();
                    return true;
                }
            }
            return false;
        }""", target_label)

        if not clicked:
            await self.page.keyboard.press("Escape")
        await asyncio.sleep(0.3)
        return bool(clicked)

    async def upscale_video(
        self,
        resolution: str = "1080p",
        video_url: str | None = None,
        output_dir: Path | None = None,
        timeout_seconds: int = 120,
    ) -> Path | None:
        """Download an upscaled version of a generated video.

        Real-Flow flow (matches what the user records via codegen):
          1. Click the video card to open the enlarged preview.
          2. In the preview toolbar, click the "Download" button — labeled
             with the ``download`` material-icon glyph plus a localized
             string (``Tải xuống`` / ``Скачать`` / ``Download`` …).
          3. The button opens a menu with one menuitem per available quality
             (``1K Kích thước gốc`` / ``1080p`` / ``4K …``). Click the one
             that matches the requested resolution and Playwright catches
             the resulting download.

        Args:
            resolution: ``"1080p"`` or ``"4k"`` (case-insensitive). For
                ``"1k"`` / ``"original"`` the base quality menuitem is used.
            video_url: GCS URL of the target video card. None = most recent.
            output_dir: Save directory. Defaults to ``self.output_dir``.
            timeout_seconds: Max wait for the download to start.
        """
        logger.info("[FlowVideoClient] Upscaling video to %s (via card context menu)", resolution)

        # 1) Hover over the card to reveal context menu and click the three-dots (more_vert) button
        opened = await self._click_more_vert(video_url)
        if not opened:
            logger.warning("[FlowVideoClient] Could not click more_vert on video card for upscale")
            await self._screenshot("err_upscale_more_vert")
            return None
        # Dropdown menu takes a moment to render
        await asyncio.sleep(1.0)
        await self._screenshot("upscale_01_menu_opened")

        # 2) Find the Download menuitem
        download_item = None
        # We try multiple selectors to find a menu item that contains the word "download" (case-insensitive)
        for selector in ['[role="menuitem"]', '[role="option"]', 'li', 'button', 'div']:
            loc = self.page.locator(selector).filter(has_text=re.compile(r"download", re.I))
            count = await loc.count()
            if count > 0:
                for i in range(count):
                    candidate = loc.nth(i)
                    if await candidate.is_visible():
                        download_item = candidate
                        break
            if download_item:
                break

        # Fallback to direct localized matching if "download" icon string was not matched
        if not download_item:
            for selector in ['[role="menuitem"]', '[role="option"]', 'li', 'button', 'div']:
                loc = self.page.locator(selector).filter(has_text=re.compile(r"tải xuống|download|скачать", re.I))
                count = await loc.count()
                if count > 0:
                    for i in range(count):
                        candidate = loc.nth(i)
                        if await candidate.is_visible():
                            download_item = candidate
                            break
                if download_item:
                    break

        if not download_item:
            logger.warning("[FlowVideoClient] Could not find Download menu item in context menu")
            await self._screenshot("err_upscale_no_download_item")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return None

        # Hover on the Download menu item to reveal the resolution submenu
        await download_item.hover()
        await asyncio.sleep(1.0)
        await self._screenshot("upscale_02_download_hovered")

        # 3) Click the resolution option and catch the download
        target = resolution.lower().replace(" ", "")
        out_dir = output_dir or self.output_dir or DOWNLOADS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        # Build a regex covering common menuitem variants.
        if target in ("1k", "original", "originalsize", "base"):
            label_re = re.compile(r"\b1k\b|original|kích thước gốc|оригинал", re.I)
        elif target in ("1080p", "1080", "fhd"):
            label_re = re.compile(r"1080|full hd|fhd", re.I)
        elif target in ("4k", "uhd"):
            label_re = re.compile(r"\b4k\b|uhd|ultra", re.I)
        elif target in ("720p", "720", "hd"):
            label_re = re.compile(r"720|\bhd\b", re.I)
        else:
            label_re = re.compile(re.escape(target), re.I)

        try:
            item = self.page.get_by_role("menuitem", name=label_re)
            count = await item.count()
            if count == 0:
                # Fallback: any role=menuitem visible whose normalized text contains the target.
                coords = await self.page.evaluate("""(target) => {
                    const norm = (s) => (s || '').toLowerCase().replace(/\\s+/g, '');
                    for (const item of document.querySelectorAll('[role="menuitem"], [role="option"], li, button, div')) {
                        const t = norm(item.textContent);
                        const rect = item.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        if (t.includes(target)) {
                            return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
                        }
                    }
                    return null;
                }""", target)
                if not coords:
                    menu_dump = await self.page.evaluate("""() =>
                        [...document.querySelectorAll('[role="menuitem"], [role="option"], li')]
                            .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                            .map(el => (el.textContent || '').trim().slice(0, 80))
                            .filter(t => t.length > 0)
                            .slice(0, 20)
                    """)
                    logger.warning(
                        "[FlowVideoClient] Resolution menuitem '%s' not found. "
                        "Visible menu items: %s", resolution, menu_dump,
                    )
                    await self._screenshot("err_upscale_res_not_found")
                    await self.page.keyboard.press("Escape")
                    return None

                await self._screenshot("upscale_03_clicking_coords")
                async with self.page.expect_download(timeout=timeout_seconds * 1000) as dl_info:
                    await self.page.mouse.click(coords['x'], coords['y'])
            else:
                # Click the first visible matching element
                target_item = None
                for i in range(count):
                    candidate = item.nth(i)
                    if await candidate.is_visible():
                        target_item = candidate
                        break
                if not target_item:
                    target_item = item.first

                await self._screenshot("upscale_03_clicking_item")
                async with self.page.expect_download(timeout=timeout_seconds * 1000) as dl_info:
                    await target_item.click()

            # Wait for download to start with periodic logging so the user knows it's alive!
            download = None
            start_time = time.monotonic()
            deadline = start_time + timeout_seconds
            
            while time.monotonic() < deadline:
                try:
                    # Wait for 10 seconds in this iteration
                    download = await asyncio.wait_for(asyncio.shield(dl_info.value), timeout=10.0)
                    break
                except asyncio.TimeoutError:
                    elapsed = time.monotonic() - start_time
                    logger.info(
                        "[FlowVideoClient] Still waiting for Google to finish upscaling and start the download... "
                        "(%.0fs elapsed, max timeout %.0fs)",
                        elapsed, timeout_seconds
                    )
                    await self._screenshot("upscale_waiting_progress")
            
            if not download:
                raise TimeoutError(f"Upscale download did not start within {timeout_seconds} seconds")

            filename = download.suggested_filename or f"upscaled_{resolution}.mp4"
            dest = out_dir / filename
            await download.save_as(str(dest))
            logger.info("[FlowVideoClient] Upscaled video saved: %s", dest)
            await self._screenshot("upscale_04_finished")
            return dest

        except Exception as e:
            logger.warning("[FlowVideoClient] Upscale download failed: %s", e)
            await self._screenshot("err_upscale_download_exception")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return None

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(3))
    async def extend_video(
        self,
        prompt: str,
        video_url: str | None = None,
        model: FlowVideoModel | None = None,
        timeout_seconds: int = 300,
    ) -> VideoGenerationResult:
        """Extend a previously generated video using the Extend feature.

        Clicks the video card, selects model, clicks the Extend button,
        fills the "What's next?" prompt, and submits.

        Args:
            prompt: Description of what should happen in the extended clip.
            video_url: GCS URL of the video to extend — used to find the right card.
                       If None, clicks the most recent video card in the project.
            model: Model to use for extension. Defaults to self.model.
            timeout_seconds: Max wait for the extended video (default 300s).
        """
        logger.info("[FlowVideoClient] Extending video: %s", prompt[:80])
        start_time = time.monotonic()

        await self._ensure_generation_page()

        # Click the video card to open the inline editor
        ok = await self._click_video_card(video_url)
        if not ok:
            return VideoGenerationResult(
                platform=Platform.FLOW,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                error="Could not find or click video card",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        # Optionally switch model in the card panel
        target_model = model or self.model
        target_label = VIDEO_MODEL_UI_LABELS[target_model]
        await self._select_model_in_panel(target_label)

        # Click the Extend button
        ok = await self._click_extend_button()
        if not ok:
            return VideoGenerationResult(
                platform=Platform.FLOW,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                error="Could not find Extend button",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )
        await asyncio.sleep(0.5)

        # Fill the extend prompt ("What's next?" area — new contenteditable after clicking Extend)
        filled = await self._fill_prompt(prompt)
        if not filled:
            return VideoGenerationResult(
                platform=Platform.FLOW,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                error="Could not find extend prompt input",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )
        await asyncio.sleep(0.3)

        existing_videos = await self._collect_existing_videos()
        captured_urls, listener = self._setup_video_listener()

        clicked = await self._click_submit()
        if not clicked:
            try:
                self.page.remove_listener('response', listener)
            except Exception:
                pass
            return VideoGenerationResult(
                platform=Platform.FLOW,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                error="Could not click submit for extend",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        logger.info("[FlowVideoClient] Extend submitted, waiting for video...")

        video_urls = await self._wait_for_video(
            captured_urls,
            existing_videos=existing_videos,
            timeout_seconds=timeout_seconds,
        )
        await asyncio.sleep(2)

        try:
            self.page.remove_listener('response', listener)
        except Exception:
            pass

        if not video_urls:
            dom_error = getattr(self, '_dom_error', None)
            error_msg = (
                f"Error card detected: {dom_error[:200]}"
                if dom_error
                else "No extended video generated (timeout)"
            )
            return VideoGenerationResult(
                platform=Platform.FLOW,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                error=error_msg,
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        video_url_result = video_urls[0]
        prompt_dir = self._make_prompt_dir(prompt)
        local_path, filename = await self.download_video(video_url_result, output_dir=prompt_dir)

        size_bytes = local_path.stat().st_size if local_path and local_path.exists() else None

        duration_ms = int((time.monotonic() - start_time) * 1000)
        logger.info("[FlowVideoClient] Extend complete in %dms", duration_ms)

        return VideoGenerationResult(
            platform=Platform.FLOW,
            prompt=prompt,
            video=GeneratedVideo(
                url=video_url_result,
                local_path=str(local_path) if local_path else None,
                filename=filename,
                size_bytes=size_bytes,
            ),
            status=GenerationStatus.COMPLETED,
            duration_ms=duration_ms,
            model=target_model.value,
            project_url=self.page.url if "/project/" in (self.page.url or "") else None,
        )

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(3))
    async def generate_video(
        self,
        prompt: str,
        start_image: Path | None = None,
        end_image: Path | None = None,
        reference_images: list[Path] | None = None,
        timeout_seconds: int = 300,
    ) -> VideoGenerationResult:
        """Generate videos via browser automation.

        Supports T2V (text only), I2V (start_image), and I2V_DUAL (start+end images).
        Mode is inferred automatically from the provided image arguments.

        Args:
            prompt: Text description of the video to generate.
            start_image: Local path to the starting frame image (I2V / I2V_DUAL).
            end_image: Local path to the ending frame image (I2V_DUAL only).
            timeout_seconds: Maximum wait time (default 300s).
        """
        mode = (
            "r2v" if reference_images
            else "i2v_dual" if (start_image and end_image)
            else "i2v" if start_image
            else "t2v"
        )
        logger.info("[FlowVideoClient] Generating video (%s): %s", mode, prompt[:80])
        start_time = time.monotonic()

        await self._ensure_generation_page()
        await self._screenshot("01_page_ready")
        await self._setup_video_config()
        await self._screenshot("02_config_applied")

        # R2V: switch to Ingredients tab and upload reference images
        if reference_images:
            ok = await self._switch_to_ingredients_tab()
            if not ok:
                await self._screenshot("err_ingredients_tab")
                return VideoGenerationResult(
                    platform=Platform.FLOW,
                    prompt=prompt,
                    status=GenerationStatus.FAILED,
                    error="Could not switch to Ingredients tab for R2V",
                    duration_ms=int((time.monotonic() - start_time) * 1000),
                )
            await asyncio.sleep(0.5)
            for img_path in reference_images:
                ok = await self._add_reference_image(img_path)
                if not ok:
                    await self.page.mouse.click(10, 10)  # close panel on failure
                    await self._screenshot("err_ref_image_upload")
                    return VideoGenerationResult(
                        platform=Platform.FLOW,
                        prompt=prompt,
                        status=GenerationStatus.FAILED,
                        error=f"Failed to upload reference image: {img_path.name}",
                        duration_ms=int((time.monotonic() - start_time) * 1000),
                    )
            # Close config panel after all reference images uploaded
            await self.page.mouse.click(10, 10)
            await asyncio.sleep(2.0)
            await self._screenshot("03_refs_uploaded")

        # Upload frame images before submitting (I2V / I2V_DUAL)
        if start_image:
            ok = await self._upload_frame_image("start", start_image)
            if not ok:
                await self._screenshot("err_start_image_upload")
                return VideoGenerationResult(
                    platform=Platform.FLOW,
                    prompt=prompt,
                    status=GenerationStatus.FAILED,
                    error="Failed to upload start frame image",
                    duration_ms=int((time.monotonic() - start_time) * 1000),
                )
            await self._screenshot("03_start_image_uploaded")
        if end_image:
            ok = await self._upload_frame_image("end", end_image)
            if not ok:
                await self._screenshot("err_end_image_upload")
                return VideoGenerationResult(
                    platform=Platform.FLOW,
                    prompt=prompt,
                    status=GenerationStatus.FAILED,
                    error="Failed to upload end frame image",
                    duration_ms=int((time.monotonic() - start_time) * 1000),
                )
            await self._screenshot("04_end_image_uploaded")

        # ── Re-verify orientation before submission ──
        # Image uploads / frames-mode / ingredients-tab interactions may
        # have re-opened the config panel and inadvertently changed the
        # orientation. Read the config button text and fix if needed.
        await self._verify_orientation()

        # Snapshot existing videos in DOM so we only detect NEW ones
        existing_videos = await self._collect_existing_videos()

        filled = await self._fill_prompt(prompt)
        if not filled:
            await self._screenshot("err_prompt_fill")
            return VideoGenerationResult(
                platform=Platform.FLOW,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                error="Could not find prompt input",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )
        await self._screenshot("05_prompt_filled")
        await asyncio.sleep(0.5)

        # Start listening for video URLs BEFORE submit
        captured_urls, listener = self._setup_video_listener()

        clicked = await self._click_submit()
        if not clicked:
            try:
                self.page.remove_listener('response', listener)
            except Exception:
                pass
            await self._screenshot("err_submit")
            return VideoGenerationResult(
                platform=Platform.FLOW,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                error="Could not find or click submit button",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )
        await self._screenshot("06_submitted")

        logger.info("[FlowVideoClient] Prompt submitted, waiting for video...")

        video_urls = await self._wait_for_video(
            captured_urls,
            existing_videos=existing_videos,
            timeout_seconds=timeout_seconds,
        )

        # Brief delay for any async URL extraction
        await asyncio.sleep(2)

        # Stop listening
        try:
            self.page.remove_listener('response', listener)
        except Exception:
            pass

        if not video_urls:
            dom_error = getattr(self, '_dom_error', None)
            error_msg = (
                f"Error card detected: {dom_error[:200]}"
                if dom_error
                else "No video generated (timeout)"
            )
            await self._screenshot("err_timeout_no_video")
            return VideoGenerationResult(
                platform=Platform.FLOW,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                error=error_msg,
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        await self._screenshot("07_video_ready")
        # Download the first video
        video_url = video_urls[0]
        logger.info("[FlowVideoClient] Downloading video: %s", video_url[:120])
        prompt_dir = self._make_prompt_dir(prompt)
        local_path, filename = await self.download_video(video_url, output_dir=prompt_dir)

        size_bytes = None
        if local_path and local_path.exists():
            size_bytes = local_path.stat().st_size

        video = GeneratedVideo(
            url=video_url,
            local_path=str(local_path) if local_path else None,
            filename=filename,
            size_bytes=size_bytes,
        )

        duration_ms = int((time.monotonic() - start_time) * 1000)
        logger.info(
            "[FlowVideoClient] Video complete in %dms (%s bytes)",
            duration_ms, size_bytes or "?",
        )

        cleanup_count = (
            len(reference_images) if reference_images
            else 2 if (start_image and end_image)
            else 1 if start_image
            else 0
        )
        if cleanup_count > 0:
            await self._cleanup_uploaded_items(cleanup_count)

        return VideoGenerationResult(
            platform=Platform.FLOW,
            prompt=prompt,
            video=video,
            status=GenerationStatus.COMPLETED,
            duration_ms=duration_ms,
            model=self.model.value,
            project_url=self.page.url if "/project/" in (self.page.url or "") else None,
        )
