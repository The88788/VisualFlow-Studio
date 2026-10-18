"""Text-to-image generation through the authenticated flow.google.com profile."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import shutil
import signal
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ...core.config import TMP_DIR, config as app_config
from ...core.database import db
from ...core.logger import logger
from ..storage import storage
from .gflow_image_download import download_image_from_editor
from .gflow_runtime import prepare_profile, profile_lock
from .gflow_utils import GFlowProviderError, extract_result_json


IMAGE_MODELS = frozenset(("nano2", "nano-pro", "image4", "imagen4"))
IMAGE_ASPECTS = frozenset(("9:16", "16:9", "1:1", "4:3", "3:4"))


class GFlowImageProvider:
    """Run image jobs serially against the same Chrome profile as video."""

    async def process(self, image_id: str) -> None:
        task = await db.get_image_task(image_id)
        if not task or task["status"] != "pending":
            return
        if not await db.try_claim_image_task(image_id):
            return

        completed = False
        try:
            async with profile_lock:
                await self._run(task)
            completed = True
        except asyncio.CancelledError:
            raise
        except GFlowProviderError as exc:
            await self._fail(image_id, exc.code, exc.message)
        except Exception as exc:  # pragma: no cover - last-resort boundary
            logger.exception("[GFlowImageProvider] Unexpected failure for %s", image_id)
            await self._fail(image_id, "GENERATION_FAILED", f"{type(exc).__name__}: {exc}")
        finally:
            # Successful inputs are disposable. Failed inputs must remain until
            # normal retention cleanup so the dashboard can retry the exact same
            # request without asking the designer to upload everything again.
            if completed:
                await storage.delete_prefix(f"references/{image_id}")

    async def _run(self, task: dict[str, Any]) -> None:
        image_id = str(task["image_id"])
        prepare_profile(
            Path(app_config.gflow_home) / f"profile_{app_config.gflow_profile}"
        )
        output_dir = TMP_DIR / "gflow_images" / image_id
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            reference_keys = json.loads(task.get("reference_files") or "[]")
        except (json.JSONDecodeError, TypeError):
            reference_keys = []
        reference_paths: list[Path] = []
        for index, key in enumerate(reference_keys if isinstance(reference_keys, list) else []):
            suffix = Path(str(key)).suffix.lower() or ".png"
            local_reference = output_dir / "references" / f"reference-{index + 1}{suffix}"
            local_reference.parent.mkdir(parents=True, exist_ok=True)
            local_reference.write_bytes(await storage.download(str(key)))
            reference_paths.append(local_reference)
        command = [
            app_config.gflow_executable,
            "image",
            "i2i" if reference_paths else "t2i",
            str(task["prompt"]),
            "--profile",
            app_config.gflow_profile,
            "--ui-mode",
            "classic",
            "--model",
            str(task["model"]),
            "--aspect",
            str(task["aspect_ratio"]),
            "--count",
            str(task["requested_count"]),
            "--out",
            str(output_dir),
            "--json",
        ]
        for reference_path in reference_paths:
            command.extend(["--ref", str(reference_path)])
        if app_config.gflow_project_id:
            command.extend(["--project", app_config.gflow_project_id])

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
            "[GFlowImageProvider] Starting image=%s model=%s count=%s",
            image_id,
            task["model"],
            task["requested_count"],
        )
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
            await self._terminate(proc)
            raise GFlowProviderError(
                "TIMEOUT", f"Image generation exceeded {app_config.gflow_timeout}s"
            ) from exc

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if stderr_text:
            logger.debug("[GFlowImageProvider:%s] %s", image_id, stderr_text[-12000:])
        result = extract_result_json(stdout_text)
        if proc.returncode != 0 or result.get("status") != "ok":
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            detail = str(error.get("detail") or error.get("title") or "gflow image command failed")
            exit_code = error.get("exit_code", proc.returncode)
            code = "INVALID_ARGUMENT" if exit_code in (2, 11, 23, 25, 28, 36) else "GENERATION_FAILED"
            if exit_code in (10, 38, 39):
                code = "RATE_LIMITED"
            raise GFlowProviderError(code, detail)

        images = result.get("images")
        if not isinstance(images, list) or not images:
            raise GFlowProviderError("GENERATION_FAILED", "gflow returned no images")

        stored: list[str] = []
        metadata: list[dict[str, Any]] = []
        for index, item in enumerate(images):
            if not isinstance(item, dict):
                continue
            local_path = Path(str(item.get("local_path") or ""))
            if not local_path.is_file() or local_path.stat().st_size <= 0:
                raise GFlowProviderError(
                    "GENERATION_FAILED", f"Generated image file is missing: {local_path}"
                )
            storage_key = f"images/{image_id}/{index + 1}.png"
            await storage.upload_file(storage_key, local_path, content_type="image/png")
            stored.append(storage_key)
            dimensions = item.get("dimensions") if isinstance(item.get("dimensions"), dict) else {}
            metadata.append(
                {
                    "index": index,
                    "media_name": item.get("media_name"),
                    "workflow_id": item.get("workflow_id"),
                    "seed": item.get("seed"),
                    "width": dimensions.get("width"),
                    "height": dimensions.get("height"),
                    "file_size": local_path.stat().st_size,
                }
            )

        if not stored:
            raise GFlowProviderError("GENERATION_FAILED", "No generated image files were saved")
        await db.update_image_task(
            image_id,
            status="completed",
            progress=100,
            result_files=json.dumps(stored),
            result_metadata=json.dumps(metadata),
            completed_at=datetime.utcnow(),
        )
        logger.info("[GFlowImageProvider] Completed image=%s count=%d", image_id, len(stored))

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

    async def _fail(self, image_id: str, code: str, message: str) -> None:
        logger.warning("[GFlowImageProvider] %s failed: %s", image_id, message)
        await db.update_image_task(
            image_id,
            status="failed",
            progress=0,
            error_code=code,
            error_message=message,
        )

    async def upscale(self, image_id: str, index: int, scale: str = "2k") -> None:
        """Create and persist an on-demand Flow upscale for one generated image."""
        try:
            async with profile_lock:
                await self._run_upscale(image_id, index, scale)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep the original image downloadable on failure
            logger.exception(
                "[GFlowImageProvider] Upscale failed image=%s index=%s scale=%s",
                image_id,
                index,
                scale,
            )
            await self._set_upscale_state(
                image_id,
                index,
                scale,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            shutil.rmtree(
                TMP_DIR / "gflow_images" / image_id / f"upscale-{index + 1}-{scale}",
                ignore_errors=True,
            )

    async def _run_upscale(self, image_id: str, index: int, scale: str) -> None:
        task = await db.get_image_task(image_id)
        if not task or task.get("status") != "completed":
            raise GFlowProviderError("INVALID_ARGUMENT", "Image is not ready for upscale")
        metadata = self._metadata(task)
        if index < 0 or index >= len(metadata):
            raise GFlowProviderError("INVALID_ARGUMENT", "Image result index is invalid")
        media_name = str(metadata[index].get("media_name") or "")
        if not media_name:
            raise GFlowProviderError("INVALID_ARGUMENT", "Flow media id is missing")

        await self._set_upscale_state(image_id, index, scale, status="generating")
        prepare_profile(
            Path(app_config.gflow_home) / f"profile_{app_config.gflow_profile}"
        )
        output_dir = TMP_DIR / "gflow_images" / image_id / f"upscale-{index + 1}-{scale}"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        project_id = str(app_config.gflow_project_id or "")
        if not project_id:
            raise GFlowProviderError(
                "INVALID_ARGUMENT", "Flow project id is required for image upscale"
            )
        try:
            result = await asyncio.wait_for(
                download_image_from_editor(
                    profile_dir=(
                        Path(app_config.gflow_home)
                        / f"profile_{app_config.gflow_profile}"
                    ),
                    project_id=project_id,
                    media_id=media_name,
                    output_dir=output_dir,
                    quality=scale.upper(),
                ),
                timeout=app_config.gflow_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise GFlowProviderError(
                "TIMEOUT", f"Image upscale exceeded {app_config.gflow_timeout}s"
            ) from exc
        suffix = ".jpg" if result.suffix.lower() == ".jpeg" else result.suffix.lower()
        storage_key = f"images/{image_id}/{index + 1}-{scale}{suffix}"
        content_type = mimetypes.guess_type(result.name)[0] or "application/octet-stream"
        await storage.upload_file(storage_key, result, content_type=content_type)
        await self._set_upscale_state(
            image_id,
            index,
            scale,
            status="completed",
            storage_key=storage_key,
            file_size=result.stat().st_size,
            content_type=content_type,
        )
        logger.info(
            "[GFlowImageProvider] Upscale completed image=%s index=%s scale=%s bytes=%s",
            image_id,
            index,
            scale,
            result.stat().st_size,
        )

    @staticmethod
    def _metadata(task: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            value = json.loads(task.get("result_metadata") or "[]")
        except (json.JSONDecodeError, TypeError):
            value = []
        return value if isinstance(value, list) else []

    async def _set_upscale_state(
        self, image_id: str, index: int, scale: str, **state: Any
    ) -> None:
        task = await db.get_image_task(image_id)
        if not task:
            return
        metadata = self._metadata(task)
        if index < 0 or index >= len(metadata) or not isinstance(metadata[index], dict):
            return
        upscales = metadata[index].setdefault("upscales", {})
        upscales[scale] = state
        await db.update_image_task(image_id, result_metadata=json.dumps(metadata))


_provider: Optional[GFlowImageProvider] = None


def get_gflow_image_provider() -> GFlowImageProvider:
    global _provider
    if _provider is None:
        _provider = GFlowImageProvider()
    return _provider
