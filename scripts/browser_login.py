#!/usr/bin/env python3
"""One-shot browser-login tool for the browser2api (Flow UI) provider.

Reads credentials from ``config/accounts.json``, fires up Chrome per account
with the account's proxy and a fresh user-data-dir under
``config.browser_profile_root / <account_id>_puppeteer``, drives the full
Google login flow via ``FlowAuthenticator``, and persists the resulting
``__Secure-next-auth.session-token`` back into the ``accounts`` table with
``browser_ready=1``.

Usage::

    # all active accounts where browser_ready=0
    xvfb-run -a .venv/bin/python scripts/browser_login.py

    # just one account by id
    xvfb-run -a .venv/bin/python scripts/browser_login.py --id 53

    # force re-login including accounts that already have browser_ready=1
    xvfb-run -a .venv/bin/python scripts/browser_login.py --all

This is a separate profile tree from the HTTP (veo) provider — HTTP path
doesn't touch ``/opt/veoapi_browser_profiles/`` at all.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Make "src.*" importable when run as ``python scripts/browser_login.py``
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import config as app_config  # noqa: E402
from src.core.database import db  # noqa: E402
from src.services.providers import _VENDOR_SRC  # noqa: F401,E402  (path injection)
from src.services.veo_client import normalize_proxy_url  # noqa: E402

from browser2api import BrowserManager  # noqa: E402
from browser2api.platforms.flow import login_and_get_session_token  # noqa: E402
from browser2api.platforms.flow.exceptions import (  # noqa: E402
    AccountDisabledError,
    CaptchaRequiredError,
    FlowAuthError,
    LoginFailedError,
    SessionTokenNotFoundError,
    TwoFactorRequiredError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("browser_login")
log.setLevel(logging.DEBUG)

# Re-route browser_login's own logger through the veoapi handlers (same
# trick _attach_browser2api_logging does for browser2api/playwright).
# Without this, when run as a subprocess of the FastAPI service the logs
# vanish — basicConfig is a no-op once src.core.logger has been imported,
# because root remains handler-less but pre-existing 'veoapi' handlers
# are attached to a NAMED logger, not to root. End result: every
# log.info/log.exception call from login_one is silently dropped, which
# is exactly how a re-auth subprocess can exit with code=1 leaving
# zero diagnostic trail.
try:
    from src.core.logger import logger as _veoapi_logger  # noqa: E402

    _existing_handler_ids = {id(h) for h in log.handlers}
    for _h in _veoapi_logger.handlers:
        if id(_h) not in _existing_handler_ids:
            log.addHandler(_h)
    log.propagate = False
except Exception:
    # If the import fails for any reason, fall back to the basicConfig
    # stderr handler — better than nothing.
    pass

ACCOUNTS_JSON = ROOT / "config" / "accounts.json"


def load_accounts_file() -> dict[str, dict]:
    """Return ``{email: entry}`` from config/accounts.json."""
    if not ACCOUNTS_JSON.exists():
        log.error("accounts.json not found at %s", ACCOUNTS_JSON)
        return {}
    data = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8"))
    return {entry["email"]: entry for entry in data if entry.get("email")}


def mask_proxy(url: str | None) -> str:
    if not url:
        return "none"
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.hostname}:{p.port}"
    except Exception:
        return "<set>"


_PROXY_ERROR_MARKERS: tuple[str, ...] = (
    "err_too_many_retries",
    "err_proxy_connection_failed",
    "err_tunnel_connection_failed",
    "err_proxy_auth_unsupported",
    "err_no_supported_proxies",
    "err_proxy_certificate_invalid",
    "net::err_connection_reset",
    "net::err_connection_refused",
    "net::err_connection_closed",
    "net::err_timed_out",
    "net::err_name_not_resolved",
)


def _is_proxy_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _PROXY_ERROR_MARKERS)


async def login_one(account_db, file_entry: dict, profile_root: Path) -> bool:
    """Log in ONE account and persist ST + browser_ready=1. Returns True on success.

    Tries proxy candidates in order — residential first, datacenter
    second, direct (no proxy) last. A bad/dead proxy on the first
    attempt manifests as ``net::ERR_TOO_MANY_RETRIES`` (or similar
    proxy-flavoured Chromium errors) on the very first ``page.goto``,
    long before any login UI is visible. Without rotation a single
    flaky proxy bricks the account; with rotation we recover transparently.
    """
    if not file_entry.get("password"):
        log.error("[%s] no password in accounts.json — skipping", account_db.email)
        return False

    profile_path = profile_root / f"{account_db.id}_puppeteer"
    profile_path.mkdir(parents=True, exist_ok=True)

    debug_dir = (
        ROOT / "logs" / "browser_login" / f"{account_db.id}_{datetime.utcnow():%Y%m%d_%H%M%S}"
    )

    # Build the proxy candidate chain: residential → datacenter → direct.
    # Skip empty / duplicate entries.
    candidates: list[tuple[str, str | None]] = []
    seen: set[str | None] = set()
    for tag, raw in (
        ("residential", file_entry.get("residential_proxy") or ""),
        ("datacenter", file_entry.get("proxy") or ""),
    ):
        if not raw:
            continue
        norm = normalize_proxy_url(raw)
        if norm in seen:
            continue
        seen.add(norm)
        candidates.append((tag, norm))

    if None not in seen:
        candidates.append(("direct", None))

    account_dict = {
        "email": file_entry["email"],
        "password": file_entry["password"],
        "otp_secret": (file_entry.get("otp_secret") or "").strip(),
        "profile_path": str(profile_path),
        "name": file_entry.get("name") or file_entry["email"],
    }

    last_exc: BaseException | None = None
    st: str | None = None

    for tag, proxy_url in candidates:
        log.info(
            "[%s] Logging in via %s proxy → profile=%s proxy=%s debug=%s",
            account_db.email, tag, profile_path, mask_proxy(proxy_url), debug_dir,
        )

        manager = BrowserManager(
            data_dir=profile_path.parent,
            proxy_url=proxy_url,
            use_cdp=True,
            user_data_dir_override=profile_path,
        )

        async def dump_final_screenshot(tag2: str) -> None:
            """Capture page state at the moment of failure so the user can see
            what Chrome actually rendered."""
            try:
                ctx = manager._contexts.get("flow")
                if not ctx or not ctx.pages:
                    return
                debug_dir.mkdir(parents=True, exist_ok=True)
                for i, pg in enumerate(ctx.pages):
                    png = debug_dir / f"99_final_{tag2}_{tag}_tab{i}.png"
                    try:
                        await pg.screenshot(path=str(png), full_page=True)
                        html = await pg.content()
                        (debug_dir / f"99_final_{tag2}_{tag}_tab{i}.html").write_text(
                            html, encoding="utf-8"
                        )
                        (debug_dir / f"99_final_{tag2}_{tag}_tab{i}.url.txt").write_text(
                            pg.url or "", encoding="utf-8"
                        )
                        log.error(
                            "[%s] final screenshot (proxy=%s tab%d url=%s) saved → %s",
                            account_db.email, tag, i, pg.url, png,
                        )
                    except Exception as e:
                        log.debug("screenshot tab%d failed: %s", i, e)
            except Exception as e:
                log.debug("dump_final_screenshot failed: %s", e)

        try:
            st = await login_and_get_session_token(
                account=account_dict,
                browser_manager=manager,
                debug_dir=debug_dir,
                try_reauth_first=True,
            )
            # Give Chrome time to commit cookies to the profile's SQLite store.
            log.info(
                "[%s] login ok via %s proxy, letting Chrome flush cookies before close...",
                account_db.email, tag,
            )
            await asyncio.sleep(10)
            last_exc = None
            break  # success — stop trying further proxies
        except AccountDisabledError as e:
            log.error("[%s] ACCOUNT DISABLED: %s", account_db.email, e)
            await dump_final_screenshot("disabled")
            await db.update_account(account_db.id, status="banned", is_active=False)
            try:
                await manager.close()
            except Exception:
                pass
            return False
        except (
            LoginFailedError,
            CaptchaRequiredError,
            TwoFactorRequiredError,
            SessionTokenNotFoundError,
            FlowAuthError,
        ) as e:
            log.error(
                "[%s] login failed via %s proxy: %s: %s",
                account_db.email, tag, type(e).__name__, e,
            )
            await dump_final_screenshot("login_failed")
            try:
                await manager.close()
            except Exception:
                pass
            # These are real auth errors — rotating proxies won't help.
            return False
        except Exception as e:
            last_exc = e
            if _is_proxy_error(e):
                log.warning(
                    "[%s] proxy %s failed (%s: %s) — will try next candidate",
                    account_db.email, tag, type(e).__name__,
                    str(e).split("\n", 1)[0],
                )
            else:
                log.exception("[%s] unexpected error via %s proxy", account_db.email, tag)
            await dump_final_screenshot("unexpected")
            try:
                await manager.close()
            except Exception:
                pass
            # Only rotate to the next proxy on proxy-shaped errors. Other
            # unexpected errors (selector not found, etc.) are not going
            # to be fixed by a different proxy.
            if not _is_proxy_error(e):
                return False
            continue

    if last_exc is not None and not st:
        log.error(
            "[%s] all %d proxy candidate(s) failed — last error: %s: %s",
            account_db.email, len(candidates), type(last_exc).__name__, last_exc,
        )
        return False

    if not st:
        log.error("[%s] no session token returned", account_db.email)
        return False

    status_update: dict = {}

    status_update.update(
        st=st,
        browser_ready=True,
        browser_last_check=datetime.utcnow(),
    )
    await db.update_account(account_db.id, **status_update)

    # Mirror the fresh ST into config/auth_tokens.json so a service restart
    # cannot clobber DB.st with a stale value from this bootstrap file.
    try:
        from src.core.config import CONFIG_PATH
        tokens_path = Path(CONFIG_PATH).parent / "auth_tokens.json"
        if tokens_path.exists():
            existing_tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
        else:
            existing_tokens = {}
        if existing_tokens.get(account_db.email) != st:
            existing_tokens[account_db.email] = st
            tokens_path.write_text(
                json.dumps(existing_tokens, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info("[%s] Mirrored fresh ST → %s", account_db.email, tokens_path)
    except Exception as e:
        log.warning("[%s] Could not mirror ST to auth_tokens.json: %s", account_db.email, e)

    log.info("[%s] SUCCESS — ST stored, browser_ready=1", account_db.email)
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description="Log in accounts for the browser provider")
    parser.add_argument("--id", type=int, help="log in only this account id")
    parser.add_argument(
        "--all",
        action="store_true",
        help="include accounts that already have browser_ready=1 (force re-login)",
    )
    args = parser.parse_args()

    await db.connect()
    rc = 0
    try:
        if args.id:
            acc = await db.get_account(args.id)
            if not acc:
                log.error("account id=%s not found", args.id)
                return 2
            targets = [acc]
        else:
            active = await db.get_active_accounts()
            targets = active if args.all else [a for a in active if not a.browser_ready]

        if not targets:
            log.info("nothing to do — no matching accounts")
            return 0

        file_idx = load_accounts_file()
        if not file_idx:
            return 2

        profile_root = Path(
            app_config.browser_profile_root or "/opt/veoapi_browser_profiles"
        )
        profile_root.mkdir(parents=True, exist_ok=True)

        log.info(
            "will log in %d account(s) into %s: %s",
            len(targets), profile_root, [a.email for a in targets],
        )

        ok = 0
        for acc in targets:
            entry = file_idx.get(acc.email)
            if not entry:
                log.error("[%s] not found in accounts.json — skipping", acc.email)
                continue
            try:
                if await login_one(acc, entry, profile_root):
                    ok += 1
            except KeyboardInterrupt:
                log.warning("interrupted")
                break
            except Exception:
                log.exception("[%s] login_one crashed", acc.email)

        log.info("=== Done: %d/%d succeeded ===", ok, len(targets))
        rc = 0 if ok == len(targets) else 1
    finally:
        await db.close()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
