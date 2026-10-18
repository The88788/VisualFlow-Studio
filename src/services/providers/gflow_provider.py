"""Google Flow provider backed by the maintained ``gflow`` CLI.

This provider exists for accounts migrated from ``labs.google`` to
``flow.google.com``.  Those accounts no longer expose the old NextAuth cookie
or the ``aisandbox-pa`` REST transport used by :mod:`veo_client` and the
vendored browser2api fork.  ``gflow`` drives a real, headed Chrome profile and
speaks Flow's current ``batchexecute`` transport.

The first production slice intentionally supports T2V only.  Unsupported
modes fail before a browser submit so they can never fall through to the stale
HTTP provider and accidentally spend credits in an unexpected way.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ...core.config import TMP_DIR, config as app_config
from ...core.database import db
from ...core.logger import logger
from ...core.models import VideoDB, VideoMode, VideoStatus
from ..storage import storage
from .gflow_browser_download import download_video_from_editor
from .gflow_utils import (
    GFlowProviderError,
    extract_result_json,
    map_model,
    select_recoverable_video,
)
from .gflow_runtime import prepare_profile, profile_lock


GFLOW_SUPPORTED_MODES = {VideoMode.T2V.value}


class GFlowProvider:
    """Serialize one authenticated gflow profile behind the MyFlow queue."""

    def __init__(self) -> None:
        # Exposed for compatibility with existing diagnostics. Images and
        # videos share the same underlying profile lock.
        self._profile_lock = profile_lock

    async def process_video(self, video_id: str) -> None:
        video = await db.get_video(video_id)
        if not video:
            logger.error("[GFlowProvider] Video %s not found", video_id)
            return
        if video.status != VideoStatus.PENDING.value:
            logger.info(
                "[GFlowProvider] Video %s is not pending (status=%s); skipping",
                video_id,
                video.status,
            )
            return

        if video.mode not in GFLOW_SUPPORTED_MODES:
            await self._fail(
                video_id,
                "INVALID_ARGUMENT",
                f"gflow provider currently supports T2V only (received mode={video.mode})",
            )
            return

        claimed = await db.try_claim_video_for_generation(
            video_id,
            None,
            progress_message="Generating through flow.google.com...",
        )
        if not claimed:
            return
        await db.update_video(video_id, provider="gflow", progress=5)

        try:
            async with self._profile_lock:
                await self._run_t2v(video)
        except asyncio.CancelledError:
            raise
        except GFlowProviderError as exc:
            logger.warning("[GFlowProvider] %s failed: %s", video_id, exc.message)
            await self._fail(video_id, exc.code, exc.message)
        except Exception as exc:  # pragma: no cover - last-resort boundary
            logger.exception("[GFlowProvider] Unexpected failure for %s", video_id)
            await self._fail(video_id, "GENERATION_FAILED", f"{type(exc).__name__}: {exc}")

    async def _run_t2v(self, video: VideoDB) -> None:
        prepare_profile(
            Path(app_config.gflow_home) / f"profile_{app_config.gflow_profile}"
        )
        model = map_model(video.model)
        duration = video.requested_duration or video.duration
        # Migrated Veo cohorts commonly expose a fixed 8-second length and no
        # duration control. Passing even ``--duration 8`` makes gflow refuse
        # the request pre-submit, so 8 means "use Flow's default" here.
        if model != "omni-flash" and duration not in (None, 8, 8.0):
            raise GFlowProviderError(
                "INVALID_ARGUMENT",
                f"Model {model} has a fixed 8-second duration on this Flow account",
            )

        output_dir = TMP_DIR / "gflow_videos" / video.video_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{video.video_id}.mp4"

        command = [
            app_config.gflow_executable,
            "video",
            "t2v",
            video.translated_prompt or video.prompt,
            "--profile",
            app_config.gflow_profile,
            "--ui-mode",
            "classic",
            "--model",
            model,
            "--count",
            "1",
            "--aspect",
            "9:16" if str(video.aspect_ratio) in ("9:16", "AspectRatio.PORTRAIT") else "16:9",
            "--output",
            str(output_path),
            "--json",
        ]
        if app_config.gflow_project_id:
            command.extend(["--project", app_config.gflow_project_id])
        if model == "omni-flash" and duration is not None:
            command.extend(["--duration", str(int(duration))])

        env = os.environ.copy()
        env.update(
            {
                "GFLOW_CLI_HOME": app_config.gflow_home,
                "GFLOW_CLI_PROFILE": app_config.gflow_profile,
                "GFLOW_CLI_HEADLESS": "false",
                "GFLOW_CLI_DEBUG_TRACEBACK": "1",
                "NO_COLOR": "1",
            }
        )

        logger.info(
            "[GFlowProvider] Starting T2V video=%s model=%s profile=%s project=%s",
            video.video_id,
            model,
            app_config.gflow_profile,
            app_config.gflow_project_id or "auto",
        )
        started_at = datetime.now(timezone.utc)
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=app_config.gflow_timeout
            )
        except asyncio.TimeoutError as exc:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            raise GFlowProviderError(
                "TIMEOUT", f"gflow generation exceeded {app_config.gflow_timeout}s"
            ) from exc

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if stderr_text:
            # gflow logs contain no cookie values, but can be very verbose; keep
            # them in the server log at debug level for diagnostics.
            logger.debug("[GFlowProvider:%s] %s", video.video_id, stderr_text[-12000:])
        result = extract_result_json(stdout_text)
        if proc.returncode != 0 or result.get("status") != "ok":
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            detail = str(error.get("detail") or error.get("title") or "gflow command failed")
            exit_code = error.get("exit_code", proc.returncode)
            recovered = None
            if exit_code in (1, 7):
                recovered = await self._recover_completed_video(
                    video=video,
                    prompt=video.translated_prompt or video.prompt,
                    started_at=started_at,
                    output_path=output_path,
                    env=env,
                )
            if recovered:
                result = {
                    "status": "ok",
                    "local_path": str(output_path),
                    "media_id": recovered["media_id"],
                    "project_id": recovered["project_id"],
                    "request": {"duration": duration or 8.0},
                }
            else:
                public_code = "INVALID_ARGUMENT" if exit_code in (2, 11, 23, 25, 28, 36) else "GENERATION_FAILED"
                if exit_code in (10, 38, 39):
                    public_code = "RATE_LIMITED"
                raise GFlowProviderError(public_code, detail)

        local_path = Path(str(result.get("local_path") or output_path))
        if not local_path.exists() or local_path.stat().st_size <= 0:
            raise GFlowProviderError(
                "GENERATION_FAILED", f"gflow reported success but output is missing: {local_path}"
            )

        storage_key = f"videos/{video.video_id}.mp4"
        await storage.upload_file(storage_key, local_path, content_type="video/mp4")
        project_url: Optional[str] = None
        if app_config.gflow_project_id:
            project_url = f"https://flow.google.com/project/{app_config.gflow_project_id}"
        request_data = result.get("request") if isinstance(result.get("request"), dict) else {}
        actual_duration = float(request_data.get("duration") or duration or 8.0)

        await db.update_video(
            video.video_id,
            status=VideoStatus.COMPLETED.value,
            progress=100,
            progress_message=None,
            download_url=f"{video.video_id}.mp4",
            video_url=str(result.get("media_id") or "") or None,
            provider="gflow",
            project_url=project_url,
            file_size=local_path.stat().st_size,
            duration=actual_duration,
            requested_resolution="720p",
            completed_at=datetime.utcnow(),
        )
        logger.info(
            "[GFlowProvider] Completed video=%s media_id=%s bytes=%d",
            video.video_id,
            result.get("media_id"),
            local_path.stat().st_size,
        )

    async def _recover_completed_video(
        self,
        *,
        video: VideoDB,
        prompt: str,
        started_at: datetime,
        output_path: Path,
        env: dict[str, str],
    ) -> dict[str, str] | None:
        """Recover a just-created clip when gflow fails during its download."""
        command = [
            app_config.gflow_executable,
            "data",
            "list",
            "videos",
            "--profile",
            app_config.gflow_profile,
            "--limit",
            "20",
            "--json",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                return None
            rows: list[dict[str, object]] = []
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
            match = select_recoverable_video(
                rows,
                prompt=prompt,
                started_at=started_at,
                project_id=app_config.gflow_project_id or None,
            )
            if not match:
                return None
            media_id = str(match["media_id"])
            project_id = str(match.get("project_id") or app_config.gflow_project_id or "")
            if not project_id:
                return None
            logger.warning(
                "[GFlowProvider] CLI download failed after generation; "
                "recovering media=%s through Flow UI",
                media_id,
            )
            await db.update_video(
                video.video_id,
                progress=90,
                progress_message="Downloading original 720p video from Flow...",
                video_url=media_id,
                project_url=f"https://flow.google.com/project/{project_id}/edit/{media_id}",
            )
            await download_video_from_editor(
                profile_dir=Path(app_config.gflow_home) / f"profile_{app_config.gflow_profile}",
                project_id=project_id,
                media_id=media_id,
                output_path=output_path,
            )
            return {"media_id": media_id, "project_id": project_id}
        except Exception as exc:
            logger.exception(
                "[GFlowProvider] Browser recovery failed for %s: %s",
                video.video_id,
                exc,
            )
            return None

    async def _fail(self, video_id: str, code: str, message: str) -> None:
        await db.update_video(
            video_id,
            status=VideoStatus.FAILED.value,
            progress_message=None,
            error_code=code,
            error_message=message,
        )


_provider: Optional[GFlowProvider] = None


def get_gflow_provider() -> GFlowProvider:
    global _provider
    if _provider is None:
        _provider = GFlowProvider()
    return _provider
