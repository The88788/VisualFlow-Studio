"""Proxy rotation for browser-mode generation.

When Google flags the active set of ISP proxies with "unusual activity",
this service swaps the burnt proxies for rested ones from the pool and puts
the burnt ones on a cooldown, so generation can resume on fresh IPs.

Model (browser mode):
  - Each account is pinned 1:1 to one proxy via ``account.proxy``. The browser
    session launches with that proxy, so ALL of the account's traffic goes
    through one IP (see ``browser_pool._normalize_proxy``).
  - ``proxy_pool.txt`` is the source list of proxies (12 ISP entries).
  - ``proxy_state`` (SQLite) tracks per-proxy usage/cooldown on top of that
    list: which are ``active`` (pinned to an account), ``benched`` (rested,
    reusable), or ``cooldown`` (recently burnt, resting N hours).

The rotator only does the proxy swap + browser eviction + state bookkeeping.
Re-authing the rotated accounts (through their new proxy) is driven by the
``proxy_rotation_monitor`` in ``src/main.py`` so the throttling and the
coordination with ``immediate_reauth_monitor`` live in one place.
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..core.config import CONFIG_PATH, config
from ..core.database import db
from ..core.logger import logger
from ..core.models import AccountDB
from .proxy_pool import _parse_line, get_proxy_pool
from .veo_client import normalize_proxy_url

_ACCOUNTS_JSON_PATH = CONFIG_PATH.parent / "accounts.json"


def _norm(url: Optional[str]) -> Optional[str]:
    """Canonical proxy identity used as the proxy_state primary key."""
    return normalize_proxy_url(url) if url else None


def _load_pool_entries() -> list[tuple[str, str]]:
    """Parse proxy_pool.txt into ``(normalized_url, label)`` pairs.

    Reuses :func:`proxy_pool._parse_line` for URL extraction (credential
    injection, header/comment skipping) and pulls the human label from the
    leading CSV field when present.
    """
    path = Path(config.proxy_pool_path)
    if not path.exists():
        return []
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        url = _parse_line(line)
        if not url:
            continue
        norm = _norm(url)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        stripped = line.strip()
        parts = stripped.split(",")
        label = parts[0].strip() if len(parts) >= 4 else norm
        entries.append((norm, label))
    return entries


class ProxyRotator:
    """Owns proxy_state bookkeeping and the swap-on-flag rotation."""

    def __init__(self) -> None:
        # Monotonic timestamp of the last successful rotation (anti-flapping).
        self.last_rotation_ts: float = 0.0

    async def sync(self) -> None:
        """Reconcile proxy_state with proxy_pool.txt and reflect cooldowns
        onto the round-robin ProxyPool so burnt IPs get no pool traffic."""
        entries = _load_pool_entries()
        if entries:
            inserted = await db.sync_proxy_states(entries)
            if inserted:
                logger.info(f"[ProxyRotator] Tracking {inserted} new proxy(ies) from pool")
        await self._push_unavailable()

    async def reconcile_assignments(self, accounts: list[AccountDB]) -> None:
        """Mark proxies currently pinned to an account as ``active`` (unless
        they're cooling down). Keeps proxy_state honest for inspection — the
        actual "don't hand out an in-use proxy" guard is the exclude set in
        :meth:`rotate`, but without this the dashboard would show in-use
        proxies as ``benched`` after a restart."""
        assigned = {_norm(a.proxy) for a in accounts if a.proxy}
        assigned.discard(None)
        if not assigned:
            return
        states = {s.proxy_url: s for s in await db.get_all_proxy_states()}
        for url in assigned:
            s = states.get(url)
            if s and s.status == "benched":
                await db.update_proxy_state(url, status="active")

    async def tick(self) -> int:
        """Release proxies whose cooldown has expired (cooldown → benched).
        Returns the number released."""
        now = datetime.utcnow()
        states = await db.get_all_proxy_states()
        released = 0
        for s in states:
            if s.status == "cooldown" and (s.cooldown_until is None or s.cooldown_until <= now):
                await db.update_proxy_state(s.proxy_url, status="benched")
                released += 1
                logger.info(f"[ProxyRotator] Cooldown expired, benched proxy {s.label or s.proxy_url}")
        if released:
            await self._push_unavailable()
        return released

    async def _push_unavailable(self) -> None:
        """Tell the ProxyPool which proxies are on cooldown (excluded from any
        ad-hoc round-robin pool traffic, e.g. image upload / status polling)."""
        states = await db.get_all_proxy_states()
        cooled = {s.proxy_url for s in states if s.status == "cooldown"}
        get_proxy_pool().set_unavailable(cooled)

    async def mark_generation_for_account(self, account_id: int) -> None:
        """Bump the account's current proxy ``last_generation_at`` so the
        'rested ≥ rest_window' filter in :meth:`pick_fresh` stays honest."""
        account = await db.get_account(account_id)
        if not account:
            return
        norm = _norm(account.proxy)
        if norm:
            await db.mark_proxy_generation(norm)

    async def pick_fresh(self, k: int, exclude: Optional[set[str]] = None) -> list[str]:
        """Return up to ``k`` benched proxies that are out of cooldown and have
        rested at least ``rest_window_hours`` since their last generation,
        longest-rested first. ``exclude`` is a set of normalized URLs to skip
        (proxies currently pinned to an account)."""
        exclude = exclude or set()
        now = datetime.utcnow()
        rest_cutoff = now - timedelta(hours=config.proxy_rotation_rest_window_hours)
        states = await db.get_all_proxy_states()

        candidates = []
        for s in states:
            if s.status != "benched":
                continue
            if s.proxy_url in exclude:
                continue
            if s.cooldown_until is not None and s.cooldown_until > now:
                continue
            if s.last_generation_at is not None and s.last_generation_at > rest_cutoff:
                continue
            candidates.append(s)

        # Longest-rested first: never-used (None) ahead of oldest last_generation_at.
        candidates.sort(
            key=lambda s: (s.last_generation_at is not None,
                           s.last_generation_at or datetime.min)
        )
        return [s.proxy_url for s in candidates[:k]]

    async def rotate(self, accounts: list[AccountDB]) -> dict:
        """Swap each account's burnt proxy for a fresh one.

        Steps per account: cooldown the old proxy → assign a fresh proxy in
        DB + accounts.json → mark it active → reset the cached HTTP client and
        evict the live browser session so the next launch uses the new IP.

        Re-auth is NOT done here — the caller (monitor / endpoint) reauths the
        returned accounts through their new proxy. If there aren't enough fresh
        proxies for every account, NOTHING is rotated (we don't want to send
        accounts back onto a burnt IP); the caller should alert and wait.

        Returns a summary dict with ``rotated`` = list of ``{account_id, email,
        old, new}`` and ``ok`` / ``reason``.
        """
        if not accounts:
            return {"ok": False, "reason": "no_accounts", "rotated": []}

        # Never reuse a proxy currently pinned to ANY account.
        all_accounts = await db.get_all_accounts()
        in_use = {_norm(a.proxy) for a in all_accounts if a.proxy}
        in_use.discard(None)

        fresh = await self.pick_fresh(len(accounts), exclude=in_use)
        if len(fresh) < len(accounts):
            logger.error(
                f"[ProxyRotator] Not enough fresh proxies: need {len(accounts)}, "
                f"have {len(fresh)}. Holding rotation (burnt accounts stay parked "
                f"to avoid re-flagging the same IPs). Add proxies or shorten cooldown."
            )
            return {
                "ok": False,
                "reason": "insufficient_fresh_proxies",
                "needed": len(accounts),
                "available": len(fresh),
                "rotated": [],
            }

        now = datetime.utcnow()
        cooldown_until = now + timedelta(hours=config.proxy_rotation_cooldown_hours)
        rotated = []
        new_assignments: dict[str, str] = {}  # email -> new proxy url

        for acc, new_proxy in zip(accounts, fresh):
            old_proxy = acc.proxy
            old_norm = _norm(old_proxy)

            # 1. Cooldown the burnt proxy.
            if old_norm:
                old_state = await db.get_proxy_state(old_norm)
                await db.update_proxy_state(
                    old_norm,
                    status="cooldown",
                    cooldown_until=cooldown_until,
                    flagged_count=(old_state.flagged_count + 1) if old_state else 1,
                    flagged_at=now,
                )

            # 2. Pin the fresh proxy to the account (DB + accounts.json).
            await db.update_account(acc.id, proxy=new_proxy)
            new_assignments[acc.email] = new_proxy

            # 3. Mark the fresh proxy active.
            await db.update_proxy_state(new_proxy, status="active", last_assigned_at=now)

            # 4. Drop cached client + live browser session so the next launch
            #    picks up the new proxy.
            await self._reset_account_runtime(acc.id, old_proxy)

            rotated.append({
                "account_id": acc.id,
                "email": acc.email,
                "old": old_norm,
                "new": new_proxy,
            })
            logger.info(
                f"[ProxyRotator] {acc.email}: {_mask(old_norm)} → {_mask(new_proxy)} "
                f"(old on cooldown until {cooldown_until:%Y-%m-%d %H:%M})"
            )

        self._patch_accounts_json(new_assignments)
        await self._push_unavailable()
        self.last_rotation_ts = time.time()
        return {"ok": True, "reason": "rotated", "rotated": rotated}

    async def _reset_account_runtime(self, account_id: int, old_proxy: Optional[str]) -> None:
        """Invalidate the cached HTTP client for the old proxy and evict the
        live browser session so a fresh one launches with the new proxy."""
        try:
            from ..api.routes import get_token_manager
            tm = get_token_manager()
            if tm:
                await tm.reset_client_for_proxy(old_proxy)
        except Exception as e:
            logger.debug(f"[ProxyRotator] reset_client_for_proxy skipped: {e}")
        try:
            from .providers.browser_pool import get_pool
            await get_pool().evict(account_id)
        except Exception as e:
            logger.debug(f"[ProxyRotator] browser evict skipped for {account_id}: {e}")

    def _patch_accounts_json(self, email_to_proxy: dict[str, str]) -> None:
        """Persist new proxy assignments to accounts.json so they survive a
        restart AND so browser_login.py re-auths through the new proxy (it
        reads the proxy from this file, not the DB)."""
        if not email_to_proxy:
            return
        try:
            if not _ACCOUNTS_JSON_PATH.exists():
                return
            data = json.loads(_ACCOUNTS_JSON_PATH.read_text(encoding="utf-8"))
            for entry in data:
                new_proxy = email_to_proxy.get(entry.get("email", ""))
                if new_proxy is not None:
                    entry["proxy"] = new_proxy
            _ACCOUNTS_JSON_PATH.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[ProxyRotator] failed to patch accounts.json: {e}")


def _mask(url: Optional[str]) -> str:
    """Mask credentials in a proxy URL for logging."""
    if not url:
        return "direct"
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = p.hostname or "?"
        port = p.port or "?"
        return f"{host}:{port}"
    except Exception:
        return "proxy"


_instance: Optional[ProxyRotator] = None


def get_proxy_rotator() -> ProxyRotator:
    """Return the global ProxyRotator singleton."""
    global _instance
    if _instance is None:
        _instance = ProxyRotator()
    return _instance
