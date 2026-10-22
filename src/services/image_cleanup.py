"""Consistent cleanup for every local artifact owned by an image task."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..core.config import TMP_DIR
from .storage import storage


@dataclass(frozen=True)
class ImageCleanupResult:
    """Counts removed from persistent storage and the temporary workspace."""

    result_files: int
    reference_files: int
    temporary_files: int


def _work_dir(image_id: str) -> Path:
    """Return a task-scoped work directory without permitting path traversal."""
    if not image_id or Path(image_id).name != image_id or image_id in {".", ".."}:
        raise ValueError("Invalid image task id")
    return TMP_DIR / "gflow_images" / image_id


async def delete_image_artifacts(image_id: str) -> ImageCleanupResult:
    """Delete results, references and temporary files for one image task.

    Every location is attempted even if another backend operation fails.  The
    caller must delete the database row only after this function succeeds, so
    a partial storage failure remains visible and can be retried safely.
    """
    # Validate before forming either filesystem paths or object-storage keys.
    work_dir = _work_dir(image_id)
    failures: list[tuple[str, Exception]] = []
    result_files = 0
    reference_files = 0
    temporary_files = 0

    try:
        result_files = await storage.delete_prefix(f"images/{image_id}")
    except Exception as exc:  # noqa: BLE001 - aggregate cleanup failures
        failures.append(("results", exc))

    try:
        reference_files = await storage.delete_prefix(f"references/{image_id}")
    except Exception as exc:  # noqa: BLE001 - aggregate cleanup failures
        failures.append(("references", exc))

    try:
        if work_dir.is_dir():
            temporary_files = sum(1 for path in work_dir.rglob("*") if path.is_file())
            await asyncio.to_thread(shutil.rmtree, work_dir)
    except Exception as exc:  # noqa: BLE001 - aggregate cleanup failures
        failures.append(("temporary files", exc))

    if failures:
        detail = "; ".join(f"{location}: {error}" for location, error in failures)
        raise RuntimeError(f"Image artifact cleanup failed for {image_id}: {detail}")

    return ImageCleanupResult(result_files, reference_files, temporary_files)
