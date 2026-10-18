"""Token manager for handling multiple Google accounts."""

import asyncio
import time
from datetime import datetime
from typing import Optional

from ..core.database import db
from ..core.logger import logger
from ..core.models import AccountDB
from .capsolver import CapsolverService
from .proxy_pool import get_proxy_pool
from .veo_client import VeoClient, VeoClientError, normalize_proxy_url


def _is_proxy_error(msg: str) -> bool:
    """True if the error is a proxy connectivity failure (not an auth/logic error)."""
    msg = msg.lower()
    return any(k in msg for k in (
        "connect tunnel failed",
        "connection reset",
        "failed to perform",
        "curl: (56)",
        "curl: (35)",
        "curl: (7)",
        "proxy",
        "407",
        "402",
        "recv failure",
    ))


class TokenManager:
    """Manages Google account tokens and auto-refresh."""

    def __init__(
        self,
        capsolver: Optional[CapsolverService] = None,
        proxy: Optional[str] = None,
        error_ban_threshold: int = 5,
    ):
        self.capsolver = capsolver
        self.proxy = proxy
        self.error_ban_threshold = error_ban_threshold
        self._veo_client: Optional[VeoClient] = None
        # Per-account refresh locks to allow parallel refreshes across different accounts
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        # Cache of per-account VeoClient instances (keyed by proxy URL)
        self._account_clients: dict[str, VeoClient] = {}
        # Throttle tracking: account_id -> list of timestamps
        self._throttle_hits: dict[int, list[float]] = {}
        self._throttle_threshold = 5  # disable after this many throttles
        self._throttle_window = 3600  # within 1 hour window
        # PUBLIC_ERROR_UNUSUAL_ACTIVITY consecutive streak per account.
        # Reset on any successful generation. After threshold the account
        # rests for 30 minutes so Google's anti-abuse heuristics cool down
        # without draining the whole pool for hours.
        self._unusual_streak: dict[int, int] = {}
        self._unusual_threshold = 5
        self._unusual_rest_seconds = 30 * 60

    def _get_refresh_lock(self, account_id: int) -> asyncio.Lock:
        """Get or create a per-account refresh lock."""
        if account_id not in self._refresh_locks:
            self._refresh_locks[account_id] = asyncio.Lock()
        return self._refresh_locks[account_id]

    @property
    def veo_client(self) -> VeoClient:
        """Get VeoClient instance."""
        if self._veo_client is None:
            self._veo_client = VeoClient(
                capsolver=self.capsolver,
                proxy=self.proxy,
            )
        return self._veo_client

    async def close(self) -> None:
        """Close all VeoClient resources."""
        if self._veo_client:
            await self._veo_client.close()
        # Close all cached per-account clients
        for client in self._account_clients.values():
            await client.close()
        self._account_clients.clear()
        self._refresh_locks.clear()

    async def reset_client_for_proxy(self, proxy: str | None) -> None:
        """Reset cached VeoClient for a given proxy (raw or normalized).

        This forces a fresh session on next use, clearing any
        stale cookies (e.g. old session-token) from the cookie jar.
        Accepts both the raw Evomi-style URL and the normalized form.
        """
        # Normalize so we can find the cached key regardless of input format.
        norm = normalize_proxy_url(proxy)
        key = norm or proxy
        if key and key in self._account_clients:
            try:
                await self._account_clients[key].close()
            except Exception:
                pass
            del self._account_clients[key]
            logger.debug(f"Reset cached VeoClient for proxy {key}")
        elif not key and self._veo_client:
            try:
                await self._veo_client.close()
            except Exception:
                pass
            self._veo_client = None
            logger.debug("Reset default VeoClient")

    def get_veo_client_for_account(self, account: AccountDB) -> VeoClient:
        """
        Get VeoClient with account-specific proxy.

        Priority: residential_proxy (for auth/generate/upscale) > proxy (datacenter) > default.
        Caches clients by the effective proxy key to avoid resource leaks.
        """
        # Residential proxy takes priority for the main session (auth, generate, upscale).
        # Heavy traffic calls (upload_image, get_video_status) will override with datacenter
        # proxy via their own request_proxy parameter.
        effective_proxy = normalize_proxy_url(account.residential_proxy) or account.proxy
        if effective_proxy:
            if effective_proxy not in self._account_clients:
                self._account_clients[effective_proxy] = VeoClient(
                    capsolver=self.capsolver,
                    proxy=effective_proxy,
                )
            return self._account_clients[effective_proxy]
        # Fall back to default client (global proxy or no proxy)
        return self.veo_client

    def get_active_proxy_pool(self) -> list[str]:
        """Return unique proxies from all cached account clients (excluding None/empty)."""
        proxies = [p for p in self._account_clients if p]
        if self.veo_client.proxy:
            proxies.append(self.veo_client.proxy)
        return list(dict.fromkeys(proxies))  # deduplicate, preserve order

    async def add_account(
        self,
        email: str,
        st: str = "",
        refresh_token: Optional[str] = None,
        remark: Optional[str] = None,
        proxy: Optional[str] = None,
    ) -> int:
        """
        Add a new account and exchange ST or refresh_token for AT.

        Args:
            email: Account email
            st: Session token (NextAuth). Mutually exclusive with refresh_token.
            refresh_token: Google OAuth refresh_token (cockpit-tools accounts).
            remark: Optional note about the account
            proxy: Optional proxy URL for this account

        Returns:
            Created account ID
        """
        # Check if account exists
        existing = await db.get_account_by_email(email)
        if existing:
            raise ValueError(f"Account {email} already exists")

        # Exchange token (use account proxy if provided)
        proxy = normalize_proxy_url(proxy) if proxy else proxy
        try:
            if proxy:
                temp_client = VeoClient(capsolver=self.capsolver, proxy=proxy)
                if refresh_token:
                    token_info = await temp_client.exchange_refresh_token_for_at(refresh_token)
                else:
                    token_info = await temp_client.exchange_st_for_at(st)
                await temp_client.close()
            else:
                if refresh_token:
                    token_info = await self.veo_client.exchange_refresh_token_for_at(refresh_token)
                else:
                    token_info = await self.veo_client.exchange_st_for_at(st)
        except VeoClientError as e:
            raise ValueError(f"Failed to exchange token: {e}")

        # Create account
        account_id = await db.create_account(
            email=email,
            st=st or "",
            remark=remark,
            proxy=proxy,
            refresh_token=refresh_token,
        )

        # Update with token info
        await db.update_account(
            account_id,
            at=token_info.at,
            at_expires=datetime.fromtimestamp(token_info.expires_at),
            project_id=token_info.project_id,
            credits=token_info.credits,
            user_paygate_tier=token_info.user_paygate_tier,
        )

        logger.info(f"Account added: {email}")
        return account_id

    async def refresh_account_token(
        self, account_id: int, force: bool = False
    ) -> bool:
        """
        Refresh an account's access token.

        Args:
            account_id: Account ID
            force: Force refresh even if not expired

        Returns:
            True if refresh successful
        """
        lock = self._get_refresh_lock(account_id)
        async with lock:
            account = await db.get_account(account_id)
            if not account:
                logger.error(f"Account {account_id} not found")
                return False

            # Check if refresh needed
            if not force and account.at_expires:
                # Refresh if expiring in less than 5 minutes
                if account.at_expires.timestamp() > time.time() + 300:
                    return True

            try:
                # Use account-specific client (residential proxy takes priority)
                client = self.get_veo_client_for_account(account)
                try:
                    if account.refresh_token:
                        token_info = await client.exchange_refresh_token_for_at(account.refresh_token)
                    else:
                        token_info = await client.exchange_st_for_at(account.st)
                except VeoClientError as proxy_err:
                    if not _is_proxy_error(str(proxy_err)):
                        raise

                    # refresh_token accounts use OAuth (oauth2.googleapis.com) which is
                    # less sensitive to proxy issues; still retry with fallbacks if needed
                    # Residential proxy dead → try datacenter first
                    if account.residential_proxy:
                        logger.warning(
                            f"Residential proxy error for {account.email}, "
                            f"falling back to datacenter for token refresh: {proxy_err}"
                        )
                        fallback_proxy = account.proxy
                        if fallback_proxy and fallback_proxy not in self._account_clients:
                            self._account_clients[fallback_proxy] = VeoClient(
                                capsolver=self.capsolver,
                                proxy=fallback_proxy,
                            )
                        fallback_client = (
                            self._account_clients[fallback_proxy]
                            if fallback_proxy
                            else self.veo_client
                        )
                        try:
                            if account.refresh_token:
                                token_info = await fallback_client.exchange_refresh_token_for_at(account.refresh_token)
                            else:
                                token_info = await fallback_client.exchange_st_for_at(account.st)
                        except VeoClientError as dc_err:
                            if not _is_proxy_error(str(dc_err)):
                                raise
                            # Datacenter also dead → rotate from pool
                            token_info = await self._rotate_proxy_and_refresh(account, dc_err)
                    else:
                        # No residential — datacenter proxy itself is dead → rotate from pool
                        token_info = await self._rotate_proxy_and_refresh(account, proxy_err)

                await db.update_account(
                    account_id,
                    at=token_info.at,
                    at_expires=datetime.fromtimestamp(token_info.expires_at),
                    project_id=token_info.project_id,
                    credits=token_info.credits,
                    user_paygate_tier=token_info.user_paygate_tier,
                )

                # Reset consecutive errors on successful refresh
                await db.reset_consecutive_errors(account_id)

                # Free tier accounts: keep active for browser mode if they have credits
                if token_info.user_paygate_tier == "PAYGATE_TIER_NOT_PAID":
                    has_credits = token_info.credits is not None and token_info.credits > 0
                    if has_credits:
                        await db.update_account(
                            account_id,
                            is_active=True,
                            status="free_tier",
                        )
                        logger.warning(
                            f"Account {account.email} is free tier with {token_info.credits} credits "
                            f"— kept active for browser mode only"
                        )
                        return True
                    else:
                        await db.update_account(
                            account_id,
                            is_active=False,
                            status="free_tier",
                        )
                        logger.warning(
                            f"Account {account.email} auto-disabled: free tier with no credits"
                        )
                        return False

                # Low credits: mark as relaxed-only (still active, but only for relaxed models)
                if token_info.credits is not None and token_info.credits < 100:
                    if account.status != "relaxed_only":
                        await db.update_account(
                            account_id,
                            status="relaxed_only",
                        )
                        logger.warning(
                            f"Account {account.email} marked relaxed_only: {token_info.credits} credits left"
                        )
                    return True

                logger.info(
                    f"Token refreshed for account {account.email} "
                    f"(credits={token_info.credits}, tier={token_info.user_paygate_tier})"
                )
                return True

            except VeoClientError as e:
                logger.error(
                    f"Failed to refresh token for {account.email}: {e}"
                )

                # Handle specific errors
                if e.error_code == "UNAUTHORIZED":
                    await self.mark_reauth_required(account_id)
                    if account.refresh_token:
                        logger.warning(
                            f"Account {account.email} marked for re-auth: refresh_token revoked or expired"
                        )
                    else:
                        logger.warning(
                            f"Account {account.email} marked for re-auth: invalid ST"
                        )

                return False

    async def _rotate_proxy_and_refresh(
        self, account: "AccountDB", last_err: VeoClientError, max_attempts: int = 5
    ):
        """Try proxies from the pool until token exchange succeeds; save the working proxy."""
        pool = get_proxy_pool()
        for attempt in range(max_attempts):
            # Replacing the account's datacenter slot — never grab a
            # residential entry from the pool here, those are reserved
            # for explicit residential_proxy assignment.
            new_proxy = await pool.get_next_datacenter()
            if not new_proxy:
                break
            if new_proxy == account.proxy:
                # Skip the same broken proxy
                new_proxy = await pool.get_next_datacenter()
                if not new_proxy or new_proxy == account.proxy:
                    break
            logger.warning(
                f"Proxy rotation for {account.email}: attempt {attempt + 1}, "
                f"trying {new_proxy!r} (last error: {last_err})"
            )
            temp_client = VeoClient(capsolver=self.capsolver, proxy=new_proxy)
            try:
                if account.refresh_token:
                    token_info = await temp_client.exchange_refresh_token_for_at(account.refresh_token)
                else:
                    token_info = await temp_client.exchange_st_for_at(account.st)
                # Save the working proxy permanently
                await db.update_account(account.id, proxy=new_proxy)
                if new_proxy not in self._account_clients:
                    self._account_clients[new_proxy] = temp_client
                else:
                    await temp_client.close()
                logger.info(
                    f"Proxy rotated for {account.email}: {account.proxy!r} → {new_proxy!r} (saved)"
                )
                return token_info
            except VeoClientError as e:
                await temp_client.close()
                if not _is_proxy_error(str(e)):
                    raise
                last_err = e
        raise last_err

    async def mark_reauth_required(self, account_id: int) -> None:
        """Mark account as requiring re-authorization."""
        await db.update_account(
            account_id,
            status="needs_reauth",
            remark="Session token invalid or expired"
        )
        logger.info(f"Account {account_id} marked as needs_reauth")

    async def get_valid_account(self) -> Optional[AccountDB]:
        """
        Get a valid account with active token.

        Returns:
            Account with valid AT or None
        """
        accounts = await db.get_active_accounts()

        for account in accounts:
            if account.status != "active":
                continue

            # Check if token is valid
            if account.at and account.at_expires:
                if account.at_expires.timestamp() > time.time():
                    return account

            # Try to refresh
            if await self.refresh_account_token(account.id):
                return await db.get_account(account.id)

        return None

    async def record_success(self, account_id: int) -> None:
        """Record a successful generation for an account."""
        await db.increment_account_stats(account_id, success=True)
        await db.update_account_used(account_id)
        # Reset the consecutive UNUSUAL_ACTIVITY streak on any real success.
        self._unusual_streak.pop(account_id, None)

    async def record_error(
        self, account_id: int, error_code: Optional[str] = None
    ) -> None:
        """
        Record a failed generation for an account.

        May auto-disable if too many consecutive errors.
        """
        await db.increment_account_stats(account_id, success=False)
        await db.update_account_used(account_id)

        # Check for auto-ban
        stats = await db.get_account_stats(account_id)
        if stats and stats.consecutive_errors >= self.error_ban_threshold:
            account = await db.get_account(account_id)
            if account:
                await db.update_account(account_id, status="disabled", is_active=False)
                logger.warning(
                    f"Account {account.email} auto-disabled: "
                    f"{stats.consecutive_errors} consecutive errors"
                )

        # Handle specific errors
        if error_code == "RATE_LIMITED":
            deadline = datetime.fromtimestamp(time.time() + 3600)
            await db.update_account(
                account_id,
                status="rate_limited",
                is_active=False,
                cooldown_until=deadline,
            )
            account = await db.get_account(account_id)
            if account:
                logger.warning(
                    f"Account {account.email} rate-limited: disabled for 1 hour"
                )
            # Auto re-enable after 1 hour
            asyncio.create_task(self._reenable_after_cooldown_rate_limited(account_id, 3600))

    async def record_throttle(self, account_id: int) -> None:
        """Record a throttle hit. Disable account for 1 hour after threshold."""
        now = time.time()
        cutoff = now - self._throttle_window

        # Clean old entries and add new
        hits = self._throttle_hits.get(account_id, [])
        hits = [t for t in hits if t > cutoff]
        hits.append(now)
        self._throttle_hits[account_id] = hits

        account = await db.get_account(account_id)
        email = account.email if account else f"id={account_id}"

        logger.warning(f"Account {email} throttled ({len(hits)}/{self._throttle_threshold} in last hour)")

        if len(hits) >= self._throttle_threshold:
            deadline = datetime.fromtimestamp(time.time() + 3600)
            await db.update_account(
                account_id,
                status="throttled",
                is_active=False,
                cooldown_until=deadline,
            )
            logger.warning(f"Account {email} disabled for 1 hour: {len(hits)} throttle errors")
            # Clear hits counter
            self._throttle_hits.pop(account_id, None)
            # Schedule auto-re-enable after 1 hour
            asyncio.create_task(self._reenable_after_cooldown(account_id, 3600))

    async def record_unusual_activity(self, account_id: int) -> None:
        """Record a PUBLIC_ERROR_UNUSUAL_ACTIVITY hit. After ``_unusual_threshold``
        consecutive hits send the account to rest for ``_unusual_rest_seconds``.

        Streak resets on any successful generation (see ``record_success``).
        """
        streak = self._unusual_streak.get(account_id, 0) + 1
        self._unusual_streak[account_id] = streak

        account = await db.get_account(account_id)
        email = account.email if account else f"id={account_id}"
        logger.warning(
            f"Account {email} UNUSUAL_ACTIVITY streak {streak}/{self._unusual_threshold}"
        )

        if streak >= self._unusual_threshold:
            deadline = datetime.fromtimestamp(time.time() + self._unusual_rest_seconds)
            await db.update_account(
                account_id,
                status="resting",
                is_active=False,
                cooldown_until=deadline,
            )
            self._unusual_streak.pop(account_id, None)
            minutes = max(1, self._unusual_rest_seconds // 60)
            logger.warning(
                f"Account {email} resting for {minutes}m: {streak} consecutive UNUSUAL_ACTIVITY"
            )
            asyncio.create_task(
                self._reenable_after_cooldown(account_id, self._unusual_rest_seconds)
            )

    async def _reenable_after_cooldown(self, account_id: int, delay: int) -> None:
        """Re-enable an account after cooldown period (throttled or resting)."""
        await asyncio.sleep(delay)
        account = await db.get_account(account_id)
        if account and account.status in ("throttled", "resting"):
            await db.update_account(
                account_id, status="active", is_active=True, cooldown_until=None,
            )
            await db.reset_consecutive_errors(account_id)
            logger.info(
                f"Account {account.email} re-enabled after {delay}s "
                f"{account.status} cooldown"
            )

    async def _reenable_after_cooldown_rate_limited(self, account_id: int, delay: int) -> None:
        """Re-enable a rate-limited account after cooldown."""
        await asyncio.sleep(delay)
        account = await db.get_account(account_id)
        if account and account.status == "rate_limited":
            await db.update_account(
                account_id, status="active", is_active=True, cooldown_until=None,
            )
            await db.reset_consecutive_errors(account_id)
            logger.info(f"Account {account.email} re-enabled after {delay}s rate-limit cooldown")

    async def manual_cooldown(self, account_id: int, hours: float) -> bool:
        """Manually put an account into cooldown for ``hours`` hours.

        Sets status='resting' (matches the dashboard cooldown badge),
        is_active=False and cooldown_until = now + hours, then schedules
        the standard auto-reenable timer.
        """
        if hours <= 0:
            return False
        account = await db.get_account(account_id)
        if not account:
            return False
        delay = int(hours * 3600)
        deadline = datetime.fromtimestamp(time.time() + delay)
        await db.update_account(
            account_id,
            status="resting",
            is_active=False,
            cooldown_until=deadline,
        )
        # Wipe any partial streak so the manual cooldown is the canonical state.
        self._unusual_streak.pop(account_id, None)
        self._throttle_hits.pop(account_id, None)
        asyncio.create_task(self._reenable_after_cooldown(account_id, delay))
        logger.info(
            f"Account {account.email} manually placed in cooldown for {hours}h "
            f"(until {deadline.isoformat()})"
        )
        return True

    async def restore_cooldown_timers(self) -> None:
        """Re-arm cooldown re-enable timers after a service restart.

        Walks all accounts whose status is one of the cooldown states and
        either re-schedules :py:meth:`_reenable_after_cooldown` for the
        remaining duration, or — if the deadline already passed during
        downtime — re-enables the account immediately.
        """
        accounts = await db.get_all_accounts()
        now = time.time()
        rearmed = 0
        expired = 0
        for acc in accounts:
            if acc.status not in ("throttled", "resting", "rate_limited"):
                continue
            deadline = acc.cooldown_until
            if not deadline:
                # Legacy row from before cooldown_until was tracked — treat
                # as expired so the account isn't stuck forever.
                logger.info(
                    f"Account {acc.email} in {acc.status} without cooldown_until — releasing"
                )
                await db.update_account(
                    acc.id, status="active", is_active=True, cooldown_until=None,
                )
                await db.reset_consecutive_errors(acc.id)
                expired += 1
                continue
            remaining = deadline.timestamp() - now
            if remaining <= 0:
                await db.update_account(
                    acc.id, status="active", is_active=True, cooldown_until=None,
                )
                await db.reset_consecutive_errors(acc.id)
                logger.info(
                    f"Account {acc.email} cooldown ({acc.status}) expired during downtime — re-enabled"
                )
                expired += 1
            else:
                target_method = (
                    self._reenable_after_cooldown_rate_limited
                    if acc.status == "rate_limited"
                    else self._reenable_after_cooldown
                )
                asyncio.create_task(target_method(acc.id, int(remaining)))
                rearmed += 1
                logger.info(
                    f"Account {acc.email} cooldown re-armed: {int(remaining)}s remaining "
                    f"(status={acc.status})"
                )
        if rearmed or expired:
            logger.info(
                f"Cooldown restoration: {rearmed} timer(s) re-armed, {expired} expired"
            )

    async def enable_account(self, account_id: int) -> bool:
        """Enable an account."""
        account = await db.get_account(account_id)
        if not account:
            return False

        await db.update_account(
            account_id, status="active", is_active=True, cooldown_until=None,
        )
        await db.reset_consecutive_errors(account_id)
        # Clear throttle history on manual enable
        self._throttle_hits.pop(account_id, None)
        self._unusual_streak.pop(account_id, None)
        logger.info(f"Account {account.email} enabled")
        return True

    async def disable_account(self, account_id: int) -> bool:
        """Disable an account."""
        account = await db.get_account(account_id)
        if not account:
            return False

        await db.update_account(account_id, status="disabled", is_active=False)
        logger.info(f"Account {account.email} disabled")
        return True

    async def refresh_all_expiring(self, margin_seconds: int = 300, force: bool = False) -> int:
        """
        Refresh all accounts with tokens expiring soon.

        Args:
            margin_seconds: Refresh tokens expiring within this time
            force: Force refresh even if tokens are not expired

        Returns:
            Number of tokens refreshed
        """
        accounts = await db.get_active_accounts()
        refreshed = 0

        for account in accounts:
            should_refresh = force
            if not should_refresh and account.at_expires:
                if account.at_expires.timestamp() < time.time() + margin_seconds:
                    should_refresh = True
            
            if should_refresh:
                if await self.refresh_account_token(account.id, force=True):
                    refreshed += 1

        if refreshed > 0:
            logger.info(f"Refreshed {refreshed} expiring tokens")

        return refreshed

    async def sync_tokens_from_file(self, file_path: str) -> None:
        """
        Sync accounts from a JSON file (email -> st).
        """
        import json
        from pathlib import Path

        path = Path(file_path)
        if not path.exists():
            logger.warning(f"Auth tokens file not found: {path}")
            return

        try:
            with open(path, "r") as f:
                tokens = json.load(f)

            count = 0
            updated = 0
            skipped = 0
            for email, st in tokens.items():
                if not st:
                    continue

                existing = await db.get_account_by_email(email)
                if existing:
                    # auth_tokens.json is a bootstrap file. browser_login.py /
                    # the puppeteer auth path keep DB.st fresh on every login,
                    # but they DON'T write back to this file. So the file is
                    # almost always stale relative to the DB. Only let it
                    # overwrite the DB row when:
                    #   - DB has no usable st (empty / "pending_auth"), or
                    #   - account is explicitly marked needs_reauth /
                    #     check_required (operator manually pasted a fresh st
                    #     into the file to recover the account).
                    db_has_valid_st = bool(existing.st) and existing.st != "pending_auth"
                    needs_recovery = existing.status in ("needs_reauth", "check_required")
                    if db_has_valid_st and not needs_recovery and existing.st != st:
                        skipped += 1
                        logger.debug(
                            f"Token Sync: keeping fresher DB st for {email} "
                            f"(file st differs but DB looks healthy)"
                        )
                        continue

                    updates = {"st": st, "at": None, "at_expires": None}
                    if existing.st != st or needs_recovery:
                        updates["status"] = "active"
                        updates["is_active"] = True
                        updates["remark"] = "Restored via auth_tokens.json"

                    await db.update_account(existing.id, **updates)

                    # Reset cached VeoClient to clear stale session cookies
                    # Without this, the old session-token in the cookie jar
                    # overrides the new ST → 401 UNAUTHORIZED
                    await self.reset_client_for_proxy(existing.proxy)

                    updated += 1
                else:
                    # Add new account
                    try:
                        await self.add_account(
                            email=email,
                            st=st,
                            remark="Imported from auth_tokens.json"
                        )
                        count += 1
                    except Exception as e:
                        logger.error(f"Failed to import account {email}: {e}")

            logger.info(
                f"Token Sync: {count} added, {updated} updated, {skipped} kept "
                f"(DB fresher) from {path.name}"
            )

        except Exception as e:
            logger.error(f"Failed to sync tokens from file: {e}")

    async def sync_accounts_from_file(self, file_path: str) -> None:
        """
        Sync proxy settings from accounts.json file.
        
        Updates proxy field for existing accounts based on email match.
        """
        import json
        from pathlib import Path

        path = Path(file_path)
        if not path.exists():
            logger.warning(f"Accounts file not found: {path}")
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                accounts_data = json.load(f)

            if not isinstance(accounts_data, list):
                logger.error("accounts.json must be a list of account objects")
                return

            synced = 0
            for acc in accounts_data:
                email = acc.get("email")
                proxy = acc.get("proxy")
                residential_proxy_raw = acc.get("residential_proxy")
                residential_proxy = normalize_proxy_url(residential_proxy_raw)

                if not email:
                    continue

                existing = await db.get_account_by_email(email)
                if existing:
                    updates: dict = {}
                    # Update datacenter proxy if changed
                    if existing.proxy != proxy:
                        updates["proxy"] = proxy
                        logger.debug(f"Updated proxy for {email}")
                    # Update residential proxy only if accounts.json has a non-empty value.
                    # If empty/absent in file, keep whatever is already in DB (e.g. set via API).
                    if residential_proxy and existing.residential_proxy != residential_proxy:
                        updates["residential_proxy"] = residential_proxy
                        await self.reset_client_for_proxy(existing.residential_proxy)
                        logger.debug(f"Updated residential_proxy for {email}")
                    if updates:
                        await db.update_account(existing.id, **updates)
                        synced += 1

            if synced > 0:
                logger.info(f"Proxy Sync: {synced} accounts updated from {path.name}")

        except Exception as e:
            logger.error(f"Failed to sync accounts from file: {e}")
