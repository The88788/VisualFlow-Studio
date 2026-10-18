"""Per-request proxy pool for video generation.

Reads a list of proxies from a file and serves them round-robin so that
each generate_video() call goes out through a different IP.

The account's own proxy is still used for token exchange, image upload,
polling, and upscaling — only the generation POST rotates.

Supported file formats (auto-detected per line):

  CSV (proxies.csv style):
      id,server,username,password,enabled
      ps26,http://1.2.3.4:8000,user,pass,true

  Plain URL:
      http://user:pass@host:port
      http://host:port

  Lines starting with # and the header line (id,server,...) are skipped.
"""

import asyncio
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from ..core.logger import logger


def _parse_line(line: str) -> Optional[str]:
    """Parse a single line into a proxy URL string, or return None to skip."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    parts = line.split(",")

    # CSV format: id,server,username,password,enabled[,...]
    if len(parts) >= 4:
        # Skip header row
        if parts[0].strip().lower() == "id":
            return None
        server = parts[1].strip()
        username = parts[2].strip()
        password = parts[3].strip()
        enabled = parts[4].strip().lower() if len(parts) > 4 else "true"
        if enabled == "false":
            return None
        if not server:
            return None
        # Inject credentials into server URL: http://host:port → http://user:pass@host:port
        if username and password:
            parsed = urlparse(server)
            server = parsed._replace(
                netloc=f"{username}:{password}@{parsed.hostname}:{parsed.port}"
            ).geturl()
        return server

    # Plain URL format
    if line.startswith("http://") or line.startswith("https://") or line.startswith("socks"):
        return line

    return None


def _is_residential_proxy(url: Optional[str]) -> bool:
    """True if the proxy URL points at a residential endpoint.

    Identified by the substring 'residential' in the URL (Evomi's
    naming: ``core-residential.evomi.com:1000:...``). Used by the
    pool to separate residential pool entries from datacenter ones —
    heavy bandwidth ops (image upload, status polling, post-error
    rotation) must NEVER pick a residential proxy from the pool, or
    the per-account residential traffic budget gets burned by the
    very ops we route through the pool to spare it.
    """
    if not url:
        return False
    return "residential" in url.lower()


class ProxyPool:
    """Round-robin proxy pool loaded from a file."""

    def __init__(self, pool_path: str):
        self._path = Path(pool_path)
        self._proxies: list[str] = []
        self._index: int = 0
        self._dc_index: int = 0
        self._mtime: float = 0.0
        self._lock = asyncio.Lock()
        # URLs the ProxyRotator has benched / put on cooldown — never handed
        # out for round-robin work until released. Kept in normalized form.
        self._unavailable: set[str] = set()

    def set_unavailable(self, urls: set[str]) -> None:
        """Replace the set of proxies excluded from round-robin selection.

        Called by :class:`ProxyRotator` whenever proxy cooldown state
        changes so burnt IPs stop receiving any pool traffic (image upload,
        status polling) — not just the per-account generation they were
        rotated out of.
        """
        self._unavailable = {u for u in urls if u}

    def _available(self, proxies: list[str]) -> list[str]:
        """Filter out proxies currently benched/cooled-down by the rotator."""
        if not self._unavailable:
            return proxies
        from .veo_client import normalize_proxy_url
        return [
            p for p in proxies
            if normalize_proxy_url(p) not in self._unavailable
        ]

    def _reload_if_changed(self) -> None:
        """Reload proxies from file if it has changed on disk."""
        if not self._path.exists():
            if self._proxies:
                logger.warning(f"ProxyPool: file {self._path} gone, clearing pool")
                self._proxies = []
            return

        mtime = self._path.stat().st_mtime
        if mtime == self._mtime:
            return

        lines = self._path.read_text(encoding="utf-8").splitlines()
        proxies = [p for line in lines if (p := _parse_line(line)) is not None]
        self._proxies = proxies
        self._mtime = mtime
        dc_count = sum(1 for p in proxies if not _is_residential_proxy(p))
        logger.info(
            f"ProxyPool: loaded {len(proxies)} proxies from {self._path} "
            f"({dc_count} datacenter, {len(proxies) - dc_count} residential)"
        )

    async def get_next(self) -> Optional[str]:
        """Return next proxy in round-robin order, or None if pool is empty.

        NOTE: this returns ANY proxy including residential ones. For
        heavy bandwidth operations call :meth:`get_next_datacenter`
        instead — residential proxies in the pool exist for niche
        cases (e.g. account whose datacenter proxy died and we need a
        fresh residential as a replacement) and shouldn't be used
        for routine high-traffic work.
        """
        async with self._lock:
            self._reload_if_changed()
            available = self._available(self._proxies)
            if not available:
                return None
            proxy = available[self._index % len(available)]
            self._index += 1
            return proxy

    async def get_next_datacenter(self) -> Optional[str]:
        """Return next non-residential proxy in round-robin order.

        Skips any pool entry whose URL contains "residential" (Evomi's
        host naming). Returns None if the pool has no datacenter
        proxies at all — caller decides whether to fall back to direct
        connection or to ``get_next()`` (which may return residential).
        """
        async with self._lock:
            self._reload_if_changed()
            dc_proxies = [
                p for p in self._available(self._proxies)
                if not _is_residential_proxy(p)
            ]
            if not dc_proxies:
                return None
            proxy = dc_proxies[self._dc_index % len(dc_proxies)]
            self._dc_index += 1
            return proxy

    @property
    def size(self) -> int:
        self._reload_if_changed()
        return len(self._proxies)


_instance: Optional[ProxyPool] = None


def get_proxy_pool() -> ProxyPool:
    """Return the global ProxyPool instance (initialized on first call)."""
    global _instance
    if _instance is None:
        from ..core.config import config
        _instance = ProxyPool(config.proxy_pool_path)
    return _instance
