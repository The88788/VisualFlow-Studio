#!/usr/bin/env python3
"""Interactive script to launch any Chrome browser profile under VNC/Xvfb on the server."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# Make "src.*" importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import config as app_config  # noqa: E402
from src.core.database import db  # noqa: E402
from src.services.veo_client import normalize_proxy_url  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("interactive_vnc")

ACCOUNTS_JSON = ROOT / "config" / "accounts.json"


def load_accounts_file() -> dict[str, dict]:
    """Return {email: entry} from config/accounts.json."""
    if not ACCOUNTS_JSON.exists():
        log.error("accounts.json not found at %s", ACCOUNTS_JSON)
        return {}
    data = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8"))
    return {entry["email"]: entry for entry in data if entry.get("email")}


def mask_proxy(url: str | None) -> str:
    if not url:
        return "none (direct)"
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.hostname}:{p.port}"
    except Exception:
        return "<set>"


def find_chrome_executable() -> str:
    for candidate in ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]:
        if shutil.which(candidate):
            return candidate
    return "google-chrome"


async def check_command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


async def ensure_vnc_server() -> bool:
    """Check and start Xvfb, fluxbox, and x11vnc on display :99 / port 5999."""
    # Verify prerequisites are installed
    for cmd in ["Xvfb", "fluxbox", "x11vnc"]:
        if not await check_command_exists(cmd):
            log.error(
                "Prerequisite '%s' is not installed. Please run: "
                "sudo apt install -y xvfb fluxbox x11vnc",
                cmd,
            )
            return False

    # Ensure any old Xvfb on display :99 is stopped so we can start fresh with access control disabled (-ac)
    lock_file = Path("/tmp/.X99-lock")
    socket_path = Path("/tmp/.X11-unix/X99")

    if lock_file.exists() or socket_path.exists():
        if lock_file.exists():
            try:
                content = lock_file.read_text().strip()
                pid = int(content.split()[0])
                log.info(f"Stopping old Xvfb process (PID {pid}) to ensure fresh -ac start...")
                os.kill(pid, 15)  # SIGTERM
                # Wait up to 3 seconds for it to exit
                for _ in range(30):
                    await asyncio.sleep(0.1)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    # Force kill if still running
                    os.kill(pid, 9)
            except Exception:
                pass

        # Clean up stale locks/sockets
        try:
            lock_file.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            socket_path.unlink(missing_ok=True)
        except Exception:
            pass

    # Stop any old x11vnc and fluxbox processes to make sure port 5999 and display :99 are completely free
    try:
        log.info("Stopping any old x11vnc and fluxbox processes...")
        subprocess.run(["pkill", "-f", "x11vnc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-f", "fluxbox"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    log.info("Starting fresh Xvfb on display :99 with access control disabled (-ac)...")
    subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-ac"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )
    await asyncio.sleep(1.5)

    # Start fluxbox on display :99
    log.info("Starting fluxbox window manager on display :99...")
    subprocess.Popen(
        ["fluxbox"],
        env={**os.environ, "DISPLAY": ":99"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )

    # Check if VNC server port 5999 is bound
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 5999))
        s.close()
        # Port is free, start x11vnc
        log.info("Starting x11vnc on port 5999...")
        subprocess.Popen(
            [
                "x11vnc",
                "-display",
                ":99",
                "-nopw",
                "-listen",
                "localhost",
                "-shared",
                "-forever",
                "-rfbport",
                "5999",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        await asyncio.sleep(1.5)
    except socket.error:
        # Port is already bound, x11vnc is likely already running
        log.info("VNC server is already running on port 5999.")

    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description="Interactively launch a Chrome profile under VNC/Xvfb")
    parser.add_argument("--id", type=int, help="Pre-select account ID")
    args = parser.parse_args()

    # Load accounts
    await db.connect()
    try:
        accounts = await db.get_all_accounts()
        if not accounts:
            log.error("No accounts found in database.")
            return 1

        file_idx = load_accounts_file()

        # Select target account
        selected_acc = None
        if args.id:
            for acc in accounts:
                if acc.id == args.id:
                    selected_acc = acc
                    break
            if not selected_acc:
                log.error("Account ID %d not found.", args.id)
                return 1
        else:
            print("\n=== Accounts ===")
            for idx, acc in enumerate(accounts, 1):
                status_str = "READY" if acc.browser_ready else "PENDING"
                active_str = "Active" if acc.is_active else "Inactive"
                print(f"[{idx}] {acc.email} (ID: {acc.id}, Status: {status_str}, {active_str}, GoogleStatus: {acc.status})")
            
            while True:
                try:
                    choice = input("\nSelect account number (or 'q' to quit): ").strip()
                    if choice.lower() == 'q':
                        return 0
                    c_idx = int(choice)
                    if 1 <= c_idx <= len(accounts):
                        selected_acc = accounts[c_idx - 1]
                        break
                    else:
                        print("Invalid choice, try again.")
                except ValueError:
                    print("Please enter a valid number.")

        email = selected_acc.email
        entry = file_idx.get(email)
        if not entry:
            log.warning("[%s] password/proxy entry not found in accounts.json — proceeding with default info", email)
            entry = {}

        # Profile path
        profile_root = Path(app_config.browser_profile_root or "/opt/veoapi_browser_profiles")
        profile_path = profile_root / f"{selected_acc.id}_puppeteer"
        profile_path.mkdir(parents=True, exist_ok=True)

        # Determine proxy
        raw_proxy = entry.get("residential_proxy") or entry.get("proxy") or ""
        proxy_url = normalize_proxy_url(raw_proxy) if raw_proxy else None

        log.info("[%s] Selected account (ID: %d)", email, selected_acc.id)
        log.info("[%s] Profile path: %s", email, profile_path)
        log.info("[%s] Configured proxy: %s", email, mask_proxy(proxy_url))

        # Ensure Xvfb and VNC are running
        if not await ensure_vnc_server():
            return 1

        chrome_bin = find_chrome_executable()
        log.info("[%s] Using Chrome binary: %s", email, chrome_bin)

        # Parse proxy URL for Chrome compatibility
        chrome_proxy = None
        proxy_user = None
        proxy_pass = None
        if proxy_url:
            from urllib.parse import urlparse
            url_to_parse = proxy_url
            if "://" not in proxy_url:
                url_to_parse = "http://" + proxy_url
            try:
                p = urlparse(url_to_parse)
                scheme = p.scheme or "http"
                host = p.hostname
                port = p.port
                proxy_user = p.username
                proxy_pass = p.password
                if host and port:
                    chrome_proxy = f"{scheme}://{host}:{port}"
            except Exception:
                chrome_proxy = proxy_url

        chrome_args = [
            chrome_bin,
            f"--user-data-dir={profile_path}",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-setuid-sandbox",
            "--window-size=1280,720",
        ]
        if chrome_proxy:
            chrome_args.append(f"--proxy-server={chrome_proxy}")

        # Get server hostname or IP for instruction block
        import urllib.request
        try:
            public_ip = urllib.request.urlopen('https://api.ipify.org', timeout=3.0).read().decode('utf8')
        except Exception:
            public_ip = "YOUR_SERVER_IP"

        print("\n" + "=" * 80)
        print(" CHROME LAUNCHED ON DISPLAY :99 / VNC PORT 5999")
        print("=" * 80)
        print(f" Account: {email}")
        print(f" Profile: {profile_path}")
        print(f" Proxy:   {chrome_proxy or 'none'}")
        if proxy_user and proxy_pass:
            print(f" Proxy Credentials:")
            print(f"   Username: {proxy_user}")
            print(f"   Password: {proxy_pass}")
        print("-" * 80)
        print(" HOW TO CONNECT FROM YOUR WINDOWS MACHINE:")
        print(" 1. Run this command in PowerShell/CMD on your Windows PC:")
        print(f"\n    ssh -L 5900:localhost:5999 user@{public_ip}\n")
        print(" 2. Open VNC Viewer on Windows and connect to:")
        print("\n    localhost:5900\n")
        print(" 3. When finished, close the Chrome browser window (or press Ctrl+C here)")
        print("=" * 80 + "\n")

        # Launch Chrome on display :99
        process = await asyncio.create_subprocess_exec(
            *chrome_args,
            env={**os.environ, "DISPLAY": ":99"},
        )
        try:
            await process.wait()
            log.info("Chrome process closed.")
        except asyncio.CancelledError:
            log.info("Interrupted. Terminating Chrome...")
            try:
                process.terminate()
                await process.wait()
            except Exception:
                pass

    finally:
        await db.close()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nTerminated.")
        sys.exit(0)
