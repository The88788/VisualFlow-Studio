"""Shared runtime primitives for one authenticated gflow Chrome profile."""

import asyncio
import os
from pathlib import Path

from ...core.logger import logger


# Chrome persistent profiles are exclusively locked. Images and videos must
# share one process-local lock, not one lock per provider.
profile_lock = asyncio.Lock()


def prepare_profile(profile_dir: Path) -> None:
    """Remove Chrome singleton markers only when no live process owns this profile.

    Docker assigns a new hostname to every recreated container. Chrome's
    ``SingletonLock`` therefore looks like a lock held on another computer
    after an otherwise clean deployment. The files are safe to remove only
    when no process in the current container has this exact profile path in
    its command line. The process-local ``profile_lock`` serializes callers.
    """
    profile_text = str(profile_dir)
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            command = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if ("chrome" in command or "chromium" in command) and profile_text in command:
            return

    removed: list[str] = []
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        marker = profile_dir / name
        if os.path.lexists(marker):
            marker.unlink(missing_ok=True)
            removed.append(name)
    if removed:
        logger.warning(
            "[GFlowRuntime] Removed stale Chrome profile markers: %s",
            ", ".join(removed),
        )
