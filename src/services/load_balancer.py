"""Load balancer for distributing requests across accounts."""

import asyncio
import time
from typing import Optional

from ..core.database import db
from ..core.logger import logger
from ..core.models import AccountDB


class LoadBalancer:
    """Simple load balancer for selecting accounts."""

    def __init__(self) -> None:
        self._resting_release_lock = asyncio.Lock()

    def _filter_eligible_accounts(
        self,
        accounts: list[AccountDB],
        *,
        model_key: str,
        provider: str,
    ) -> list[AccountDB]:
        """Apply provider/model filters to an account list."""
        if provider == "browser":
            accounts = [a for a in accounts if getattr(a, "browser_ready", False)]
        else:
            accounts = [a for a in accounts if a.status != "free_tier"]

        is_relaxed = model_key.endswith("_relaxed") or "omni_flash" in model_key
        if not is_relaxed:
            excluded = {"relaxed_only"} if provider == "browser" else {"relaxed_only", "free_tier"}
            full_accounts = [a for a in accounts if a.status not in excluded]
            if full_accounts:
                accounts = full_accounts

        return accounts

    async def _revive_oldest_resting_account(
        self,
        *,
        model_key: str,
        provider: str,
    ) -> Optional[AccountDB]:
        """Emergency-release the oldest resting account when the active pool is empty."""
        async with self._resting_release_lock:
            active_accounts = self._filter_eligible_accounts(
                await db.get_active_accounts(),
                model_key=model_key,
                provider=provider,
            )
            if active_accounts:
                return None

            candidates = [
                a for a in await db.get_all_accounts()
                if a.status == "resting" and not a.is_active
            ]
            if provider == "browser":
                candidates = [a for a in candidates if getattr(a, "browser_ready", False)]

            if not candidates:
                return None

            candidates.sort(
                key=lambda a: (
                    a.cooldown_until.timestamp() if a.cooldown_until else 0,
                    a.last_used_at.timestamp() if a.last_used_at else 0,
                    a.id,
                )
            )
            chosen = candidates[0]

            await db.update_account(
                chosen.id,
                status="active",
                is_active=True,
                cooldown_until=None,
            )
            await db.reset_consecutive_errors(chosen.id)
            revived = await db.get_account(chosen.id)
            if revived:
                logger.warning(
                    "[LoadBalancer] No active %s accounts — emergency reactivated resting account %s",
                    provider,
                    revived.email,
                )
            return revived

    async def get_account(
        self,
        model_key: str = "",
        exclude_ids: set[int] | None = None,
        ignore_shift: bool = False,
        provider: str = "veo",
    ) -> Optional[AccountDB]:
        """
        Get an available account using an 8-hour shift rotation logic.
        Ensures exactly 2 accounts are active per 8-hour interval, giving others rest.
        Falls back to active accounts gracefully if accounts fail.

        Accounts with status='relaxed_only' are only eligible for relaxed models
        (model_key ending with '_relaxed'). For non-relaxed models they are skipped.

        Args:
            model_key: Google model key — used to filter relaxed_only accounts.
            provider: 'veo' (default, HTTP path) or 'browser'. When 'browser', only
                      accounts with ``browser_ready=True`` are considered.

        Returns:
            An active account assigned to the current shift, or None
        """
        accounts = self._filter_eligible_accounts(
            await db.get_active_accounts(),
            model_key=model_key,
            provider=provider,
        )

        if not accounts:
            revived = await self._revive_oldest_resting_account(
                model_key=model_key,
                provider=provider,
            )
            if revived is not None:
                accounts = self._filter_eligible_accounts(
                    [revived],
                    model_key=model_key,
                    provider=provider,
                )

        available_before_exclude = len(accounts)

        if exclude_ids:
            accounts = [a for a in accounts if a.id not in exclude_ids]

        if not accounts:
            # Only warn when the pool is genuinely empty. When callers pass
            # exclude_ids (e.g. accounts are currently saturated/busy), the
            # correct behavior is to wait quietly rather than emit a
            # misleading "no accounts" warning.
            if available_before_exclude == 0:
                logger.warning("No active accounts available")
            return None

        # Sort accounts deterministically by their ID to ensure consistent shifts
        accounts.sort(key=lambda a: a.id)

        # 8-hour shifts (8 hours = 28800 seconds)
        shift_duration = 8 * 3600
        current_shift = int(time.time() // shift_duration)

        # Number of accounts to be active simultaneously during a shift
        accounts_per_shift = 2

        active_pool = []
        if ignore_shift or len(accounts) <= accounts_per_shift:
            # Shift exhausted or too few accounts — use entire pool
            active_pool = accounts
        else:
            # Calculate the deterministic starting index for this shift
            start_idx = (current_shift * accounts_per_shift) % len(accounts)

            # Select accounts using modulo arithmetic for wrap-around
            for i in range(accounts_per_shift):
                idx = (start_idx + i) % len(accounts)
                active_pool.append(accounts[idx])

            logger.debug(
                f"Shift {current_shift}: selected accounts "
                f"{[a.email for a in active_pool]} out of {len(accounts)} healthy accounts"
            )

        # Distribute load evenly among the accounts in the active pool (internal round-robin)
        
        # Prefer accounts with valid AT tokens to minimize immediate refresh latency
        now = time.time()
        valid_pool = [
            a for a in active_pool
            if a.at and a.at_expires and a.at_expires.timestamp() > now + 300
        ]

        selection_pool = valid_pool if valid_pool else active_pool

        # Pick the one used least recently in the selection pool to balance request load
        selection_pool.sort(
            key=lambda a: a.last_used_at.timestamp() if a.last_used_at else 0
        )

        selected = selection_pool[0]
        # Bump last_used_at NOW so the next concurrent get_account call sees
        # this account as recently-used and rotates onward. Without this,
        # last_used_at only moves when the generation finishes — a long
        # generation keeps the account looking "least recently used" the
        # whole time and every request in that window piles onto it.
        try:
            await db.touch_account_last_used(selected.id)
        except Exception as e:  # pragma: no cover
            logger.debug(f"touch_account_last_used failed for {selected.id}: {e}")
        logger.debug(f"Assigned account for current generation via shift logic: {selected.email}")
        return selected

    async def get_account_round_robin(self) -> Optional[AccountDB]:
        """
        Get an account using round-robin based on last_used_at.

        Selects the account that was used least recently.

        Returns:
            Account or None
        """
        accounts = await db.get_active_accounts()

        if not accounts:
            return None

        # Filter valid tokens
        now = time.time()
        valid_accounts = [
            a
            for a in accounts
            if a.at and a.at_expires and a.at_expires.timestamp() > now + 300
        ]

        if not valid_accounts:
            valid_accounts = accounts

        # Sort by last_used_at (None first, then oldest)
        valid_accounts.sort(
            key=lambda a: (
                a.last_used_at.timestamp() if a.last_used_at else 0
            )
        )

        return valid_accounts[0]

    async def get_account_least_used(self) -> Optional[AccountDB]:
        """
        Get the account with lowest use_count today.

        Returns:
            Account or None
        """
        accounts = await db.get_active_accounts()

        if not accounts:
            return None

        # Get stats for all accounts
        account_with_stats = []
        for account in accounts:
            stats = await db.get_account_stats(account.id)
            today_count = stats.today_count if stats else 0
            account_with_stats.append((account, today_count))

        # Sort by today's count
        account_with_stats.sort(key=lambda x: x[1])

        return account_with_stats[0][0]


# Global instance
load_balancer = LoadBalancer()
