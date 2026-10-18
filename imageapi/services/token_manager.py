"""Token manager for ImageAPI — manages account tokens lifecycle."""

import asyncio
import time
from datetime import datetime
from typing import Optional

from ..core.database import db
from ..core.logger import logger
from ..core.models import AccountDB
from .capsolver import CapsolverService
from .image_client import ImageClient, ImageClientError, TokenInfo


class TokenManager:
    """Multi-account token lifecycle management."""

    def __init__(
        self,
        capsolver: Optional[CapsolverService] = None,
        proxy: Optional[str] = None,
        error_ban_threshold: int = 5,
    ):
        self.capsolver = capsolver
        self.proxy = proxy
        self.error_ban_threshold = error_ban_threshold
        self._default_client: Optional[ImageClient] = None
        self._account_clients: dict[str, ImageClient] = {}
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        self._throttle_hits: dict[int, list[float]] = {}
        self._throttle_threshold = 5
        self._throttle_window = 3600

    def _get_refresh_lock(self, account_id: int) -> asyncio.Lock:
        if account_id not in self._refresh_locks:
            self._refresh_locks[account_id] = asyncio.Lock()
        return self._refresh_locks[account_id]

    def get_client_for_account(self, account: AccountDB) -> ImageClient:
        """Get or create ImageClient for account (cached by proxy)."""
        proxy = account.proxy or self.proxy or ""
        if proxy not in self._account_clients:
            self._account_clients[proxy] = ImageClient(
                capsolver=self.capsolver,
                proxy=proxy or None,
            )
        return self._account_clients[proxy]

    def get_default_client(self) -> ImageClient:
        if not self._default_client:
            self._default_client = ImageClient(
                capsolver=self.capsolver,
                proxy=self.proxy or None,
            )
        return self._default_client

    async def add_account(
        self, email: str, st: str, remark: str = "", proxy: str = ""
    ) -> dict:
        """Add account, exchange ST for AT."""
        existing = await db.get_account_by_email(email)
        if existing:
            # Update ST
            await db.update_account(existing.id, st=st, status="active", is_active=True)
            account_id = existing.id
            logger.info(f"Updated existing account: {email}")
        else:
            account_id = await db.add_account(email, st, remark, proxy)
            logger.info(f"Added new account: {email} (id={account_id})")

        # Exchange token
        account = await db.get_account(account_id)
        client = self.get_client_for_account(account)

        try:
            token_info = await client.exchange_st_for_at(st)
            credits_info = await client.get_credits(token_info.at)

            await db.update_account(
                account_id,
                at=token_info.at,
                at_expires=datetime.fromtimestamp(token_info.expires_at),
                project_id=token_info.project_id,
                credits=credits_info.get("credits"),
                user_paygate_tier=credits_info.get("userPaygateTier"),
                status="active",
                is_active=True,
            )

            return {
                "account_id": account_id,
                "email": email,
                "status": "active",
                "project_id": token_info.project_id,
                "credits": credits_info.get("credits"),
                "tier": credits_info.get("userPaygateTier"),
            }
        except ImageClientError as e:
            logger.error(f"Token exchange failed for {email}: {e}")
            await db.update_account(account_id, status="needs_reauth", is_active=False)
            return {
                "account_id": account_id,
                "email": email,
                "status": "needs_reauth",
                "error": str(e),
            }

    async def refresh_account_token(self, account_id: int, force: bool = False) -> bool:
        """Refresh token with per-account lock."""
        lock = self._get_refresh_lock(account_id)
        async with lock:
            account = await db.get_account(account_id)
            if not account:
                logger.error(f"Account {account_id} not found")
                return False

            if not force and account.at_expires:
                if account.at_expires.timestamp() > time.time() + 300:
                    return True

            try:
                client = self.get_client_for_account(account)
                token_info = await client.exchange_st_for_at(account.st)
                credits_info = await client.get_credits(token_info.at)

                await db.update_account(
                    account_id,
                    at=token_info.at,
                    at_expires=datetime.fromtimestamp(token_info.expires_at),
                    project_id=token_info.project_id,
                    credits=credits_info.get("credits"),
                    user_paygate_tier=credits_info.get("userPaygateTier"),
                )

                # Reset consecutive errors on successful refresh
                await db.reset_account_failures(account_id)

                tier = credits_info.get("userPaygateTier", "")
                if tier == "PAYGATE_TIER_NOT_PAID":
                    await db.update_account(account_id, is_active=False, status="free_tier")
                    logger.warning(
                        f"Account {account.email} auto-disabled: free tier (PAYGATE_TIER_NOT_PAID)"
                    )
                    return False

                credits = credits_info.get("credits")
                if credits is not None and credits < 100:
                    await db.update_account(account_id, is_active=False, status="low_credits")
                    logger.warning(
                        f"Account {account.email} auto-disabled: {credits} credits left"
                    )
                    return True

                logger.info(
                    f"Token refreshed for {account.email} "
                    f"(credits={credits_info.get('credits')}, tier={tier})"
                )
                return True

            except ImageClientError as e:
                logger.error(f"Failed to refresh token for {account.email}: {e}")
                if e.error_code == "UNAUTHORIZED":
                    await db.update_account(
                        account_id,
                        status="needs_reauth",
                        is_active=False,
                        remark="Session token invalid or expired",
                    )
                    logger.warning(
                        f"Account {account.email} marked for re-auth: invalid ST"
                    )
                return False

    async def refresh_all_expiring(self, force: bool = False) -> dict:
        """Refresh all account tokens."""
        accounts = await db.get_all_accounts()
        results = {"total": len(accounts), "success": 0, "failed": 0}

        for account in accounts:
            if account.status in ("disabled", "free_tier"):
                continue
            try:
                ok = await self.refresh_account_token(account.id, force=force)
                if ok:
                    results["success"] += 1
                else:
                    results["failed"] += 1
            except Exception as e:
                logger.error(f"Refresh error for {account.email}: {e}")
                results["failed"] += 1

        logger.info(f"Token refresh: {results['success']}/{results['total']} success")
        return results

    async def record_success(self, account_id: int) -> None:
        """Record a successful generation for an account."""
        await db.increment_account_stats(account_id, success=True)
        await db.update_account(account_id, last_used_at=datetime.now())

    async def record_error(self, account_id: int, error_code: Optional[str] = None) -> None:
        """Record a failed generation. May auto-disable on consecutive errors or RATE_LIMITED."""
        await db.increment_account_stats(account_id, success=False)
        await db.update_account(account_id, last_used_at=datetime.now())

        account = await db.get_account(account_id)
        email = account.email if account else f"id={account_id}"

        # Auto-ban after N consecutive errors
        stats = await db.get_account_stats(account_id)
        if stats and stats.consecutive_errors >= self.error_ban_threshold:
            await db.update_account(account_id, status="disabled", is_active=False)
            logger.warning(
                f"Account {email} auto-disabled: {stats.consecutive_errors} consecutive errors"
            )

        # RATE_LIMITED — disable immediately
        if error_code == "RATE_LIMITED":
            await db.update_account(account_id, status="disabled", is_active=False)
            logger.warning(f"Account {email} disabled: rate limited by Google")

        # UNAUTHORIZED — mark for re-auth
        if error_code == "UNAUTHORIZED":
            await db.update_account(
                account_id, status="needs_reauth", is_active=False,
                remark="Session token invalid or expired",
            )
            logger.warning(f"Account {email} marked for re-auth: invalid ST")

    async def record_throttle(self, account_id: int) -> None:
        """Record a throttle hit. Disable account for 1 hour after threshold."""
        now = time.time()
        cutoff = now - self._throttle_window

        hits = self._throttle_hits.get(account_id, [])
        hits = [t for t in hits if t > cutoff]
        hits.append(now)
        self._throttle_hits[account_id] = hits

        account = await db.get_account(account_id)
        email = account.email if account else f"id={account_id}"

        logger.warning(f"Account {email} throttled ({len(hits)}/{self._throttle_threshold} in last hour)")

        if len(hits) >= self._throttle_threshold:
            await db.update_account(account_id, status="throttled", is_active=False)
            logger.warning(f"Account {email} disabled for 1 hour: {len(hits)} throttle errors")
            self._throttle_hits.pop(account_id, None)
            asyncio.create_task(self._reenable_after_cooldown(account_id, 3600))

    async def _reenable_after_cooldown(self, account_id: int, delay: int) -> None:
        """Re-enable an account after cooldown period."""
        await asyncio.sleep(delay)
        account = await db.get_account(account_id)
        if account and account.status in ("throttled", "cooldown"):
            await db.update_account(account_id, status="active", is_active=True)
            await db.reset_account_failures(account_id)
            logger.info(f"Account {account.email} re-enabled after {delay}s throttle cooldown")

    async def sync_tokens_from_file(self, filepath) -> int:
        """Load tokens from auth_tokens.json. Supports both dict {email: token} and array [{email, st}] formats."""
        import json
        from pathlib import Path

        path = Path(filepath)
        if not path.exists():
            logger.warning(f"Auth tokens file not found: {path}")
            return 0

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            new_count = 0
            updated = 0

            # Normalize to list of (email, st) tuples
            pairs = []
            if isinstance(raw, dict):
                pairs = [(email, st) for email, st in raw.items() if email and st]
            elif isinstance(raw, list):
                for entry in raw:
                    email = entry.get("email", "")
                    st = entry.get("st") or entry.get("session_token", "")
                    if email and st:
                        pairs.append((email, st))

            for email, st in pairs:
                existing = await db.get_account_by_email(email)
                if existing:
                    updates = {"st": st, "at": None, "at_expires": None}
                    if existing.st != st or existing.status in ("needs_reauth", "check_required"):
                        updates["status"] = "active"
                        updates["is_active"] = True
                        updates["remark"] = "Restored via auth_tokens.json"
                    await db.update_account(existing.id, **updates)
                    # Reset cached client to clear stale session cookies
                    if existing.proxy in self._account_clients:
                        await self._account_clients[existing.proxy].close()
                        del self._account_clients[existing.proxy]
                    updated += 1
                else:
                    try:
                        await self.add_account(email=email, st=st, remark="Imported from auth_tokens.json")
                        new_count += 1
                    except Exception as e:
                        logger.error(f"Failed to import account {email}: {e}")

            logger.info(f"Token Sync: {new_count} new accounts added, {updated} updated from {path.name}")
            return new_count + updated
        except Exception as e:
            logger.error(f"Failed to sync tokens from file: {e}")
            return 0

    async def sync_accounts_from_file(self, filepath) -> int:
        """Load proxy settings from accounts.json."""
        import json
        from pathlib import Path

        path = Path(filepath)
        if not path.exists():
            logger.warning(f"Accounts file not found: {path}")
            return 0

        try:
            accounts_data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(accounts_data, list):
                logger.error("accounts.json must be a list of account objects")
                return 0

            synced = 0
            for entry in accounts_data:
                email = entry.get("email", "")
                proxy = entry.get("proxy", "")
                if not email:
                    continue

                existing = await db.get_account_by_email(email)
                if existing and proxy and existing.proxy != proxy:
                    await db.update_account(existing.id, proxy=proxy)
                    synced += 1

            if synced > 0:
                logger.info(f"Proxy Sync: {synced} accounts updated from {path.name}")
            return synced
        except Exception as e:
            logger.error(f"Failed to sync accounts from file: {e}")
            return 0

    async def close(self) -> None:
        for client in self._account_clients.values():
            await client.close()
        if self._default_client:
            await self._default_client.close()
