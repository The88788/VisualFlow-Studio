"""Download native image variants through the migrated Flow web interface."""

from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import async_playwright

from ...core.logger import logger


async def download_image_from_editor(
    *,
    profile_dir: Path,
    project_id: str,
    media_id: str,
    output_dir: Path,
    quality: str = "2K",
) -> Path:
    """Select an image's native Flow download quality and capture the file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    gallery_url = f"https://flow.google.com/project/{project_id}"
    output_path: Path | None = None

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            accept_downloads=True,
            viewport={"width": 1920, "height": 1080},
            args=[
                "--no-sandbox",
                "--password-store=basic",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
            ignore_default_args=["--enable-automation"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(gallery_url, wait_until="domcontentloaded", timeout=90_000)

            # Image edit routes are safest to enter through the SPA gallery. A cold
            # direct /edit/<id> navigation can render Flow's generic error page.
            tile = page.locator(f'a[href*="/edit/{media_id}"]').first
            try:
                await tile.wait_for(state="visible", timeout=30_000)
                await tile.click(timeout=15_000)
            except Exception:
                # Recent migrated cohorts also support the direct route. Keep it as
                # a bounded fallback for virtualized galleries that hide the tile.
                await page.goto(
                    f"{gallery_url}/edit/{media_id}",
                    wait_until="domcontentloaded",
                    timeout=90_000,
                )

            download_button = page.locator("button").filter(
                has=page.locator("mat-icon", has_text="download")
            ).first
            await download_button.wait_for(state="visible", timeout=90_000)
            await download_button.click(timeout=15_000)

            quality_item = page.get_by_role("menuitem").filter(
                has_text=re.compile(rf"\b{re.escape(quality)}\b", re.IGNORECASE)
            )
            await quality_item.wait_for(state="visible", timeout=20_000)
            if await quality_item.count() != 1:
                offered = await page.get_by_role("menuitem").all_text_contents()
                raise RuntimeError(
                    f"Flow download quality {quality} matched "
                    f"{await quality_item.count()} items; offered: {offered}"
                )
            async with page.expect_download(timeout=300_000) as download_info:
                await quality_item.click(timeout=15_000)
            download = await download_info.value
            suffix = Path(download.suggested_filename).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                suffix = ".png"
            output_path = output_dir / f"upscaled{suffix}"
            await download.save_as(str(output_path))
        finally:
            await context.close()

    if output_path is None or not output_path.is_file() or output_path.stat().st_size <= 12:
        raise RuntimeError("Flow browser download did not create an upscaled image")
    header = output_path.read_bytes()[:12]
    is_image = (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or (header[:4] == b"RIFF" and header[8:12] == b"WEBP")
    )
    if not is_image:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("Flow browser download did not return an image file")
    logger.info(
        "[GFlowImageDownload] Downloaded media=%s bytes=%d quality=%s",
        media_id,
        output_path.stat().st_size,
        quality,
    )
    return output_path
