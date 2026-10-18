"""TOTP (RFC 6238) generator compatible with Google Authenticator.

Pure stdlib — no external dependencies. Accepts base32-encoded secrets,
returns a 6-digit code valid for the current 30-second window.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import struct
import time


def generate_totp(
    secret_base32: str,
    time_step: int = 30,
    digits: int = 6,
    timestamp: float | None = None,
) -> str:
    """Generate a TOTP code from a base32-encoded shared secret.

    Args:
        secret_base32: Base32 secret (as displayed when adding to Authenticator).
                       Case-insensitive, spaces/padding tolerated.
        time_step: Time step in seconds (default 30 per Google Authenticator).
        digits: Number of digits in the output code (default 6).
        timestamp: Unix timestamp to use (default: now). Useful for testing.

    Returns:
        Zero-padded numeric string of length ``digits``.
    """
    # Normalize secret: strip whitespace, uppercase, add padding
    cleaned = secret_base32.replace(" ", "").replace("-", "").upper()
    pad = (-len(cleaned)) % 8
    cleaned += "=" * pad

    try:
        key = base64.b32decode(cleaned, casefold=True)
    except Exception as e:
        raise ValueError(f"Invalid base32 TOTP secret: {e}") from e

    if timestamp is None:
        timestamp = time.time()

    counter = int(timestamp) // time_step
    counter_bytes = struct.pack(">Q", counter)

    digest = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = (
        (digest[offset] & 0x7F) << 24
        | (digest[offset + 1] & 0xFF) << 16
        | (digest[offset + 2] & 0xFF) << 8
        | (digest[offset + 3] & 0xFF)
    )
    code = truncated % (10 ** digits)
    return str(code).zfill(digits)
