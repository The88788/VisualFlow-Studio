"""Download an existing Flow video through the migrated UI.

The migrated editor exposes downloads as ``blob:`` URLs, so an HTTP client
cannot recover the file from Chrome history or the poster URL.  Playwright
must click Flow's own download control and capture the browser download.
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import async_playwright

from ...core.logger import logger


async def download_video_from_editor(
    *, profile_dir: Path, project_id: str, media_id: str, output_path: Path
) -> None:
    """Download the original 720p asset from a migrated Flow editor page."""
    url = f"https://flow.google.com/project/{project_id}/edit/{media_id}"
    output_path.parent.mkdir(parents=True, exist_ok=True)

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
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)

            download_button = page.locator("button").filter(
                has=page.locator("mat-icon", has_text="download")
            ).first
            await download_button.wait_for(state="visible", timeout=90_000)
            await download_button.click()

            # The account may render unavailable 1080p/4K upsells alongside
            # the original.  Selecting 720p is credit-free and matches the
            # currently available subscription tier.
            quality = page.get_by_role("menuitem").filter(
                has_text=re.compile(r"\b720p\b", re.IGNORECASE)
            )
            await quality.wait_for(state="visible", timeout=15_000)
            async with page.expect_download(timeout=120_000) as download_info:
                await quality.click()
            download = await download_info.value
            await download.save_as(str(output_path))
        finally:
            await context.close()

    if not output_path.is_file() or output_path.stat().st_size <= 8:
        raise RuntimeError("Flow browser download did not create a video file")
    with output_path.open("rb") as stream:
        header = stream.read(8)
    if header[4:8] != b"ftyp":
        output_path.unlink(missing_ok=True)
        raise RuntimeError("Flow browser download did not return an MP4 file")
    logger.info(
        "[GFlowBrowserDownload] Downloaded media=%s bytes=%d quality=720p",
        media_id,
        output_path.stat().st_size,
    )
