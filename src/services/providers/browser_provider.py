"""Browser-path video-generation provider.

Runs video generation via the vendored ``browser2api`` fork, using
``BrowserSessionPool`` to reuse per-account Chrome processes with multi-tab
concurrency.

Scope
-----
Supported modes (activated when ``config.provider_mode == "browser"``):

* **T2V** — text-to-video
* **I2V** / **I2V_DUAL** — image-to-video with start frame (and optional end frame)
* **R2V** — reference-image-guided generation (Ingredients)
* **EXTEND** — extend a previously browser-generated video
* **UPSCALE** — upscale a previously browser-generated video to 1080p or 4K

Unsupported (still routed to HTTP provider): INSERT, REMOVE, RESHOOT.

EXTEND / UPSCALE are pinned to the account that generated the source video
and navigate to the source's ``project_url`` before acting — the ⋮ menu and
inline extend UI only work inside the project that owns the card.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ...core.config import BASE_DIR, TMP_DIR, config as app_config
from ...core.database import db
from ...core.logger import logger
from ...core.models import AccountDB, VideoDB, VideoMode, VideoStatus
from ..load_balancer import load_balancer
from ..storage import storage
from .browser_pool import get_pool
from .flow_error_sniffer import FlowErrorSniffer, FlowPublicError

# Imports from the vendored fork (path injection in providers/__init__.py)
from browser2api.platforms.flow import FlowVideoClient  # type: ignore
from browser2api.platforms.flow.enums import (  # type: ignore
    FlowCount,
    FlowOrientation,
    FlowVideoModel,
)
from browser2api.platforms.flow.exceptions import (  # type: ignore
    AccountDisabledError,
    FlowAuthError,
    LoginFailedError,
    SessionTokenNotFoundError,
)
from browser2api.types import GenerationStatus, Platform  # type: ignore


# Below this credit balance, paid (non-free_tier) accounts are forced
# onto the "lower priority" model so the remaining credits stretch
# further. Override via [browser].low_credit_threshold or env. Set to 0
# to disable the downgrade entirely.
_LOW_CREDIT_THRESHOLD: int = (
    int(app_config.get("browser", "low_credit_threshold", 100))
    if hasattr(app_config, "get")
    else 100
)

# Upscale (1080p / 4K) is legitimately slower than base generation —
# Flow renders the upscaled MP4 in the background and only triggers
# the browser download once the file is ready. The base generation
# timeout (5 minutes) is too tight for the 4K case in particular, so we
# use a separate, longer budget. Override via [browser].upscale_timeout.
_UPSCALE_TIMEOUT_SECONDS: int = (
    int(app_config.get("browser", "upscale_timeout", 1500))
    if hasattr(app_config, "get")
    else 1500
)

# Accounts pinned to ``status='relaxed_only'`` in the dashboard always
# generate on the lower-priority queue (Veo 3.1 - Lite [Lower) regardless
# of which model the user requested. That queue is slower by design, so
# this account class also gets a separate, longer generation timeout.
# Override via [browser].relaxed_only_timeout.
_RELAXED_ONLY_TIMEOUT_SECONDS: int = (
    int(app_config.get("browser", "relaxed_only_timeout", 1800))
    if hasattr(app_config, "get")
    else 1800
)


# ---------- model / orientation mapping ----------

_MODEL_MAP: dict[str, FlowVideoModel] = {
    "veo31_fast": FlowVideoModel.VEO_3_1_FAST,
    "veo31_pro": FlowVideoModel.VEO_3_1_QUALITY,
    "veo3_fast": FlowVideoModel.VEO_3_1_LITE,
    "veo3_pro": FlowVideoModel.VEO_3_1_QUALITY,
    "veo3_quality": FlowVideoModel.VEO_3_1_QUALITY,
    "veo2_fast": FlowVideoModel.VEO_3_1_LITE_LOWER,
    "omni_flash": FlowVideoModel.OMNI_FLASH,
    "omni-flash": FlowVideoModel.OMNI_FLASH,
}


def _map_model(internal_model: str) -> FlowVideoModel:
    m = _MODEL_MAP.get((internal_model or "").lower())
    if m is None:
        logger.warning(
            "[BrowserProvider] Unknown model %r, falling back to VEO_3_1_FAST",
            internal_model,
        )
        return FlowVideoModel.VEO_3_1_FAST
    return m


def _map_orientation(aspect_ratio: str) -> FlowOrientation:
    raw = aspect_ratio.value if hasattr(aspect_ratio, "value") else aspect_ratio
    return (
        FlowOrientation.PORTRAIT
        if str(raw).strip() == "9:16"
        else FlowOrientation.LANDSCAPE
    )


def _map_resolution(requested: Optional[str]) -> str:
    """Map VeoAPI requested_resolution to Flow UI's label ('1080p' or '4k')."""
    r = (requested or "").lower().strip()
    if "4" in r:
        return "4k"
    return "1080p"


# ---------- unknown-error classifier ----------
#
# We deliberately avoid emitting novel error_codes like
# ``browser_generation_failed`` / ``browser_extend_failed`` to external
# clients — they already have handling for the classic VeoAPI codes
# (see ``src/core/errors.py::VEO_ERROR_MAPPING``). This classifier
# maps an unknown browser-path exception to the closest already-known
# code so the client experience is identical between providers.

# Substrings that imply transient/network/throttle trouble — these map
# to ``RATE_LIMITED`` (which the API layer surfaces as a 429, the
# universal "retry later / high load" signal).
_TRANSIENT_ERROR_MARKERS: tuple[str, ...] = (
    "timeout", "timed out", "timedout",
    "connection", "econnreset", "econnrefused", "econnaborted",
    "proxy", "network", "net::err_",
    "overload", "throttl", "too many requests",
    "temporarily unavailable", "service unavailable",
    "502 ", "503 ", "504 ", "429 ",
    "reset by peer", "broken pipe",
    "target closed", "browser has been closed", "target page",
    "websocket", "protocol error",
)

# Substrings that imply Google/Flow couldn't produce a video for this
# specific input (bad image format, unsupported aspect, etc.). These
# map to ``INVALID_ARGUMENT`` (API: VALIDATION_INVALID_PARAMS, 400).
_INVALID_ARG_MARKERS: tuple[str, ...] = (
    "invalid", "unsupported", "bad request",
    "image too small", "image too large", "unreadable",
)


# Substrings that mean Flow's UI rendered the generic "something went
# wrong" failure card. Empirically this is almost always a content /
# safety-filter rejection that the sniffer missed (Google sometimes
# surfaces the failure only via the DOM, not via the JSON we sniff).
# Treat it like any other PUBLIC_ERROR_* — fail the video immediately
# with a content-rejection code, do NOT rotate accounts (won't help)
# and do NOT mask as RATE_LIMITED / GENERATION_FAILED (would mislead
# the user into retrying the same banned prompt).
_DOM_ERROR_CONTENT_MARKERS: tuple[str, ...] = (
    "error card detected",
    "something went wrong",
    "oops",
)


def _looks_like_content_rejection(message: str) -> bool:
    """True if the failure text matches Flow's generic 'something went
    wrong' card. See ``_DOM_ERROR_CONTENT_MARKERS`` for context."""
    if not message:
        return False
    lower = message.lower()
    return any(marker in lower for marker in _DOM_ERROR_CONTENT_MARKERS)


def _classify_unknown_error(exc: BaseException) -> tuple[str, str]:
    """Map a browser-path exception to a ``(error_code, error_message)``
    the client already understands.

    Priority:
      1. ``RATE_LIMITED`` — anything that smells like timeout / network /
         throttling. Surfaces as HTTP 429 to the client.
      2. ``INVALID_ARGUMENT`` — bad input signals.
      3. ``GENERATION_FAILED`` — generic fallback (HTTP 500).

    The original exception class + text goes into ``error_message`` so
    ops still have the diagnostic detail in logs / admin tooling.
    """
    name = exc.__class__.__name__
    text = str(exc) or name
    lower = f"{name} {text}".lower()

    if any(marker in lower for marker in _TRANSIENT_ERROR_MARKERS):
        return "RATE_LIMITED", text

    if any(marker in lower for marker in _INVALID_ARG_MARKERS):
        return "INVALID_ARGUMENT", text

    return "GENERATION_FAILED", text


# ---------- provider ----------

BROWSER_GENERATION_MODES = {
    VideoMode.T2V.value,
    VideoMode.I2V.value,
    VideoMode.I2V_DUAL.value,
    VideoMode.R2V.value,
}
# EXTEND + UPSCALE are mode-routed: a separate video record is created (by
# POST /extend or POST /upscale) with mode=extend or mode=upscale and
# continuation_video_id/original_video_id pointing at the source. The inline
# upscale at the end of _run_generation handles the DIFFERENT flow where
# ``requested_resolution`` was set on the base-generation request — those
# records never enter _process_postop.
BROWSER_POSTOP_MODES = {
    VideoMode.EXTEND.value,
    VideoMode.UPSCALE.value,
}
BROWSER_SUPPORTED_MODES = BROWSER_GENERATION_MODES | BROWSER_POSTOP_MODES

MAX_ACCOUNTS = 3
DEFAULT_TIMEOUT_SECONDS = 900

# Max wall-clock seconds to wait for a browser-ready account before giving up
# on placement in the current attempt. Matches the 10-minute pending start
# budget so videos do not bounce back out too aggressively during load spikes.
_NO_ACCOUNT_WAIT_SECONDS = 600
_NO_ACCOUNT_POLL_SECONDS = 10

# Auth-failure streak required before marking an account needs_reauth. A single
# FlowAuthError / SessionTokenNotFoundError / LoginFailedError can be a
# transient Flow UI hiccup (timeout, selector race) — no reason to burn the
# account and wait an hour for the reauth monitor to pick it up. Only after
# this many consecutive auth failures on the same account do we actually
# mark it needs_reauth.
_AUTH_STREAK_THRESHOLD = 3

# AccountDisabled streak before actually flagging status=banned. First hit is
# treated as a possibly-transient Google UI misread → mark needs_reauth so
# immediate_reauth_monitor gives it one more shot before we write it off.
_DISABLED_STREAK_THRESHOLD = 2


class BrowserProvider:
    """Browser-driven video generation / extend / upscale via Flow UI."""

    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self._timeout = max(60, int(timeout_seconds))
        # In-memory per-account failure streaks. Reset on successful
        # generation or after the account's status flips to needs_reauth/banned.
        self._auth_fail_streak: dict[int, int] = {}
        self._disabled_streak: dict[int, int] = {}
        # Reserve account slots BEFORE acquire_tab() so queued videos do not
        # all choose the same account and then sit behind that account's tab
        # semaphore while other browser-ready accounts are idle.
        self._reserved_slots: dict[int, int] = {}
        self._slot_lock = asyncio.Lock()
        self._per_account_limit = max(1, int(app_config.browser_concurrency_per_account))

    async def process_video(self, video_id: str) -> None:
        """Full lifecycle: pick account → generate/extend/upscale → update DB.

        Per-video on-disk artifacts (screenshots, materialized inputs, local
        mp4 copy, failure dumps) are intentionally NOT deleted here — they
        are cleaned asynchronously by the ``sweep_browser_artifacts``
        background task once the artifact directory has been idle for
        ``browser_artifacts_max_age_hours`` (default 1h). This keeps a
        short forensic window open after failure / support requests.
        """
        video = await db.get_video(video_id)
        if not video:
            logger.error("[BrowserProvider] Video %s not found", video_id)
            return

        # Bail out immediately if the watchdog (fail_stale_pending_videos)
        # or an explicit cancel already flipped this row out of PENDING.
        # Without this check we'd still walk the full _process_generation
        # path: acquire the semaphore-equivalent (browser pool), sit in
        # _wait_for_browser_account for up to 5 minutes, open a Chrome tab,
        # only to discover at try_claim that the video is terminal.  To the
        # user that whole stretch looks like the failed/rate-limited video
        # is "still generating".
        if video.status != VideoStatus.PENDING.value:
            logger.info(
                "[BrowserProvider] Video %s is not PENDING (status=%s) — "
                "skipping process_video (watchdog or cancel already owns it)",
                video_id, video.status,
            )
            return

        if video.mode not in BROWSER_SUPPORTED_MODES:
            msg = f"BrowserProvider does not support mode={video.mode}"
            logger.warning("[BrowserProvider] %s — failing video %s", msg, video_id)
            await db.update_video(
                video_id,
                status=VideoStatus.FAILED.value,
                error_code="INVALID_ARGUMENT",
                error_message=msg,
            )
            return

        await db.update_video(
            video_id,
            provider="browser",
            progress_message=f"Selecting account (browser, mode={video.mode})...",
        )

        if video.mode in BROWSER_POSTOP_MODES:
            await self._process_postop(video)
        else:
            await self._process_generation(video)

    # ---------- fresh generation (T2V / I2V / I2V_DUAL / R2V) ----------

    async def _process_generation(self, video: VideoDB) -> None:
        tried_ids: set[int] = set()
        # Default to RATE_LIMITED when we exhaust accounts without ever
        # successfully starting a generation — "no accounts available" is
        # a load-shedding condition, not an internal error, so clients
        # should see a retry-later signal.
        last_code: str = "RATE_LIMITED"
        last_error: str = "No browser-ready accounts available"
        any_attempt_made = False
        # Whether we've already moved the video PENDING→GENERATING in this
        # _process_generation call. The atomic claim runs only on the FIRST
        # attempt; on retries with a rotated account we just update fields
        # (status is already GENERATING and trying to claim again would
        # spuriously think the video was killed externally).
        claimed_in_this_call = False

        for attempt in range(MAX_ACCOUNTS):
            # Wait for a browser-ready account to appear. Under real load the
            # pool can stay saturated for several minutes, so rotation
            # attempts get the same full placement budget as the initial
            # attempt instead of bailing after 30 seconds.
            wait_budget = _NO_ACCOUNT_WAIT_SECONDS
            account = await self._wait_for_browser_account(
                video=video,
                exclude_ids=tried_ids,
                max_wait_seconds=wait_budget,
            )
            if not account:
                if last_code != "PUBLIC_ERROR_UNUSUAL_ACTIVITY":
                    last_code = "RATE_LIMITED"
                    last_error = (
                        "No browser-ready accounts available"
                        if not any_attempt_made
                        else last_error
                    )
                break
            tried_ids.add(account.id)
            reserved_slot = True

            try:
                # Per-account relaxed → fast downgrade. If this account has
                # relaxed_disabled=True and the requested model is a relaxed
                # variant, swap to the fast key and update the DB so the
                # dashboard badge reflects the model that actually runs.
                from ..veo_client import RELAXED_TO_FAST_FALLBACK
                _fast_key = RELAXED_TO_FAST_FALLBACK.get(video.google_model_key or "")
                if _fast_key and getattr(account, "relaxed_disabled", False):
                    logger.info(
                        "[BrowserProvider] Relaxed disabled on %s — swap %s -> %s",
                        account.email, video.google_model_key, _fast_key,
                    )
                    await db.update_video(video.video_id, google_model_key=_fast_key)
                    video = await db.get_video(video.video_id) or video

                # Atomic claim only on the FIRST attempt. Subsequent
                # rotation attempts within this same _process_generation
                # call shouldn't re-claim — status is already GENERATING
                # from the first claim, and retrying try_claim would
                # spuriously think the video was killed.
                if not claimed_in_this_call:
                    claimed = await db.try_claim_video_for_generation(
                        video.video_id,
                        account.id,
                        progress_message=f"Generating via browser ({account.email})...",
                    )
                    if not claimed:
                        cur = await db.get_video(video.video_id)
                        cur_status = cur.status if cur else "<deleted>"
                        logger.info(
                            "[BrowserProvider] Video %s no longer PENDING "
                            "(status=%s) — aborting before generation",
                            video.video_id, cur_status,
                        )
                        return
                    claimed_in_this_call = True
                else:
                    # Rotation: just retarget the video to the new account.
                    await db.update_video(
                        video.video_id,
                        account_id=account.id,
                        progress_message=f"Generating via browser ({account.email})...",
                    )
                any_attempt_made = True
                await self._run_generation(video, account)
                # Success — reset all penalty counters for this account so
                # one old HTTP-path error doesn't follow it forever.
                await self._record_browser_success(account.id)
                return
            except FlowPublicError as e:
                # PUBLIC_ERROR_UNUSUAL_ACTIVITY = Google's anti-abuse flag on
                # this specific account, NOT a content rejection. Rotate to
                # the next browser-ready account; on 5 consecutive hits the
                # account goes to rest for 30 minutes via TokenManager.
                if e.error_code == "PUBLIC_ERROR_UNUSUAL_ACTIVITY":
                    logger.warning(
                        "[BrowserProvider] UNUSUAL_ACTIVITY on %s — rotating to next account",
                        account.email,
                    )
                    try:
                        from ...api.routes import get_token_manager
                        await get_token_manager().record_unusual_activity(account.id)
                    except Exception as _exc:  # pragma: no cover
                        logger.debug("[BrowserProvider] record_unusual_activity skipped: %s", _exc)
                    last_code = "PUBLIC_ERROR_UNUSUAL_ACTIVITY"
                    last_error = e.error_message or e.error_code
                    continue

                # Other PUBLIC_ERROR_* = real content/policy rejection.
                # Fail immediately with the same error_code the HTTP path
                # writes, don't rotate accounts.
                logger.warning(
                    "[BrowserProvider] Flow policy error on %s: %s=%s — failing video",
                    account.email, e.error_code, e.error_message,
                )
                await self._fail(video.video_id, e.error_code, e.error_message)
                return
            except AccountDisabledError as e:
                await self._handle_account_disabled(account, e)
                last_code = "RATE_LIMITED"
                last_error = f"Account disabled: {e}"
                continue
            except (LoginFailedError, SessionTokenNotFoundError, FlowAuthError) as e:
                await self._handle_auth_failure(account, e)
                last_code = "RATE_LIMITED"
                last_error = f"Auth required: {e}"
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_code, last_error = _classify_unknown_error(e)
                logger.exception(
                    "[BrowserProvider] Generation failed on %s: %s (mapped to %s)",
                    account.email, e, last_code,
                )
                continue
            finally:
                if reserved_slot:
                    await self._release_account_slot(account.id)

        # PUBLIC_ERROR_UNUSUAL_ACTIVITY must NEVER reach the user — it's
        # Google flagging a specific account, not anything the user did.
        # If every browser-ready account we tried hit UNUSUAL_ACTIVITY,
        # they're all flagged simultaneously (rare but happens after a
        # mass-reauth burst). Treat it like the cold-start case: keep
        # the video PENDING, clear excluded accounts, and retry after a
        # cool-off so flagged accounts have time to age out.
        if last_code == "PUBLIC_ERROR_UNUSUAL_ACTIVITY":
            # Stay well under the 10-min pending SLA enforced by
            # fail_stale_pending_videos — otherwise the watchdog kills the
            # video before the retry even fires. 60s leaves room for one
            # more rotation before the user-facing timeout.
            retry_delay = 60.0
            logger.warning(
                "[BrowserProvider] All %d account(s) tried for %s hit "
                "UNUSUAL_ACTIVITY — keeping PENDING and retrying in %.0fs "
                "(masking from user)",
                len(tried_ids), video.video_id, retry_delay,
            )
            await db.update_video(
                video.video_id,
                status=VideoStatus.PENDING.value,
                account_id=None,
                progress=0,
                progress_message="Waiting for accounts to cool down...",
                error_code=None,
                error_message=None,
            )

            async def _retry_after_unusual():
                try:
                    await asyncio.sleep(retry_delay)
                    cur = await db.get_video(video.video_id)
                    if cur and cur.status == VideoStatus.PENDING.value:
                        await self.process_video(video.video_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover
                    logger.error(
                        "[BrowserProvider] UNUSUAL_ACTIVITY retry failed for %s: %s",
                        video.video_id, exc,
                    )

            asyncio.create_task(_retry_after_unusual())
            return

        # If we never even started a generation attempt — i.e. no
        # browser-ready account was available within the wait budget — this
        # is a "pool not warm yet" condition, not a real failure. Common on
        # cold restart when 2-4 videos hit the queue before the Chrome pool
        # finishes launching. Bouncing the video to FAILED with RATE_LIMITED
        # destroys user work that would have succeeded a minute later. Keep
        # it PENDING and schedule a delayed re-process so a fresh attempt
        # runs after the pool warms up.
        if not any_attempt_made:
            retry_delay = 60.0
            logger.warning(
                "[BrowserProvider] No browser-ready account became available for "
                "%s after %d wait attempt(s) — keeping PENDING and retrying in %.0fs",
                video.video_id, MAX_ACCOUNTS, retry_delay,
            )
            await db.update_video(
                video.video_id,
                status=VideoStatus.PENDING.value,
                account_id=None,
                progress=0,
                progress_message="Waiting for browser pool to warm up...",
                error_code=None,
                error_message=None,
            )

            async def _retry_later():
                try:
                    await asyncio.sleep(retry_delay)
                    # Avoid double-processing if something else picked it up.
                    cur = await db.get_video(video.video_id)
                    if cur and cur.status == VideoStatus.PENDING.value:
                        await self.process_video(video.video_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # pragma: no cover
                    logger.error(
                        "[BrowserProvider] Delayed retry failed for %s: %s",
                        video.video_id, e,
                    )

            asyncio.create_task(_retry_later())
            return

        await db.update_video(
            video.video_id,
            status=VideoStatus.FAILED.value,
            error_code=last_code,
            error_message=last_error,
            progress_message=None,
        )

    async def _run_generation(self, video: VideoDB, account: AccountDB) -> None:
        """Run T2V / I2V / I2V_DUAL / R2V on the given account."""
        pool = get_pool()
        model = _map_model(video.model)
        # Per-account timeout (raised below for the relaxed_only path,
        # which uses the lower-priority queue and is slower by design).
        gen_timeout = self._timeout

        # Hard pin: accounts marked status='relaxed_only' in the dashboard
        # always generate on the lower-priority queue (Veo 3.1 - Lite
        # [Lower) regardless of what the user requested. The slow queue
        # also needs a longer timeout — we'd otherwise TIMEOUT every
        # video on this class of account.
        if getattr(account, "force_relaxed", False) or account.status == "relaxed_only":
            if model not in (FlowVideoModel.VEO_3_1_LITE_LOWER, FlowVideoModel.OMNI_FLASH):
                logger.info(
                    "[BrowserProvider] Account %s is relaxed_only — pinning "
                    "%s -> VEO_3_1_LITE_LOWER",
                    account.email, model.name,
                )
                model = FlowVideoModel.VEO_3_1_LITE_LOWER
            if _RELAXED_ONLY_TIMEOUT_SECONDS > gen_timeout:
                gen_timeout = _RELAXED_ONLY_TIMEOUT_SECONDS
        else:
            # Low-credit downgrade: paid accounts (NOT free_tier) whose
            # credit balance has dropped below the threshold are forced
            # to the "lower priority" model variant. Picking the regular
            # model on a low-credit paid account would quickly exhaust
            # the remaining balance; the lower-priority queue stretches
            # what's left.
            # free_tier accounts have their own routing (load_balancer)
            # and use the relaxed/lite path independently, so they're
            # skipped here.
            if (
                _LOW_CREDIT_THRESHOLD > 0
                and account.status != "free_tier"
                and account.credits is not None
                and account.credits < _LOW_CREDIT_THRESHOLD
            ):
                if model not in (FlowVideoModel.VEO_3_1_LITE_LOWER, FlowVideoModel.OMNI_FLASH):
                    logger.info(
                        "[BrowserProvider] Low credits on %s (%s left, "
                        "threshold=%s) — forcing %s -> VEO_3_1_LITE_LOWER "
                        "(lower-priority queue)",
                        account.email, account.credits, _LOW_CREDIT_THRESHOLD,
                        model.name,
                    )
                    model = FlowVideoModel.VEO_3_1_LITE_LOWER
                # Same reasoning — low-credit accounts get pushed onto
                # the slow queue, so use the longer timeout too.
                if _RELAXED_ONLY_TIMEOUT_SECONDS > gen_timeout:
                    gen_timeout = _RELAXED_ONLY_TIMEOUT_SECONDS
        orientation = _map_orientation(video.aspect_ratio)
        prompt = video.translated_prompt or video.prompt

        output_dir = TMP_DIR / "browser_videos" / video.video_id
        output_dir.mkdir(parents=True, exist_ok=True)

        start_path, end_path, ref_paths = await self._materialize_images(video, output_dir)

        # Guard: if mode requires images but storage returned nothing, fail fast
        # rather than silently downgrading to T2V.
        _needs_start = video.mode in (
            VideoMode.I2V.value, VideoMode.I2V_DUAL.value, VideoMode.R2V.value,
        )
        _needs_refs = video.mode == VideoMode.R2V.value
        if _needs_start and not start_path and not ref_paths:
            raise RuntimeError(
                f"Required start image not available for mode={video.mode} "
                f"(storage key={video.start_image_path!r}) — image may have been lost"
            )
        if _needs_refs and not ref_paths:
            raise RuntimeError(
                f"Reference images not available for R2V "
                f"(keys={video.reference_images_paths!r}) — images may have been lost"
            )

        logger.info(
            "[BrowserProvider] Running %s: video=%s account=%s model=%s orient=%s "
            "start=%s end=%s refs=%d",
            video.mode.upper(), video.video_id, account.email,
            model.name, orientation.name,
            start_path.name if start_path else "-",
            end_path.name if end_path else "-",
            len(ref_paths),
        )

        # Keep the tab open across generation + optional inline upscale — the
        # upscale's ⋮-menu only finds the card we just generated if we stay in
        # the same project/tab.
        want_upscale = (
            bool(video.requested_resolution)
            and (video.video_type or "normal") != "upscale"
        )

        async with (await pool.acquire_tab(account)) as (page, context):
            client = FlowVideoClient(
                page=page,
                context=context,
                output_dir=output_dir,
                model=model,
                orientation=orientation,
                count=FlowCount.X1,
                duration=video.requested_duration or video.duration,
            )
            client._screenshots_dir = BASE_DIR / "logs" / "browser_screenshots" / video.video_id
            # Race generation against a Flow-error sniffer so that a
            # content-policy / safety-filter rejection (PUBLIC_ERROR_*) —
            # the same signal HTTP path parses out of the JSON — aborts
            # immediately instead of waiting for the full timeout.
            result = await self._run_with_sniffer(
                context=context,
                tag=f"gen:{video.video_id[:8]}",
                video_id=video.video_id,
                coro=client.generate_video(
                    prompt=prompt,
                    start_image=start_path,
                    end_image=end_path,
                    reference_images=ref_paths or None,
                    timeout_seconds=gen_timeout,
                ),
            )

            if result.status != GenerationStatus.COMPLETED or result.video is None:
                err = result.error or "Unknown failure"
                # If Flow showed the generic DOM failure card and the sniffer
                # didn't catch a specific PUBLIC_ERROR_*, this is almost
                # always a content/safety rejection — surface it as such
                # rather than as a generic / transient failure.
                if _looks_like_content_rejection(err):
                    raise FlowPublicError(
                        "PUBLIC_ERROR_SAFETY_FILTER",
                        "The prompt or input image was rejected by Google's "
                        "safety filter. Try rephrasing the prompt or using a "
                        "different image.",
                    )
                raise RuntimeError(
                    f"Flow generation failed: status={result.status} error={err}"
                )

            # Push the downloaded MP4 to app storage so the download endpoint
            # can serve it. Flow's GCS URL isn't a stable public URL and gets
            # mangled if written as-is into the download_url column.
            base_download_url = await self._persist_to_storage(
                video.video_id, result.video.local_path,
            )
            base_size = result.video.size_bytes
            project_url = result.project_url
            logger.info(
                "[BrowserProvider] Base video ready: video=%s key=%s project=%s want_upscale=%s",
                video.video_id, base_download_url, project_url, want_upscale,
            )

            upscale_download_url: Optional[str] = None
            upscale_size: Optional[int] = None
            upscale_error: Optional[str] = None
            if want_upscale:
                await db.update_video(
                    video.video_id,
                    progress=80,
                    progress_message=f"Upscaling to {video.requested_resolution}...",
                )
                try:
                    upscale_download_url, upscale_size = await self._inline_upscale(
                        client=client,
                        video_id=video.video_id,
                        resolution=_map_resolution(video.requested_resolution),
                        target_video_url=result.video.url,
                        output_dir=output_dir,
                    )
                except Exception as e:
                    upscale_error = f"{type(e).__name__}: {e}"
                    logger.warning(
                        "[BrowserProvider] Inline upscale failed for %s: %s — keeping base video",
                        video.video_id, upscale_error,
                    )

        # Persist final state. If upscale succeeded, swap URLs to the stored
        # upscaled file; otherwise keep base video + surface the error.
        if upscale_download_url:
            await db.update_video(
                video.video_id,
                status=VideoStatus.COMPLETED.value,
                download_url=upscale_download_url,
                video_url=upscale_download_url,
                project_url=project_url,
                provider="browser",
                video_type="upscale",
                file_size=upscale_size,
                completed_at=datetime.utcnow(),
                progress=100,
                progress_message=None,
            )
        else:
            await db.update_video(
                video.video_id,
                status=VideoStatus.COMPLETED.value,
                download_url=base_download_url,
                video_url=base_download_url,
                project_url=project_url,
                provider="browser",
                file_size=base_size,
                completed_at=datetime.utcnow(),
                progress=100,
                progress_message=None,
                error_message=(
                    f"Upscale to {video.requested_resolution} failed: {upscale_error}"
                    if upscale_error else None
                ),
            )

    # ---------- post-op (EXTEND / UPSCALE) ----------

    async def _process_postop(self, video: VideoDB) -> None:
        """Extend or upscale pinned to the source video's account + project."""
        # Validation failures map to INVALID_ARGUMENT (client-side, 400);
        # availability failures map to RATE_LIMITED (retry-later, 429) —
        # keeps the error_code space the same as the HTTP path.
        src_id = video.continuation_video_id or video.original_video_id
        if not src_id:
            await self._fail(video.video_id, "INVALID_ARGUMENT",
                             f"{video.mode} requires continuation_video_id / original_video_id")
            return

        src = await db.get_video(src_id)
        if not src:
            await self._fail(video.video_id, "INVALID_ARGUMENT",
                             f"Source video {src_id} not found")
            return

        if src.provider != "browser" or not src.project_url:
            await self._fail(video.video_id, "INVALID_ARGUMENT",
                             f"Source {src_id} was not generated via browser "
                             f"(provider={src.provider}, project_url={src.project_url}) — "
                             f"browser {video.mode} requires a browser-generated source")
            return

        if not src.account_id:
            await self._fail(video.video_id, "INVALID_ARGUMENT",
                             f"Source {src_id} has no account_id")
            return

        account = await db.get_account(src.account_id)
        if not account or not account.is_active or not account.browser_ready:
            await self._fail(video.video_id, "RATE_LIMITED",
                             f"Source account {src.account_id} unavailable "
                             f"(active={account.is_active if account else '?'}, "
                             f"browser_ready={account.browser_ready if account else '?'})")
            return

        reserved = await self._wait_for_specific_account_slot(
            account=account,
            video_id=video.video_id,
            progress_message=f"Waiting for source account {account.email}...",
            max_wait_seconds=_NO_ACCOUNT_WAIT_SECONDS,
        )
        if not reserved:
            await db.update_video(
                video.video_id,
                status=VideoStatus.PENDING.value,
                account_id=None,
                progress=0,
                progress_message=f"Waiting for source account {account.email} to free up...",
                error_code=None,
                error_message=None,
            )

            async def _retry_postop_later():
                try:
                    await asyncio.sleep(30.0)
                    cur = await db.get_video(video.video_id)
                    if cur and cur.status == VideoStatus.PENDING.value:
                        await self.process_video(video.video_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover
                    logger.error(
                        "[BrowserProvider] source-account retry failed for %s: %s",
                        video.video_id, exc,
                    )

            asyncio.create_task(_retry_postop_later())
            return

        try:
            # Same atomic-claim guard as _process_generation: if the
            # watchdog already failed this video for being stuck in
            # PENDING, don't resurrect it.
            claimed = await db.try_claim_video_for_generation(
                video.video_id,
                account.id,
                progress_message=f"{video.mode} via browser ({account.email})...",
            )
            if not claimed:
                cur = await db.get_video(video.video_id)
                cur_status = cur.status if cur else "<deleted>"
                logger.info(
                    "[BrowserProvider] %s video %s no longer PENDING "
                    "(status=%s) — aborting postop", video.mode,
                    video.video_id, cur_status,
                )
                return
            # EXTEND and standalone UPSCALE are mode-routed through
            # _process_postop. The other upscale path — base generation with
            # ``requested_resolution`` set — runs inline at the end of
            # _run_generation and never reaches this branch.
            if video.mode == VideoMode.UPSCALE.value:
                await self._run_standalone_upscale(video, src, account)
            else:
                await self._run_extend(video, src, account)
            # Success — reset penalty counters for this account.
            await self._record_browser_success(account.id)
        except FlowPublicError as e:
            # extend/upscale is pinned to the source account — we can't
            # rotate. Still record the streak so a flagged account goes to
            # rest after 5 consecutive UNUSUAL_ACTIVITY hits.
            if e.error_code == "PUBLIC_ERROR_UNUSUAL_ACTIVITY":
                try:
                    from ...api.routes import get_token_manager
                    await get_token_manager().record_unusual_activity(account.id)
                except Exception:
                    pass
                # Source-pinned ops can't rotate, but the user must NEVER
                # see UNUSUAL_ACTIVITY. Re-queue PENDING and retry after
                # the account has cooled — the same account becomes
                # eligible again once Google's heuristics relax. Keep the
                # delay under the 10-min pending SLA so the retry actually
                # fires before fail_stale_pending_videos cuts it off.
                retry_delay = 60.0
                logger.warning(
                    "[BrowserProvider] UNUSUAL_ACTIVITY on source-pinned %s "
                    "(%s) for %s — keeping PENDING and retrying in %.0fs "
                    "(masking from user)",
                    account.email, video.mode, video.video_id, retry_delay,
                )
                await db.update_video(
                    video.video_id,
                    status=VideoStatus.PENDING.value,
                    progress=0,
                    progress_message="Waiting for account to cool down...",
                    error_code=None,
                    error_message=None,
                )

                async def _retry_postop_after_unusual():
                    try:
                        await asyncio.sleep(retry_delay)
                        cur = await db.get_video(video.video_id)
                        if cur and cur.status == VideoStatus.PENDING.value:
                            await self.process_video(video.video_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # pragma: no cover
                        logger.error(
                            "[BrowserProvider] postop UNUSUAL_ACTIVITY retry "
                            "failed for %s: %s", video.video_id, exc,
                        )

                asyncio.create_task(_retry_postop_after_unusual())
                return

            logger.warning(
                "[BrowserProvider] Flow policy error on %s (%s): %s=%s",
                account.email, video.mode, e.error_code, e.error_message,
            )
            await self._fail(video.video_id, e.error_code, e.error_message)
        except AccountDisabledError as e:
            await self._handle_account_disabled(account, e)
            # Source account went away — to the client this is retry-later,
            # not an input problem.
            await self._fail(video.video_id, "RATE_LIMITED", f"Account disabled: {e}")
        except (LoginFailedError, SessionTokenNotFoundError, FlowAuthError) as e:
            await self._handle_auth_failure(account, e)
            await self._fail(video.video_id, "RATE_LIMITED", f"Auth required: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            code, msg = _classify_unknown_error(e)
            logger.exception(
                "[BrowserProvider] %s failed on %s: %s (mapped to %s)",
                video.mode, account.email, e, code,
            )
            await self._fail(video.video_id, code, msg)
        finally:
            await self._release_account_slot(account.id)

    async def _run_extend(
        self, video: VideoDB, src: VideoDB, account: AccountDB,
    ) -> None:
        pool = get_pool()
        model = _map_model(video.model)
        gen_timeout = self._timeout

        # Hard pin: relaxed_only accounts always extend on the lower-
        # priority queue with the longer timeout.
        if getattr(account, "force_relaxed", False) or account.status == "relaxed_only":
            if model not in (FlowVideoModel.VEO_3_1_LITE_LOWER, FlowVideoModel.OMNI_FLASH):
                logger.info(
                    "[BrowserProvider] Account %s is relaxed_only — pinning "
                    "extend %s -> VEO_3_1_LITE_LOWER",
                    account.email, model.name,
                )
                model = FlowVideoModel.VEO_3_1_LITE_LOWER
            if _RELAXED_ONLY_TIMEOUT_SECONDS > gen_timeout:
                gen_timeout = _RELAXED_ONLY_TIMEOUT_SECONDS
        elif (
            _LOW_CREDIT_THRESHOLD > 0
            and account.status != "free_tier"
            and account.credits is not None
            and account.credits < _LOW_CREDIT_THRESHOLD
        ):
            # Same low-credit downgrade as _run_generation — paid
            # accounts with low balance go to the lower-priority queue.
            if model not in (FlowVideoModel.VEO_3_1_LITE_LOWER, FlowVideoModel.OMNI_FLASH):
                logger.info(
                    "[BrowserProvider] Low credits on %s (%s left, threshold=%s) — "
                    "forcing extend %s -> VEO_3_1_LITE_LOWER",
                    account.email, account.credits, _LOW_CREDIT_THRESHOLD, model.name,
                )
                model = FlowVideoModel.VEO_3_1_LITE_LOWER
            if _RELAXED_ONLY_TIMEOUT_SECONDS > gen_timeout:
                gen_timeout = _RELAXED_ONLY_TIMEOUT_SECONDS
        orientation = _map_orientation(video.aspect_ratio or src.aspect_ratio)
        prompt = video.translated_prompt or video.prompt

        output_dir = TMP_DIR / "browser_videos" / video.video_id
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "[BrowserProvider] Extending video=%s (src=%s, project=%s) account=%s model=%s",
            video.video_id, src.video_id, src.project_url, account.email, model.name,
        )

        async with (await pool.acquire_tab(account)) as (page, context):
            await self._navigate_to_project(page, src.project_url)
            client = FlowVideoClient(
                page=page,
                context=context,
                output_dir=output_dir,
                model=model,
                orientation=orientation,
                count=FlowCount.X1,
            )
            client._screenshots_dir = BASE_DIR / "logs" / "browser_screenshots" / video.video_id
            result = await self._run_with_sniffer(
                context=context,
                tag=f"extend:{video.video_id[:8]}",
                video_id=video.video_id,
                coro=client.extend_video(
                    prompt=prompt,
                    video_url=src.video_url or src.download_url,
                    model=model,
                    timeout_seconds=gen_timeout,
                ),
            )

        if result.status != GenerationStatus.COMPLETED or result.video is None:
            err = result.error or "Unknown failure"
            if _looks_like_content_rejection(err):
                raise FlowPublicError(
                    "PUBLIC_ERROR_SAFETY_FILTER",
                    "The prompt was rejected by Google's safety filter. "
                    "Try rephrasing.",
                )
            raise RuntimeError(
                f"Flow extend failed: status={result.status} error={err}"
            )

        download_url = await self._persist_to_storage(
            video.video_id, result.video.local_path,
        )
        await db.update_video(
            video.video_id,
            status=VideoStatus.COMPLETED.value,
            download_url=download_url,
            video_url=download_url,
            project_url=result.project_url or src.project_url,
            provider="browser",
            file_size=result.video.size_bytes,
            completed_at=datetime.utcnow(),
            progress=100,
            progress_message=None,
        )
        logger.info(
            "[BrowserProvider] Extend ready: video=%s key=%s",
            video.video_id, download_url,
        )

    async def _run_standalone_upscale(
        self, video: VideoDB, src: VideoDB, account: AccountDB,
    ) -> None:
        """Upscale a previously browser-generated video via a fresh tab.

        Unlike the inline upscale (which runs in the same tab that just
        produced the source card), this navigates to the source's
        ``project_url`` so the ⋮-menu / card selectors can locate the card
        by its Flow URL.
        """
        pool = get_pool()
        # Resolution may come in on requested_resolution (POST /upscale) or
        # be implied by google_model_key like ``veo_3_1_upsampler_4k``.
        requested = video.requested_resolution
        if not requested and "4k" in (video.google_model_key or "").lower():
            requested = "4k"
        resolution = _map_resolution(requested)

        output_dir = TMP_DIR / "browser_videos" / video.video_id
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "[BrowserProvider] Standalone upscale: video=%s (src=%s, project=%s) "
            "account=%s resolution=%s",
            video.video_id, src.video_id, src.project_url, account.email, resolution,
        )

        target_url = src.video_url or src.download_url

        async with (await pool.acquire_tab(account)) as (page, context):
            await self._navigate_to_project(page, src.project_url)
            client = FlowVideoClient(
                page=page,
                context=context,
                output_dir=output_dir,
                model=_map_model(video.model or src.model),
                orientation=_map_orientation(video.aspect_ratio or src.aspect_ratio),
                count=FlowCount.X1,
            )
            client._screenshots_dir = BASE_DIR / "logs" / "browser_screenshots" / video.video_id
            downloaded: Optional[Path] = await self._run_with_sniffer(
                context=context,
                tag=f"upscale:{video.video_id[:8]}",
                coro=client.upscale_video(
                    resolution=resolution,
                    video_url=target_url,
                    output_dir=output_dir,
                    timeout_seconds=_UPSCALE_TIMEOUT_SECONDS,
                ),
            )

        if not downloaded or not downloaded.exists():
            raise RuntimeError(
                f"Standalone upscale returned no file (resolution={resolution})"
            )

        size = downloaded.stat().st_size
        download_url = await self._persist_to_storage(video.video_id, str(downloaded))
        await db.update_video(
            video.video_id,
            status=VideoStatus.COMPLETED.value,
            download_url=download_url,
            video_url=download_url,
            project_url=src.project_url,
            provider="browser",
            video_type="upscale",
            file_size=size,
            completed_at=datetime.utcnow(),
            progress=100,
            progress_message=None,
        )
        logger.info(
            "[BrowserProvider] Standalone upscale ready: video=%s resolution=%s size=%d key=%s",
            video.video_id, resolution, size, download_url,
        )

    async def _inline_upscale(
        self,
        client: FlowVideoClient,
        video_id: str,
        resolution: str,
        target_video_url: Optional[str],
        output_dir: Path,
    ) -> tuple[str, int]:
        """Run upscale in the current tab against the just-generated card.

        Returns ``(download_url, size_bytes)``. Raises on failure.
        """
        logger.info(
            "[BrowserProvider] Inline upscale: video=%s to %s",
            video_id, resolution,
        )
        downloaded: Optional[Path] = await self._run_with_sniffer(
            context=client.context,
            tag=f"upscale:{video_id[:8]}",
            coro=client.upscale_video(
                resolution=resolution,
                video_url=target_video_url,
                output_dir=output_dir,
                timeout_seconds=_UPSCALE_TIMEOUT_SECONDS,
            ),
        )
        if not downloaded or not downloaded.exists():
            raise RuntimeError(f"Upscale returned no file (resolution={resolution})")

        size = downloaded.stat().st_size
        download_url = await self._persist_to_storage(video_id, str(downloaded))
        logger.info(
            "[BrowserProvider] Inline upscale ready: video=%s resolution=%s size=%d key=%s",
            video_id, resolution, size, download_url,
        )
        return download_url, size

    async def _run_with_sniffer(
        self, *, context, tag: str, coro, video_id: Optional[str] = None,
    ):
        """Run ``coro`` (a generate/extend/upscale call) while a
        ``FlowErrorSniffer`` watches the network. If a Flow PUBLIC_ERROR /
        filter response arrives first, cancel ``coro`` and raise
        :class:`FlowPublicError` so the caller can fail the video with the
        same ``error_code`` the HTTP path uses.

        Also inspects the sniffer AFTER the coroutine finishes — sometimes
        the Flow UI reports completion state (or times out with a generic
        "Session token refresh failed" kind of error) while the very last
        poll response already carried a PUBLIC_ERROR. In that case the
        sniffed error takes precedence.
        """
        # Persist the captured task_id (Google operation name) to the video
        # record as soon as the submit response is observed on the wire. This
        # lets the HTTP poller pick the video up after a service restart even
        # though the browser session is gone.
        async def _persist_task_id(op_name: str) -> None:
            if not video_id:
                return
            try:
                await db.update_video(video_id, task_id=op_name)
                logger.info(
                    "[BrowserProvider:%s] saved task_id=%s for HTTP fallback polling",
                    tag, op_name,
                )
            except Exception as exc:
                logger.warning(
                    "[BrowserProvider:%s] failed to persist task_id: %s", tag, exc,
                )

        sniffer = FlowErrorSniffer(
            context, tag=tag,
            on_operation_name=_persist_task_id if video_id else None,
        )
        sniffer.attach()
        op_task = asyncio.ensure_future(coro)
        fail_task = asyncio.ensure_future(sniffer.wait_for_failure())
        try:
            done, _pending = await asyncio.wait(
                {op_task, fail_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if fail_task in done and op_task not in done:
                op_task.cancel()
                try:
                    await op_task
                except (asyncio.CancelledError, Exception):
                    pass
                code, message = fail_task.result()
                raise FlowPublicError(code, message)

            # op_task finished first (successfully or with its own exception).
            # Give the sniffer a tiny window to see the terminal response
            # (it fires off in parallel with the DOM success signal).
            try:
                await asyncio.wait_for(asyncio.shield(fail_task), timeout=0.2)
            except (asyncio.TimeoutError, Exception):
                pass

            err = sniffer.peek_error()
            if err:
                # The operation returned but Flow itself reported a filter.
                # Prefer the sniffed error over a potentially-stale result.
                code, message = err
                raise FlowPublicError(code, message)

            # If the operation raised (DOM error card, timeout, runtime error)
            # OR returned a failure-shaped result, retroactively search ALL
            # stored network responses with the broader matcher. Without
            # this the caller would see a generic "Error card detected" /
            # "GENERATION_FAILED" while the JSON had the real PUBLIC_ERROR_*.
            op_exc: Optional[BaseException] = op_task.exception()
            if op_exc is not None:
                broad_err = sniffer.search_all_for_error()
                if broad_err:
                    code, message = broad_err
                    logger.info(
                        "[BrowserProvider:%s] op_task raised %s but network "
                        "carried %s — surfacing real error",
                        tag, type(op_exc).__name__, code,
                    )
                    raise FlowPublicError(code, message)
                # No PUBLIC_ERROR found in network — re-raise original
                raise op_exc

            result = op_task.result()
            _failed = False
            if hasattr(result, 'status'):
                _failed = result.status != GenerationStatus.COMPLETED
            elif hasattr(result, 'error') and result.error:
                _failed = True
            if _failed:
                broad_err = sniffer.search_all_for_error()
                if broad_err:
                    code, message = broad_err
                    raise FlowPublicError(code, message)

            return result
        finally:
            if not fail_task.done():
                fail_task.cancel()
                try:
                    await fail_task
                except (asyncio.CancelledError, Exception):
                    pass
            sniffer.detach()

    async def _persist_to_storage(
        self, video_id: str, local_path: Optional[str],
    ) -> str:
        """Upload a local MP4 to app storage and return the download_url field
        value the download endpoint expects (filename only — the endpoint
        prefixes ``videos/`` itself, matching the HTTP provider's convention).
        """
        if not local_path:
            raise RuntimeError("Flow result has no local_path — video wasn't downloaded")
        p = Path(local_path)
        if not p.exists():
            raise RuntimeError(f"Local video file missing: {local_path}")
        storage_key = f"videos/{video_id}.mp4"
        await storage.upload_file(storage_key, p, content_type="video/mp4")
        return f"{video_id}.mp4"

    # ---------- helpers ----------

    async def _navigate_to_project(self, page, project_url: Optional[str]) -> None:
        """Navigate to a Flow project and wait until video cards are visible.

        Virtuoso renders cards lazily — the list container appears quickly
        but individual items may take several seconds. We wait explicitly
        for at least one item inside ``[data-testid="virtuoso-item-list"]``
        (up to 20 s) so that ``_click_more_vert`` finds the hover target.
        """
        if not project_url:
            return
        try:
            await page.goto(project_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            logger.warning(
                "[BrowserProvider] Navigation to %s failed: %s — continuing on current page",
                project_url, e,
            )
            return

        # Wait for the Virtuoso list to contain at least one child item.
        # Falls back to a plain sleep if the selector never appears (e.g.
        # the project has no videos yet).
        try:
            await page.wait_for_selector(
                '[data-testid="virtuoso-item-list"] > *',
                timeout=20_000,
            )
            # Extra moment for the <video> thumbnail to attach inside the card.
            await asyncio.sleep(2)
            logger.info(
                "[BrowserProvider] Project page ready (video cards visible): %s",
                page.url,
            )
        except Exception:
            # List didn't appear in 20s — fall back to a fixed wait and hope
            # for the best; _click_more_vert has its own timeouts.
            logger.warning(
                "[BrowserProvider] Virtuoso list not visible after 20s on %s — "
                "proceeding anyway",
                project_url,
            )
            await asyncio.sleep(5)

    async def _materialize_images(
        self, video: VideoDB, output_dir: Path,
    ) -> tuple[Optional[Path], Optional[Path], list[Path]]:
        """Download start/end/reference images from storage to local files.

        browser2api expects ``pathlib.Path`` pointing at real files on disk;
        our storage layer keeps image bytes under a key (local fs or S3).
        """
        start = await self._download_image(video.start_image_path, output_dir, "start")
        end = await self._download_image(video.end_image_path, output_dir, "end")

        refs: list[Path] = []
        if video.reference_images_paths:
            try:
                keys = json.loads(video.reference_images_paths)
            except Exception:
                keys = []
            for i, key in enumerate(keys or []):
                p = await self._download_image(key, output_dir, f"ref{i}")
                if p:
                    if p.suffix.lower() == ".mp4":
                        try:
                            from ..ffmpeg_service import ffmpeg_service
                            processed_p = await ffmpeg_service.process_reference_video(p)
                            if processed_p != p:
                                try:
                                    p.unlink(missing_ok=True)
                                except Exception:
                                    pass
                                p = processed_p
                        except Exception as e:
                            logger.warning(
                                "[BrowserProvider] Failed to process reference video %s: %s",
                                p.name, e,
                            )
                    refs.append(p)
        return start, end, refs

    async def _download_image(
        self, storage_key: Optional[str], output_dir: Path, name: str,
    ) -> Optional[Path]:
        if not storage_key:
            return None
        try:
            data = await storage.download(storage_key)
        except Exception as e:
            logger.warning(
                "[BrowserProvider] Could not load image %s: %s", storage_key, e,
            )
            return None
        ext = Path(storage_key).suffix or ".jpg"
        dest = output_dir / f"{name}{ext}"
        dest.write_bytes(data)
        return dest

    async def _fail(self, video_id: str, code: str, message: str) -> None:
        logger.warning("[BrowserProvider] Failing %s: %s (%s)", video_id, code, message)
        await db.update_video(
            video_id,
            status=VideoStatus.FAILED.value,
            error_code=code,
            error_message=message,
            progress_message=None,
        )

    # ---------- account lifecycle helpers ----------

    async def _wait_for_browser_account(
        self,
        *,
        video: VideoDB,
        exclude_ids: set[int],
        max_wait_seconds: int,
    ) -> Optional[AccountDB]:
        """Poll ``load_balancer`` until a browser-ready account is available,
        or the wait budget runs out.

        Prevents the "instantly fail with RATE_LIMITED" behaviour right after
        ``systemctl restart`` / mass reauth, when the pool is still warming
        up but accounts are on their way. If ``exclude_ids`` already contains
        every browser-ready account, returns None immediately (waiting is
        pointless — those accounts just burned).
        """
        deadline = time.monotonic() + max(0, int(max_wait_seconds))
        announced_wait = False
        # Bump updated_at periodically so fail_stale_pending_videos sees
        # the video is being actively worked on. Without this the watchdog
        # would fail the video as "stale pending" right while we're inside
        # this wait — exact symptom users hit after a service restart
        # where the pool needs ~30s to launch Chrome.
        last_heartbeat = time.monotonic()
        _HEARTBEAT_INTERVAL = 60.0
        saturated_ids: set[int] = set()
        while True:
            account = await load_balancer.get_account(
                model_key=video.google_model_key,
                exclude_ids=exclude_ids | saturated_ids,
                ignore_shift=True,
                provider="browser",
            )
            if account:
                # Skip accounts whose browser_ready flag dropped between
                # get_active_accounts() and here (rare; reauth in flight).
                if not account.browser_ready:
                    logger.info(
                        "[BrowserProvider] Skipping %s (browser_ready=False)",
                        account.email,
                    )
                    exclude_ids.add(account.id)
                    continue
                if await self._try_reserve_account_slot(account.id):
                    return account
                saturated_ids.add(account.id)
                continue

            # No account. Stop waiting if every browser-ready account on file
            # is already in exclude_ids — further polling won't help.
            all_accounts = await db.get_active_accounts()
            ready_ids = {
                a.id for a in all_accounts
                if getattr(a, "browser_ready", False)
            }
            if ready_ids and ready_ids.issubset(exclude_ids):
                logger.info(
                    "[BrowserProvider] All %d browser-ready accounts already tried "
                    "for video %s — stopping wait",
                    len(ready_ids), video.video_id,
                )
                return None

            waiting_only_on_capacity = bool(ready_ids - exclude_ids) and (ready_ids - exclude_ids).issubset(saturated_ids)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "[BrowserProvider] Gave up waiting for browser-ready account "
                    "after %ds (video %s)",
                    max_wait_seconds, video.video_id,
                )
                return None

            if not announced_wait:
                announced_wait = True
                logger.info(
                    "[BrowserProvider] %s — waiting up to %ds (video %s)",
                    (
                        "All browser-ready accounts are saturated"
                        if waiting_only_on_capacity
                        else "No browser-ready accounts right now"
                    ),
                    max_wait_seconds,
                    video.video_id,
                )
                try:
                    await db.update_video(
                        video.video_id,
                        progress_message=(
                            "Waiting for a free browser account slot..."
                            if waiting_only_on_capacity
                            else "Waiting for a browser-ready account..."
                        ),
                    )
                except Exception:
                    pass

            # Heartbeat — bump updated_at every minute while we wait so
            # fail_stale_pending_videos doesn't kill a video the worker is
            # actively trying to place. The watchdog will still kill a
            # truly idle video (no _process_video task) after the SLA.
            if time.monotonic() - last_heartbeat >= _HEARTBEAT_INTERVAL:
                last_heartbeat = time.monotonic()
                try:
                    await db.update_video(
                        video.video_id,
                        progress_message=(
                            f"Waiting for a browser-ready account slot "
                            f"(elapsed {int(time.monotonic() - (deadline - max_wait_seconds))}s)..."
                        ),
                    )
                except Exception:
                    pass
                # Defensive: if the watchdog (or a cancel) flipped this row
                # out of PENDING despite the heartbeat — e.g. heartbeat
                # write got rolled back, or the row was already terminal
                # when we entered the wait — bail out instead of spinning
                # the full wait budget on a dead video.
                cur = await db.get_video(video.video_id)
                if cur and cur.status != VideoStatus.PENDING.value:
                    logger.info(
                        "[BrowserProvider] Video %s no longer PENDING "
                        "(status=%s) while waiting for account — aborting wait",
                        video.video_id, cur.status,
                    )
                    return None

            saturated_ids.clear()
            await asyncio.sleep(min(_NO_ACCOUNT_POLL_SECONDS, max(1.0, remaining)))

    async def _wait_for_specific_account_slot(
        self,
        *,
        account: AccountDB,
        video_id: str,
        progress_message: str,
        max_wait_seconds: int,
    ) -> bool:
        """Wait until a source-pinned account has a free browser slot."""
        deadline = time.monotonic() + max(0, int(max_wait_seconds))
        while True:
            if await self._try_reserve_account_slot(account.id):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await db.update_video(video_id, progress_message=progress_message)
            except Exception:
                pass
            await asyncio.sleep(min(10.0, max(1.0, remaining)))

    async def _try_reserve_account_slot(self, account_id: int) -> bool:
        """Reserve a browser generation slot for an account if capacity remains."""
        async with self._slot_lock:
            current = self._reserved_slots.get(account_id, 0)
            if current >= self._per_account_limit:
                return False
            self._reserved_slots[account_id] = current + 1
            return True

    async def _release_account_slot(self, account_id: int) -> None:
        """Release a previously reserved browser generation slot."""
        async with self._slot_lock:
            current = self._reserved_slots.get(account_id, 0)
            if current <= 1:
                self._reserved_slots.pop(account_id, None)
            else:
                self._reserved_slots[account_id] = current - 1

    async def _handle_auth_failure(
        self, account: AccountDB, exc: BaseException,
    ) -> None:
        """React to a Flow auth error. A single failure is treated as a
        transient UI hiccup — only after ``_AUTH_STREAK_THRESHOLD`` hits in
        a row do we actually flip the account to ``needs_reauth``.
        """
        streak = self._auth_fail_streak.get(account.id, 0) + 1
        self._auth_fail_streak[account.id] = streak
        if streak < _AUTH_STREAK_THRESHOLD:
            logger.warning(
                "[BrowserProvider] Transient auth problem on %s (streak %d/%d): %s "
                "— evicting session, keeping account active",
                account.email, streak, _AUTH_STREAK_THRESHOLD, exc,
            )
            try:
                await get_pool().evict(account.id, timeout=5.0)
            except Exception:
                pass
            return

        logger.warning(
            "[BrowserProvider] Auth problem on %s (streak %d/%d reached): %s "
            "— marking needs_reauth",
            account.email, streak, _AUTH_STREAK_THRESHOLD, exc,
        )
        self._auth_fail_streak.pop(account.id, None)
        try:
            await db.update_account(account.id, status="needs_reauth")
        except Exception as db_err:
            logger.error(
                "[BrowserProvider] Could not mark %s needs_reauth: %s",
                account.email, db_err,
            )
        try:
            await get_pool().evict(account.id, timeout=5.0)
        except Exception:
            pass

    async def _handle_account_disabled(
        self, account: AccountDB, exc: BaseException,
    ) -> None:
        """React to ``AccountDisabledError``. First hit is treated as a
        possibly-transient Google UI misread → mark needs_reauth so the
        immediate-reauth monitor gets one more shot. Only after
        ``_DISABLED_STREAK_THRESHOLD`` consecutive hits do we write the
        account off with ``status=banned``.
        """
        streak = self._disabled_streak.get(account.id, 0) + 1
        self._disabled_streak[account.id] = streak
        try:
            await get_pool().evict(account.id, timeout=5.0)
        except Exception:
            pass

        if streak < _DISABLED_STREAK_THRESHOLD:
            logger.warning(
                "[BrowserProvider] AccountDisabled signal on %s (streak %d/%d): %s "
                "— marking needs_reauth (soft), will re-check after reauth",
                account.email, streak, _DISABLED_STREAK_THRESHOLD, exc,
            )
            try:
                await db.update_account(account.id, status="needs_reauth")
            except Exception as db_err:
                logger.error(
                    "[BrowserProvider] Could not soft-disable %s: %s",
                    account.email, db_err,
                )
            return

        logger.warning(
            "[BrowserProvider] AccountDisabled confirmed on %s (streak %d): %s "
            "— marking banned",
            account.email, streak, exc,
        )
        self._disabled_streak.pop(account.id, None)
        try:
            await db.update_account(
                account.id, status="banned", is_active=False,
            )
        except Exception as db_err:
            logger.error(
                "[BrowserProvider] Could not ban %s: %s",
                account.email, db_err,
            )

    async def _record_browser_success(self, account_id: int) -> None:
        """Mark a successful browser generation: bump stats, reset both the
        in-memory failure streaks AND the persistent ``consecutive_errors``
        counter that the HTTP-path ``TokenManager.record_error`` writes into.

        Without this, a browser account silently accumulates
        ``consecutive_errors`` across any HTTP-path stumbles and eventually
        hits ``error_ban_threshold`` → ``status=disabled``, even though the
        browser side was doing fine. That's the main reason accounts "just
        die" in browser mode.
        """
        self._auth_fail_streak.pop(account_id, None)
        self._disabled_streak.pop(account_id, None)
        try:
            await db.increment_account_stats(account_id, success=True)
        except Exception as e:
            logger.debug("[BrowserProvider] increment_account_stats failed: %s", e)
        try:
            await db.update_account_used(account_id)
        except Exception:
            pass
        # Stamp the account's proxy as freshly used so the ProxyRotator's
        # "rested >= rest_window" filter knows this IP is hot. Cheap no-op
        # when proxy rotation is disabled.
        try:
            from ..proxy_rotator import get_proxy_rotator
            await get_proxy_rotator().mark_generation_for_account(account_id)
        except Exception as e:
            logger.debug("[BrowserProvider] mark_generation skipped: %s", e)


# ---------- module-level singleton accessor ----------

_provider: Optional[BrowserProvider] = None


def get_browser_provider() -> BrowserProvider:
    global _provider
    if _provider is None:
        _provider = BrowserProvider(
            timeout_seconds=app_config.browser_video_timeout,
        )
    return _provider


def reset_browser_provider() -> None:
    """For tests / reinitialisation."""
    global _provider
    _provider = None
