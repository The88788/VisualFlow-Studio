"""Load balancer for ImageAPI — account selection strategies."""

import time
from typing import Optional

from ..core.database import db
from ..core.logger import logger
from ..core.models import AccountDB


class LoadBalancer:
    """Account selection with 8-hour shift rotation."""

    async def get_account(self) -> Optional[AccountDB]:
        """Get account using 8-hour shift rotation (2 accounts per shift)."""
        accounts = await db.get_active_accounts()
        if not accounts:
            return None

        accounts.sort(key=lambda a: a.id)

        shift_duration = 8 * 3600
        current_shift = int(time.time() // shift_duration)
        accounts_per_shift = 2

        if len(accounts) <= accounts_per_shift:
            active_pool = accounts
        else:
            start_idx = (current_shift * accounts_per_shift) % len(accounts)
            active_pool = []
            for i in range(accounts_per_shift):
                idx = (start_idx + i) % len(accounts)
                active_pool.append(accounts[idx])

        # Prefer accounts with valid AT
        now = time.time()
        valid_pool = [
            a for a in active_pool
            if a.at and a.at_expires and a.at_expires.timestamp() > now + 300
        ]

        selection = valid_pool if valid_pool else active_pool
        # Pick least recently used
        selection.sort(key=lambda a: a.last_used_at.timestamp() if a.last_used_at else 0)
        return selection[0]

    async def get_account_round_robin(self) -> Optional[AccountDB]:
        """Simple round-robin by last_used_at."""
        accounts = await db.get_active_accounts()
        if not accounts:
            return None
        accounts.sort(key=lambda a: a.last_used_at.timestamp() if a.last_used_at else 0)
        return accounts[0]

    async def get_account_least_used(self) -> Optional[AccountDB]:
        """Get account with lowest today_count."""
        accounts = await db.get_active_accounts()
        if not accounts:
            return None

        best = accounts[0]
        best_count = float("inf")

        for acc in accounts:
            stats = await db.get_account_stats(acc.id)
            count = stats.today_count if stats else 0
            if count < best_count:
                best_count = count
                best = acc

        return best
