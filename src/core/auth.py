"""Authentication module for VeoAPI - IP whitelist only."""

from fastapi import Request

from .config import config
from .logger import logger


def get_client_ip(request: Request) -> str:
    """Extract client IP from request, considering X-Forwarded-For."""
    # Check X-Forwarded-For header (from reverse proxy)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()

    # Check X-Real-IP header
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    # Fall back to direct connection
    return request.client.host if request.client else "unknown"


def verify_ip_whitelist(request: Request) -> bool:
    """
    Verify that the client IP is in the whitelist.

    Returns True if allowed, False if blocked.
    If whitelist is disabled, always returns True.
    """
    if not config.ip_whitelist_enabled:
        return True

    client_ip = get_client_ip(request)
    allowed = client_ip in _whitelist_set

    if not allowed:
        logger.warning(f"Blocked request from non-whitelisted IP: {client_ip}")

    return allowed


def reload_whitelist() -> None:
    """Reload whitelist from config."""
    global _whitelist_set
    _whitelist_set = set(config.ip_whitelist_ips)


# Initialize whitelist set for O(1) lookups
_whitelist_set: set[str] = set(config.ip_whitelist_ips)
