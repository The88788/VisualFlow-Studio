"""Human-like Playwright interactions.

Why this exists
---------------
Playwright's stock ``page.click``, ``page.type`` and ``page.mouse.move``
produce inputs that look obviously synthetic to behavioural antibot
models: the cursor teleports straight to the target before clicking,
characters are typed at a perfectly uniform delay, scrolls happen in
single discrete jumps, and there are no idle pauses. Recent academic
work (FP-Agent 2026 and similar) singles these out as the *strongest*
discriminators between automation and real users — stronger than any
canvas / WebGL / UA fingerprint patch.

This module wraps the low-level Playwright surface with motion and
timing models drawn from public research on human input:

* **Mouse paths** use cubic Bézier curves with two random control
  points, segment-stepped at variable speed (slow-start, fast-middle,
  slow-end), with per-step jitter to break perfect smoothness. Distance
  affects total duration via Fitts's law.
* **Clicks** finish with a short hover (humans don't click the instant
  the cursor lands), then press-down → small dwell → release; press
  dwell is drawn from a lognormal distribution measured in real users
  (~70-120 ms typical).
* **Typing** uses per-character delays sampled from a lognormal with
  a tail (the occasional long pause), plus a configurable typo rate
  that inserts a wrong adjacent-key character and Backspace-corrects
  it after a human-realistic lag.
* **Scrolling** breaks the requested ``delta_y`` into small wheel
  steps following an exponential ease-out — humans flick the wheel,
  they don't move it linearly.
* **Idle** inserts realistic "looking at the screen" pauses whose
  distribution has a heavy tail (sometimes you actually stop and read).

Everything is optional and additive — call ``human_click(page, locator)``
in place of ``locator.click()`` and you're done. The same module can be
used from CloakBrowser or from vanilla Chromium; it only depends on the
Playwright async API.

Tuning
------
Each function takes a ``profile: HumanProfile`` keyword that controls
aggressiveness. Defaults are calibrated for "patient, attentive user".
For a faster operator persona, pass ``profile=FAST``. For an account
that has just been flagged and you want maximum caution, ``CAREFUL``.

Caveats
-------
* These functions ARE slower than raw Playwright by 100-2000 ms each.
  That's the point. Budget accordingly.
* The module relies on Playwright having a real ``page.mouse`` and
  ``page.keyboard``. It does NOT work for actions inside cross-origin
  iframes you don't control (reCAPTCHA challenge frame, etc.) — there
  Playwright can't move the OS cursor inside the frame anyway.
* For inputs guarded by client-side framework state (React controlled
  inputs that re-render mid-typing), we still fall back to dispatching
  one character at a time via keyboard.type, not ``.fill``, because
  ``.fill`` is the single biggest synthetic-input giveaway.
"""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field
from typing import Optional, Sequence, Union

# Playwright types are imported lazily-friendly: we accept duck-typed
# Page / Locator so the module also works under cloakbrowser's shimmed
# context without explicit type imports at module load.
try:
    from playwright.async_api import Page, Locator, ElementHandle  # type: ignore
except Exception:  # pragma: no cover
    Page = Locator = ElementHandle = object  # type: ignore


# =====================================================================
#                            Profiles
# =====================================================================

@dataclass(frozen=True)
class HumanProfile:
    """All knobs for one persona. Pass to any function via ``profile=``."""

    # ---- Mouse motion ------------------------------------------------
    # Total move duration is ``base_move_ms + distance_factor * sqrt(dist_px)``
    # (Fitts's-law-ish; sqrt because we account for target width with
    # a fixed hover circle, not log-ratio of width).
    base_move_ms: float = 180.0
    distance_factor: float = 6.5
    # Random multiplier per move, ``uniform(min, max)``. Adds variance
    # between moves so the curve length isn't deterministic from
    # distance alone.
    move_duration_jitter: tuple[float, float] = (0.85, 1.35)
    # Number of intermediate mousemove events per move. Real Chrome
    # streams ~60-120 mousemoves/sec; we aim for that density.
    move_steps_min: int = 24
    move_steps_max: int = 60
    # Per-step pixel jitter perpendicular to the path (px std-dev).
    # 0 = perfect Bézier (suspicious). Real human jitter is 0.3-1.5 px.
    path_jitter_px: float = 0.8
    # Bézier control-point offset range, in fraction of the straight
    # line distance. (0.1, 0.45) makes nicely-curved-but-not-loopy paths.
    bezier_control_offset: tuple[float, float] = (0.10, 0.42)
    # Probability that the second control point gets a sign flip,
    # giving an S-curve. Pure C-curves look slightly less natural.
    s_curve_probability: float = 0.35

    # ---- Click -------------------------------------------------------
    # Pre-click hover: cursor arrives, sits still for this long before
    # the button press. Lognormal(mu, sigma) ms; humans rarely click
    # in <50ms after arriving.
    pre_click_hover_ms_mu: float = 4.6      # ln-mean → median ~99 ms
    pre_click_hover_ms_sigma: float = 0.45
    # Press-dwell: time between mousedown and mouseup. Measured in real
    # users: 70-130 ms typical, occasional 200+ ms drag-press.
    click_dwell_ms_mu: float = 4.5          # ln-mean → median ~90 ms
    click_dwell_ms_sigma: float = 0.30
    # Probability of a tiny extra micro-move while hovering (jitter of
    # 1-3 px). Real users almost never hold the cursor perfectly still.
    hover_microjitter_probability: float = 0.55

    # ---- Typing ------------------------------------------------------
    # Per-character delay (ms). Lognormal — most chars are ~70 ms but
    # the occasional 300+ ms pause is normal (thinking, looking at kb).
    char_delay_ms_mu: float = 4.3           # median ~74 ms → ~70 wpm
    char_delay_ms_sigma: float = 0.55
    # Probability per character of a long "thinking" pause (300-1200 ms)
    # injected BEFORE the keystroke. Caps at one per ~25 chars on avg.
    thinking_pause_probability: float = 0.04
    thinking_pause_ms: tuple[float, float] = (320.0, 1100.0)
    # Probability per character of a typo (wrong adjacent key) that
    # gets noticed and Backspace-corrected. 0.015 ≈ one typo per 60-70
    # chars, which is on the realistic side for someone paying attention.
    typo_probability: float = 0.012
    # After a typo, time until the user notices and starts correcting.
    typo_notice_ms: tuple[float, float] = (140.0, 420.0)
    # Pause after Backspace before retyping the correct char.
    typo_correction_ms: tuple[float, float] = (60.0, 200.0)
    # Whitespace gets a small extra delay (word boundary, micro-think).
    word_boundary_extra_ms: tuple[float, float] = (30.0, 180.0)

    # ---- Scroll ------------------------------------------------------
    # Each scroll is broken into N wheel ticks, easing out.
    scroll_steps_min: int = 6
    scroll_steps_max: int = 14
    # ms between ticks; ease-out means later ticks come slower.
    scroll_tick_ms_min: float = 14.0
    scroll_tick_ms_max: float = 38.0
    # Probability of a small reverse-scroll micro-correction at the end
    # (humans often overshoot and pull back).
    scroll_overshoot_probability: float = 0.18

    # ---- Idle --------------------------------------------------------
    # Default "between-actions" pause distribution (ms).
    idle_ms_mu: float = 6.1                  # median ~445 ms
    idle_ms_sigma: float = 0.70


# Three preset personas. ``DEFAULT`` is what you usually want.
DEFAULT = HumanProfile()
FAST = HumanProfile(
    base_move_ms=110.0,
    distance_factor=4.2,
    move_steps_min=18,
    move_steps_max=40,
    pre_click_hover_ms_mu=4.0,
    char_delay_ms_mu=3.9,
    char_delay_ms_sigma=0.45,
    thinking_pause_probability=0.02,
    typo_probability=0.008,
    idle_ms_mu=5.4,
)
CAREFUL = HumanProfile(
    base_move_ms=260.0,
    distance_factor=9.5,
    move_steps_min=36,
    move_steps_max=80,
    move_duration_jitter=(1.0, 1.7),
    pre_click_hover_ms_mu=5.1,
    pre_click_hover_ms_sigma=0.55,
    char_delay_ms_mu=4.7,
    char_delay_ms_sigma=0.65,
    thinking_pause_probability=0.08,
    typo_probability=0.018,
    idle_ms_mu=6.7,
    idle_ms_sigma=0.85,
)


# =====================================================================
#                          Math helpers
# =====================================================================

def _lognormal_ms(mu: float, sigma: float, *, clamp_min_ms: float = 8.0,
                  clamp_max_ms: float = 8000.0) -> float:
    """Draw a duration in milliseconds from a lognormal distribution.

    Lognormal is the canonical fit for typing inter-key intervals,
    saccade latencies, and "patience" pauses. Clamping prevents the
    occasional pathological tail draw from stalling the run.
    """
    v = math.exp(random.gauss(mu, sigma))
    return max(clamp_min_ms, min(clamp_max_ms, v))


def _bezier_point(p0: tuple[float, float], p1: tuple[float, float],
                  p2: tuple[float, float], p3: tuple[float, float],
                  t: float) -> tuple[float, float]:
    """Cubic Bézier at parameter t ∈ [0, 1]."""
    u = 1.0 - t
    b = u * u * u
    c = 3 * u * u * t
    d = 3 * u * t * t
    e = t * t * t
    x = b * p0[0] + c * p1[0] + d * p2[0] + e * p3[0]
    y = b * p0[1] + c * p1[1] + d * p2[1] + e * p3[1]
    return x, y


def _ease_out_cubic(t: float) -> float:
    """Slow-start, fast-middle, slow-end. Better fit to real cursor
    velocity profiles than uniform stepping."""
    # Symmetric ease: ease-in for first half, ease-out for second.
    if t < 0.5:
        return 4.0 * t * t * t
    f = 2.0 * t - 2.0
    return 0.5 * f * f * f + 1.0


# Adjacent-key map for realistic typo generation, US QWERTY. We only
# need a small subset — typo rate is low anyway.
_ADJ_KEYS = {
    "a": "sqzw", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wrsd34",
    "f": "drtgvc", "g": "ftyhbv", "h": "gyujnb", "i": "uojk89", "j": "huiknm",
    "k": "jilom,", "l": "kop;.", "m": "njk,", "n": "bhjm",
    "o": "iklp90", "p": "ol;[0-", "q": "12wa", "r": "edft45",
    "s": "awedxz", "t": "rfgy56", "u": "yhji78", "v": "cfgb",
    "w": "qase23", "x": "zsdc", "y": "tghu67", "z": "asx",
    "1": "2q", "2": "13qw", "3": "24we", "4": "35er", "5": "46rt",
    "6": "57ty", "7": "68yu", "8": "79ui", "9": "80io", "0": "9op-",
}


def _typo_char_for(ch: str) -> Optional[str]:
    """Return a plausible adjacent-key typo for ``ch``, or None if we
    don't have a reasonable substitute (special chars, unicode, etc)."""
    low = ch.lower()
    candidates = _ADJ_KEYS.get(low)
    if not candidates:
        return None
    pick = random.choice(candidates)
    return pick.upper() if ch.isupper() else pick


# =====================================================================
#                          Mouse motion
# =====================================================================

async def human_move(page: "Page", to_x: float, to_y: float, *,
                     from_x: Optional[float] = None,
                     from_y: Optional[float] = None,
                     profile: HumanProfile = DEFAULT) -> tuple[float, float]:
    """Move the cursor along a Bézier path with variable speed and jitter.

    If ``from_x``/``from_y`` are omitted, we keep an in-page cache of the
    cursor's last known position (initialised lazily to viewport centre).
    Returns the final (x, y) so callers can chain.
    """
    last = _LAST_CURSOR.get(id(page))
    if from_x is None or from_y is None:
        if last is None:
            # First move ever on this page — start somewhere reasonable.
            # Viewport centre is a safer default than (0, 0) which would
            # produce a giant initial sweep.
            try:
                vp = page.viewport_size or {"width": 1280, "height": 800}
            except Exception:
                vp = {"width": 1280, "height": 800}
            from_x = vp["width"] / 2 + random.uniform(-80, 80)
            from_y = vp["height"] / 2 + random.uniform(-60, 60)
        else:
            from_x, from_y = last

    dx = to_x - from_x
    dy = to_y - from_y
    dist = math.hypot(dx, dy)

    if dist < 1.5:
        # Already on target. Still emit one tiny micro-move so we don't
        # look perfectly static.
        await page.mouse.move(to_x + random.uniform(-0.5, 0.5),
                              to_y + random.uniform(-0.5, 0.5))
        _LAST_CURSOR[id(page)] = (to_x, to_y)
        return to_x, to_y

    # Two random control points perpendicular to the straight line.
    nx, ny = -dy / dist, dx / dist  # unit normal
    off1 = random.uniform(*profile.bezier_control_offset) * dist
    off2 = random.uniform(*profile.bezier_control_offset) * dist
    sign2 = -1.0 if random.random() < profile.s_curve_probability else 1.0
    t1 = random.uniform(0.20, 0.45)
    t2 = random.uniform(0.55, 0.80)
    p0 = (from_x, from_y)
    p1 = (from_x + dx * t1 + nx * off1, from_y + dy * t1 + ny * off1)
    p2 = (from_x + dx * t2 + nx * off2 * sign2,
          from_y + dy * t2 + ny * off2 * sign2)
    p3 = (to_x, to_y)

    duration_ms = (profile.base_move_ms +
                   profile.distance_factor * math.sqrt(dist))
    duration_ms *= random.uniform(*profile.move_duration_jitter)
    steps = random.randint(profile.move_steps_min, profile.move_steps_max)
    # Density check: don't undersample on long moves (would look like a
    # straight teleport interrupted by 3 points).
    min_density = max(profile.move_steps_min, int(dist / 12))
    steps = max(steps, min_density)

    step_ms = duration_ms / steps

    for i in range(1, steps + 1):
        t = _ease_out_cubic(i / steps)
        bx, by = _bezier_point(p0, p1, p2, p3, t)
        # Perpendicular jitter — small noise that doesn't drift far
        # from the curve.
        if profile.path_jitter_px > 0:
            jx = random.gauss(0, profile.path_jitter_px)
            jy = random.gauss(0, profile.path_jitter_px)
            bx += jx
            by += jy
        await page.mouse.move(bx, by)
        # Sleep in ms-precision; Playwright clamps very small sleeps but
        # we want the kernel-scheduled gap anyway.
        await asyncio.sleep(step_ms / 1000.0)

    _LAST_CURSOR[id(page)] = (to_x, to_y)
    return to_x, to_y


# Per-page last-cursor cache. WeakKeyDictionary would be ideal but
# Page is unhashable across some Playwright builds; we use id() and
# accept the (tiny) risk of a stale entry after page.close().
_LAST_CURSOR: dict[int, tuple[float, float]] = {}


async def _bbox_center(target: Union["Locator", "ElementHandle"]) -> tuple[float, float]:
    """Compute viewport-relative click point inside the target.

    Pick a point near (but not exactly at) the bounding box center to
    avoid the "every click lands at integer pixel midpoint" pattern.
    """
    bb = await target.bounding_box()
    if bb is None:
        raise RuntimeError("humanize: target has no bounding box (off-screen / display:none)")
    # Random offset within the inner 60% of the box. Center bias is
    # natural — humans rarely click within 5px of the edge.
    cx = bb["x"] + bb["width"] * (0.5 + random.uniform(-0.15, 0.15))
    cy = bb["y"] + bb["height"] * (0.5 + random.uniform(-0.15, 0.15))
    return cx, cy


# =====================================================================
#                          Clicks
# =====================================================================

async def human_click(page: "Page",
                      target: Union["Locator", "ElementHandle", tuple[float, float]],
                      *,
                      button: str = "left",
                      click_count: int = 1,
                      profile: HumanProfile = DEFAULT,
                      scroll_into_view: bool = True) -> None:
    """Click a target like a person.

    ``target`` may be a Playwright Locator/ElementHandle, or a literal
    ``(x, y)`` viewport coordinate. Moves the cursor along a Bézier
    path, hovers briefly with possible micro-jitter, then presses with
    a realistic press-dwell.

    For double-clicks, set ``click_count=2`` — we still issue two real
    down/up pairs (not Playwright's atomic dblclick) so the second
    press dwell varies independently.
    """
    if isinstance(target, tuple):
        tx, ty = target
    else:
        if scroll_into_view:
            try:
                await target.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                # Best-effort — element may already be visible, or not
                # scrollable; either way we just try to click.
                pass
        tx, ty = await _bbox_center(target)

    await human_move(page, tx, ty, profile=profile)

    # Pre-click hover. Possibly with a 1-3px micro-jitter ("hand resting").
    hover_ms = _lognormal_ms(profile.pre_click_hover_ms_mu,
                             profile.pre_click_hover_ms_sigma,
                             clamp_min_ms=30.0, clamp_max_ms=600.0)
    if random.random() < profile.hover_microjitter_probability:
        # Split the hover into pre-jitter + post-jitter, with one tiny
        # cursor wiggle in between.
        half = hover_ms * random.uniform(0.3, 0.7)
        await asyncio.sleep(half / 1000.0)
        await page.mouse.move(tx + random.uniform(-2.5, 2.5),
                              ty + random.uniform(-2.0, 2.0))
        _LAST_CURSOR[id(page)] = (tx, ty)
        await asyncio.sleep((hover_ms - half) / 1000.0)
    else:
        await asyncio.sleep(hover_ms / 1000.0)

    for n in range(click_count):
        await page.mouse.down(button=button)
        dwell_ms = _lognormal_ms(profile.click_dwell_ms_mu,
                                 profile.click_dwell_ms_sigma,
                                 clamp_min_ms=40.0, clamp_max_ms=400.0)
        await asyncio.sleep(dwell_ms / 1000.0)
        await page.mouse.up(button=button)
        if n + 1 < click_count:
            # Inter-click gap for double-click: needs to be < OS
            # double-click threshold (usually 500 ms) but > 60 ms.
            await asyncio.sleep(random.uniform(0.090, 0.260))


# =====================================================================
#                          Typing
# =====================================================================

async def human_type(page: "Page", text: str, *,
                     target: Union["Locator", "ElementHandle", None] = None,
                     profile: HumanProfile = DEFAULT,
                     focus_first: bool = True) -> None:
    """Type ``text`` character by character with realistic timing.

    If ``target`` is given and ``focus_first`` is True, we human-click
    the target before typing so focus + cursor position are
    indistinguishable from a real user starting to type.

    Behaviour highlights:

    * Per-char delay drawn from lognormal.
    * Word-boundary characters get a small extra pause.
    * Occasional "thinking" pauses (300-1200 ms) before a keystroke.
    * Occasional typo: wrong adjacent key → notice delay → Backspace →
      correction delay → correct char.

    The function uses ``page.keyboard`` directly rather than
    ``target.type`` so we keep the cursor model in our own hands and
    so framework-controlled inputs (React, Lit) receive natural
    ``input`` events one at a time. We do NOT use ``.fill`` ever.
    """
    if target is not None and focus_first:
        await human_click(page, target, profile=profile)

    n = len(text)
    for i, ch in enumerate(text):
        # Thinking pause before this char?
        if random.random() < profile.thinking_pause_probability:
            await asyncio.sleep(random.uniform(*profile.thinking_pause_ms) / 1000.0)

        # Typo?
        if random.random() < profile.typo_probability:
            wrong = _typo_char_for(ch)
            if wrong is not None:
                await page.keyboard.type(wrong)
                # User notices after a beat
                await asyncio.sleep(random.uniform(*profile.typo_notice_ms) / 1000.0)
                await page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(*profile.typo_correction_ms) / 1000.0)
                # Fall through to type the correct char below.

        await page.keyboard.type(ch)

        # Inter-char delay (skip after the last char).
        if i + 1 < n:
            delay_ms = _lognormal_ms(profile.char_delay_ms_mu,
                                     profile.char_delay_ms_sigma,
                                     clamp_min_ms=18.0, clamp_max_ms=900.0)
            # Word-boundary nudge
            if ch == " " or ch in ".,!?;:\n":
                delay_ms += random.uniform(*profile.word_boundary_extra_ms)
            await asyncio.sleep(delay_ms / 1000.0)


# =====================================================================
#                          Scroll
# =====================================================================

async def human_scroll(page: "Page", delta_y: float, *,
                       delta_x: float = 0.0,
                       profile: HumanProfile = DEFAULT) -> None:
    """Scroll ``delta_y`` pixels (positive = down) with realistic
    wheel-tick pacing and an optional overshoot-correction at the end.
    """
    steps = random.randint(profile.scroll_steps_min, profile.scroll_steps_max)
    # Ease-out cubic for tick amplitudes — first ticks are biggest,
    # tail ticks are tiny, matching a real wheel flick.
    weights = []
    for i in range(steps):
        t = (i + 1) / steps
        weights.append(1.0 - (t - 1.0) ** 3 * -1.0)  # 1 → 2 monotonic
    total_w = sum(weights)
    for i, w in enumerate(weights):
        chunk = delta_y * (w / total_w)
        chunk_x = delta_x * (w / total_w)
        await page.mouse.wheel(chunk_x, chunk)
        # Inter-tick gap grows toward the end (deceleration).
        tick_ms = profile.scroll_tick_ms_min + (
            (profile.scroll_tick_ms_max - profile.scroll_tick_ms_min)
            * (i / max(steps - 1, 1))
        )
        await asyncio.sleep(tick_ms / 1000.0)

    if random.random() < profile.scroll_overshoot_probability:
        # Small reverse correction — humans overshoot and pull back.
        await asyncio.sleep(random.uniform(0.08, 0.22))
        back = -delta_y * random.uniform(0.05, 0.18)
        await page.mouse.wheel(0.0, back)


# =====================================================================
#                          Idle / pacing
# =====================================================================

async def idle(profile: HumanProfile = DEFAULT, *,
               multiplier: float = 1.0) -> None:
    """Pause for a realistic between-action duration.

    Call between distinct actions (after a click, before typing, after
    submit) instead of a fixed ``asyncio.sleep``. ``multiplier`` lets
    callers say "this is a long pause" (e.g. multiplier=4 after a
    generation submit to look like watching the loading spinner).
    """
    ms = _lognormal_ms(profile.idle_ms_mu, profile.idle_ms_sigma,
                       clamp_min_ms=80.0, clamp_max_ms=5000.0)
    await asyncio.sleep((ms * multiplier) / 1000.0)


async def attention_drift(page: "Page", *,
                          profile: HumanProfile = DEFAULT,
                          radius_px: float = 220.0) -> None:
    """Small idle mouse drift — the cursor doesn't sit perfectly still.

    Call occasionally during long waits (e.g. while a generation is in
    flight). Moves the cursor 30-220 px in a random direction along a
    short Bézier, then comes to rest.
    """
    last = _LAST_CURSOR.get(id(page))
    if last is None:
        try:
            vp = page.viewport_size or {"width": 1280, "height": 800}
        except Exception:
            vp = {"width": 1280, "height": 800}
        last = (vp["width"] / 2, vp["height"] / 2)
    angle = random.uniform(0, 2 * math.pi)
    r = random.uniform(30.0, radius_px)
    nx = last[0] + math.cos(angle) * r
    ny = last[1] + math.sin(angle) * r
    # Clamp to a reasonable viewport range so we don't drift off-screen.
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        nx = max(10.0, min(vp["width"] - 10.0, nx))
        ny = max(10.0, min(vp["height"] - 10.0, ny))
    except Exception:
        pass
    await human_move(page, nx, ny, from_x=last[0], from_y=last[1],
                     profile=profile)


# =====================================================================
#                          Convenience wrappers
# =====================================================================

async def human_fill(page: "Page",
                     target: Union["Locator", "ElementHandle"],
                     text: str, *,
                     clear_first: bool = True,
                     profile: HumanProfile = DEFAULT) -> None:
    """Replace a field's content with ``text`` via human typing.

    Drop-in replacement for ``locator.fill(text)``. With ``clear_first``,
    triple-clicks to select-all then Backspaces — visually identical to
    a user wiping a field before retyping.
    """
    await human_click(page, target, profile=profile)
    if clear_first:
        # Triple-click → select-all of the focused field on most inputs.
        # Cheaper and more natural than Ctrl+A then Delete.
        await human_click(page, target, click_count=3, profile=profile,
                          scroll_into_view=False)
        await asyncio.sleep(random.uniform(0.05, 0.18))
        await page.keyboard.press("Backspace")
        await asyncio.sleep(random.uniform(0.08, 0.22))
    await human_type(page, text, target=None, profile=profile, focus_first=False)


async def human_press(page: "Page", key: str, *,
                      profile: HumanProfile = DEFAULT) -> None:
    """Press a single key with realistic dwell. Use for Enter / Tab /
    Escape — not for typing text (use ``human_type``).
    """
    await page.keyboard.down(key)
    await asyncio.sleep(_lognormal_ms(profile.click_dwell_ms_mu,
                                      profile.click_dwell_ms_sigma,
                                      clamp_min_ms=35.0, clamp_max_ms=300.0)
                        / 1000.0)
    await page.keyboard.up(key)


__all__ = [
    "HumanProfile", "DEFAULT", "FAST", "CAREFUL",
    "human_move", "human_click", "human_type", "human_fill",
    "human_press", "human_scroll", "idle", "attention_drift",
]
