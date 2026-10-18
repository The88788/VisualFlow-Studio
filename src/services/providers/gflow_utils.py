"""Dependency-free helpers shared by the gflow provider and its tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any


_MODEL_MAP: dict[str, str] = {
    "veo31_fast": "veo-fast",
    "veo31_pro": "veo-quality",
    "veo3_fast": "veo-lite",
    "veo3_pro": "veo-quality",
    "veo3_quality": "veo-quality",
    "veo2_fast": "veo-lite-lp",
    "omni_flash": "omni-flash",
    "omni-flash": "omni-flash",
}


class GFlowProviderError(RuntimeError):
    """Typed provider failure carrying the public MyFlow error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def map_model(model: str) -> str:
    mapped = _MODEL_MAP.get((model or "").strip().lower())
    if not mapped:
        raise GFlowProviderError(
            "INVALID_ARGUMENT", f"Model {model!r} is not supported by the gflow provider"
        )
    return mapped


def extract_result_json(text: str) -> dict[str, Any]:
    """Extract the final JSON object from CLI stdout."""
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(raw[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not raw[idx + end :].strip():
            return value
    return {}


def select_recoverable_video(
    rows: list[dict[str, Any]],
    *,
    prompt: str,
    started_at: datetime,
    project_id: str | None,
) -> dict[str, Any] | None:
    """Select only a catalog row created by the current failed CLI run."""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    cutoff = started_at - timedelta(seconds=30)
    matches: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        if str(row.get("prompt") or "").strip() != prompt.strip():
            continue
        if project_id and str(row.get("project_id") or "") != project_id:
            continue
        if row.get("local_path"):
            continue
        media_id = str(row.get("media_id") or "")
        if not media_id:
            continue
        try:
            created_at = datetime.fromisoformat(
                str(row.get("created_at") or "").replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at >= cutoff:
            matches.append((created_at, row))
    return max(matches, key=lambda item: item[0])[1] if matches else None
