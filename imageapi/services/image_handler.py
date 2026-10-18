"""Image generation orchestration — task queue + worker pattern."""

import asyncio
import base64
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..core.config import DATA_DIR
from ..core.database import db
from ..core.logger import logger
from ..core.models import TaskStatus
from .image_client import GeneratedImage, ImageClient, ImageClientError, ImageGenerationResult
from .load_balancer import LoadBalancer
from .storage import storage as get_storage
from .token_manager import TokenManager

RESULTS_DIR = DATA_DIR / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


class ImageHandler:
    """Orchestrates image generation with task queue and worker pool."""

    def __init__(
        self,
        token_manager: TokenManager,
        load_balancer: LoadBalancer,
        max_concurrent: int = 3,
        max_queue_size: int = 100,
    ):
        self.token_manager = token_manager
        self.load_balancer = load_balancer
        self.max_concurrent = max_concurrent
        self.max_queue_size = max_queue_size
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue_size)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._workers: list[asyncio.Task] = []

    async def start_workers(self, count: int = 3) -> None:
        """Start worker tasks."""
        for i in range(count):
            task = asyncio.create_task(self._worker(f"worker-{i}"))
            self._workers.append(task)
        logger.info(f"Started {count} image generation workers")

    async def stop_workers(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def submit_task(
        self,
        prompt: str,
        model: str = "fast",
        aspect_ratio: Optional[str] = None,
        seed: Optional[int] = None,
        references: Optional[list[str]] = None,
        resolution: Optional[str] = None,
    ) -> str:
        """Submit a new image generation task. Returns task_id."""
        task_id = uuid.uuid4().hex

        # Save references to disk if provided
        if references:
            refs_dir = RESULTS_DIR / f"{task_id[:12]}_refs"
            refs_dir.mkdir(parents=True, exist_ok=True)
            for i, b64_data in enumerate(references):
                try:
                    raw = base64.b64decode(b64_data)
                    filepath = refs_dir / f"ref_{i}.bin"
                    filepath.write_bytes(raw)
                except Exception as e:
                    logger.warning(f"Failed to save reference {i}: {e}")

        # Save to DB
        await db.create_task(
            task_id=task_id,
            prompt=prompt,
            model=model,
            aspect_ratio=aspect_ratio,
            seed=seed,
            resolution=resolution,
        )

        # Enqueue
        try:
            self._queue.put_nowait(task_id)
        except asyncio.QueueFull:
            await db.update_task(task_id, status="failed", error_message="Queue full")
            raise

        logger.info(f"Task queued: {task_id} (model={model}, refs={len(references or [])}, queue_size={self._queue.qsize()})")
        return task_id

    async def _worker(self, name: str) -> None:
        """Worker loop: pull tasks from queue and process."""
        logger.info(f"Worker {name} started")
        while True:
            try:
                task_id = await self._queue.get()
                async with self._semaphore:
                    await self._process_task(task_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {name} error: {e}")

    async def _process_task(self, task_id: str) -> None:
        """Process a single image generation task with veoapi2-style error handling."""
        task = await db.get_task(task_id)
        if not task:
            logger.warning(f"Task {task_id} not found in DB")
            return

        start_time = time.time()
        await db.update_task(task_id, status="processing", started_at=datetime.now())

        MAX_GLOBAL_RETRIES = 5   # Total attempts across all accounts
        MAX_ACCOUNT_RETRIES = 3  # Per-account attempts
        last_error = None
        global_attempt = 0

        while global_attempt < MAX_GLOBAL_RETRIES:
            global_attempt += 1

            # Get an account from the load balancer
            account = await self.load_balancer.get_account()
            if not account:
                logger.warning(f"No accounts available for task {task_id}, attempt {global_attempt}")
                if global_attempt < MAX_GLOBAL_RETRIES:
                    await asyncio.sleep(5)
                    continue
                await db.update_task(
                    task_id, status="failed",
                    error_message="No accounts available",
                    completed_at=datetime.now(),
                    processing_time=time.time() - start_time,
                )
                return

            # Token refresh
            if not await self.token_manager.refresh_account_token(account.id):
                logger.warning(f"Token refresh failed for {account.email}, switching account")
                continue  # Try another account

            account = await db.get_account(account.id)
            if not account or not account.at:
                continue

            # Per-account retry loop
            account_success = False
            for account_attempt in range(MAX_ACCOUNT_RETRIES):
                try:
                    result = await self._generate_with_account(task, account)

                    if result.filtered:
                        await db.update_task(
                            task_id, status="filtered",
                            error_message=result.error_message or "Content filtered",
                            completed_at=datetime.now(),
                            processing_time=time.time() - start_time,
                            account_id=account.id,
                        )
                        # Filtered = not account's fault
                        await self.token_manager.record_success(account.id)
                        return

                    if result.images:
                        result_dir, image_urls = await self._save_images(task_id, result)
                        update_kwargs: dict = dict(
                            status="success",
                            images_count=len(result.images),
                            result_dir=result_dir,
                            completed_at=datetime.now(),
                            processing_time=time.time() - start_time,
                            account_id=account.id,
                        )
                        if image_urls:
                            update_kwargs["image_urls"] = json.dumps(image_urls)
                        await db.update_task(task_id, **update_kwargs)
                        await self.token_manager.record_success(account.id)
                        logger.info(
                            f"Task {task_id}: {len(result.images)} images generated "
                            f"in {time.time() - start_time:.1f}s (account={account.email})"
                        )
                        return

                    # No images and not filtered
                    last_error = result.error_message or "No images in response"
                    await self.token_manager.record_error(account.id)
                    logger.warning(f"Task {task_id}: no images from {account.email}, trying next account")
                    break  # Switch account

                except ImageClientError as e:
                    last_error = str(e)
                    logger.error(f"Task {task_id}: error with {account.email}: {e} (code={e.error_code})")

                    # --- Error-specific handling (matching veoapi2) ---

                    # INVALID_ARGUMENT / INVALID_PROMPT — bad user request, fail immediately
                    if e.error_code in ("INVALID_ARGUMENT", "INVALID_PROMPT"):
                        await db.update_task(
                            task_id, status="failed",
                            error_message=str(e),
                            completed_at=datetime.now(),
                            processing_time=time.time() - start_time,
                            account_id=account.id,
                        )
                        return

                    # PUBLIC_ERROR_USER_REQUESTS_THROTTLED — account overloaded
                    if e.error_code == "PUBLIC_ERROR_USER_REQUESTS_THROTTLED":
                        await self.token_manager.record_throttle(account.id)
                        break  # Switch account

                    # Other PUBLIC_ERROR_* — content/policy rejection, not account issue
                    if e.error_code and e.error_code.startswith("PUBLIC_ERROR"):
                        await db.update_task(
                            task_id, status="failed",
                            error_message=str(e),
                            completed_at=datetime.now(),
                            processing_time=time.time() - start_time,
                            account_id=account.id,
                        )
                        return  # Don't penalize account

                    # UNAUTHORIZED / RATE_LIMITED / TOKEN errors — switch account
                    if e.error_code in ("UNAUTHORIZED", "RATE_LIMITED", "TOKEN_EXCHANGE_ERROR", "TOKEN_EXPIRED"):
                        await self.token_manager.record_error(account.id, e.error_code)
                        break  # Switch account

                    # FORBIDDEN / CAPTCHA_FAILED / GENERATION_ERROR / IMAGE_UPLOAD_ERROR — retry same account
                    if e.error_code in ("FORBIDDEN", "CAPTCHA_FAILED", "GENERATION_ERROR", "IMAGE_UPLOAD_ERROR"):
                        await asyncio.sleep(2 * (account_attempt + 1))
                        continue  # Retry same account

                    # Unknown error — record and switch
                    await self.token_manager.record_error(account.id, e.error_code)
                    if not e.retry:
                        break
                    await asyncio.sleep(2)
                    continue

                except Exception as e:
                    last_error = str(e)
                    logger.error(f"Task {task_id}: unexpected error: {e}")
                    await self.token_manager.record_error(account.id)
                    break  # Switch account

        # All attempts exhausted
        await db.update_task(
            task_id, status="failed",
            error_message=last_error or "Generation failed after all attempts",
            completed_at=datetime.now(),
            processing_time=time.time() - start_time,
        )
        logger.error(f"Task {task_id} failed after {global_attempt} global attempts: {last_error}")

    async def _generate_with_account(self, task, account) -> ImageGenerationResult:
        """Generate images using a specific account. Token must already be refreshed."""
        client = self.token_manager.get_client_for_account(account)

        # Ensure project ID exists (needed for both upload and generation)
        project_id = account.project_id
        if not project_id:
            project_id = await client.create_project(account.st)
            if not project_id:
                raise ImageClientError(
                    "Failed to create project",
                    error_code="PROJECT_CREATION_FAILED",
                )
            await db.update_account(account.id, project_id=project_id)

        # Upload reference images if present
        image_inputs = []
        workflow_ids_to_delete = []
        refs_dir = RESULTS_DIR / f"{task.task_id[:12]}_refs"
        if refs_dir.exists():
            for filepath in sorted(refs_dir.iterdir()):
                if filepath.suffix == ".bin":
                    try:
                        image_data = filepath.read_bytes()
                        media_name, workflow_id = await client.upload_image(
                            at=account.at,
                            image_data=image_data,
                            project_id=project_id,
                        )
                        image_inputs.append({"imageInputType": "IMAGE_INPUT_TYPE_REFERENCE", "name": media_name})
                        if workflow_id:
                            workflow_ids_to_delete.append(workflow_id)
                        logger.debug(f"Uploaded reference: {filepath.name} -> {media_name}")
                    except Exception as e:
                        logger.warning(f"Failed to upload reference {filepath.name}: {e}")

        try:
            result = await client.generate_images(
                at=account.at,
                st=account.st,
                prompt=task.prompt,
                model=task.model,
                aspect_ratio=task.aspect_ratio or "1:1",
                project_id=project_id,
                seed=task.seed,
                image_inputs=image_inputs if image_inputs else None,
                user_paygate_tier=account.user_paygate_tier,
            )
        finally:
            # Always clean up uploaded reference images
            if workflow_ids_to_delete:
                await client.delete_assets(
                    at=account.at,
                    project_id=project_id,
                    workflow_ids=workflow_ids_to_delete,
                )

        # Upsample generated images (default 2K; 4K if resolution="4k")
        if result.images:
            resolution = getattr(task, "resolution", None)
            target = (
                "UPSAMPLE_IMAGE_RESOLUTION_4K"
                if resolution and resolution.lower() == "4k"
                else "UPSAMPLE_IMAGE_RESOLUTION_2K"
            )
            upsampled: list[GeneratedImage] = []
            for img in result.images:
                if img.media_id:
                    try:
                        up_bytes = await client.upsample_image(
                            at=account.at,
                            project_id=project_id,
                            media_id=img.media_id,
                            target_resolution=target,
                            user_paygate_tier=account.user_paygate_tier,
                        )
                        upsampled.append(GeneratedImage(data=up_bytes, seed=img.seed, mime_type="image/png"))
                    except ImageClientError as e:
                        logger.warning(f"Upsample failed for {img.media_id}: {e}, using original")
                        upsampled.append(img)
                else:
                    upsampled.append(img)
            result.images = upsampled

        return result

    async def _save_images(self, task_id: str, result: ImageGenerationResult) -> tuple[str, list[str]]:
        """Save generated images to disk and upload to S3 if configured.

        Returns (result_dir_path, image_urls). image_urls is empty if S3 not enabled.
        """
        result_dir = RESULTS_DIR / task_id[:12]
        result_dir.mkdir(parents=True, exist_ok=True)

        store = get_storage()
        image_urls: list[str] = []

        for i, img in enumerate(result.images):
            ext = "png" if "png" in img.mime_type else "jpg"
            filename = f"{i}.{ext}"
            filepath = result_dir / filename
            filepath.write_bytes(img.data)
            logger.debug(f"Saved image: {filepath} ({len(img.data)} bytes)")

            # Upload to S3, delete local file on success
            key = f"images/{task_id[:12]}/{filename}"
            try:
                await store.upload(key, img.data, content_type=img.mime_type)
                url = store.public_url(key)
                if url:
                    image_urls.append(url)
                    filepath.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"S3 upload failed for {key}: {e}")

        # Remove result_dir if all images were uploaded (dir is now empty)
        try:
            if image_urls and result_dir.exists() and not any(result_dir.iterdir()):
                result_dir.rmdir()
        except Exception:
            pass

        return str(result_dir), image_urls

    async def get_task_images_base64(self, task_id: str) -> list[dict]:
        """Load task images from disk as base64."""
        task = await db.get_task(task_id)
        if not task or not task.result_dir:
            return []

        result_dir = Path(task.result_dir)
        if not result_dir.exists():
            return []

        images = []
        for filepath in sorted(result_dir.iterdir()):
            if filepath.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                data = filepath.read_bytes()
                images.append({
                    "filename": filepath.name,
                    "base64": base64.b64encode(data).decode("ascii"),
                })

        return images

    async def cleanup_old_results(self, ttl_minutes: int = 30) -> int:
        """Delete old result directories."""
        import shutil

        count = 0
        now = time.time()
        cutoff = now - (ttl_minutes * 60)

        if not RESULTS_DIR.exists():
            return 0

        for entry in RESULTS_DIR.iterdir():
            if entry.is_dir():
                try:
                    mtime = entry.stat().st_mtime
                    if mtime < cutoff:
                        shutil.rmtree(entry)
                        count += 1
                except Exception as e:
                    logger.warning(f"Cleanup error for {entry}: {e}")

        # Also clean up orphaned _refs dirs older than cutoff
        for entry in RESULTS_DIR.iterdir():
            if entry.is_dir() and entry.name.endswith("_refs"):
                try:
                    mtime = entry.stat().st_mtime
                    if mtime < cutoff:
                        shutil.rmtree(entry)
                        count += 1
                except Exception:
                    pass

        if count:
            logger.info(f"Cleaned up {count} old result directories")
        return count

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()
