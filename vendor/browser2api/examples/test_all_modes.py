"""Test all generation modes added to FlowVideoClient.

Usage:
    python examples/test_all_modes.py --img1 ./a.jpg --img2 ./b.jpg

    # Run specific modes only:
    python examples/test_all_modes.py --img1 ./a.jpg --modes t2v i2v
"""

import argparse
import asyncio
import logging
from pathlib import Path

from browser2api import BrowserManager, Platform
from browser2api.platforms.flow import FlowCount, FlowOrientation, FlowVideoModel
from browser2api.platforms.flow.client import FlowVideoClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

OUTPUT = Path(__file__).resolve().parent.parent / "output" / "test_all"
PROMPT = "A cat walking through a garden, soft sunlight"
EXTEND_PROMPT = "The cat sits down and looks at the camera"


async def run(img1: Path | None, img2: Path | None, modes: list[str], project_url: str | None = None):
    bm = BrowserManager()
    results = {}

    try:
        context, page = await bm.launch_for_login(Platform.FLOW)
        client = FlowVideoClient(
            page, context,
            output_dir=OUTPUT,
            model=FlowVideoModel.VEO_3_1_FAST,
            orientation=FlowOrientation.LANDSCAPE,
            count=FlowCount.X1,
        )

        # ── T2V ──────────────────────────────────────────────────────────────
        if "t2v" in modes:
            log.info("=== T2V ===")
            r = await client.generate_video(PROMPT)
            results["t2v"] = r.status.value
            log.info("T2V → %s | %s", r.status.value, r.video.local_path if r.video else r.error)

        # ── I2V ──────────────────────────────────────────────────────────────
        if "i2v" in modes:
            if not img1:
                log.warning("I2V skipped — provide --img1")
            else:
                log.info("=== I2V ===")
                r = await client.generate_video(PROMPT, start_image=img1)
                results["i2v"] = r.status.value
                log.info("I2V → %s | %s", r.status.value, r.video.local_path if r.video else r.error)

        # ── I2V_DUAL ─────────────────────────────────────────────────────────
        if "i2v_dual" in modes:
            if not img1 or not img2:
                log.warning("I2V_DUAL skipped — provide --img1 and --img2")
            else:
                log.info("=== I2V_DUAL ===")
                r = await client.generate_video(PROMPT, start_image=img1, end_image=img2)
                results["i2v_dual"] = r.status.value
                log.info("I2V_DUAL → %s | %s", r.status.value, r.video.local_path if r.video else r.error)

        # ── R2V ──────────────────────────────────────────────────────────────
        if "r2v" in modes:
            imgs = [p for p in [img1, img2] if p]
            if not imgs:
                log.warning("R2V skipped — provide at least --img1")
            else:
                log.info("=== R2V (%d reference images) ===", len(imgs))
                r = await client.generate_video(PROMPT, reference_images=imgs)
                results["r2v"] = r.status.value
                log.info("R2V → %s | %s", r.status.value, r.video.local_path if r.video else r.error)

        # ── EXTEND ───────────────────────────────────────────────────────────
        if "extend" in modes:
            log.info("=== EXTEND (generates T2V first, then extends) ===")
            base = await client.generate_video(PROMPT)
            if base.video:
                r = await client.extend_video(
                    EXTEND_PROMPT,
                    video_url=base.video.url,
                )
                results["extend"] = r.status.value
                log.info("EXTEND → %s | %s", r.status.value, r.video.local_path if r.video else r.error)
            else:
                results["extend"] = "skipped (base generation failed)"
                log.warning("EXTEND skipped — base generation failed: %s", base.error)

        # ── UPSCALE ──────────────────────────────────────────────────────────
        if "upscale" in modes:
            if project_url:
                log.info("=== UPSCALE (using existing project: %s) ===", project_url)
                await page.goto(project_url, wait_until="commit", timeout=60000)
                import asyncio as _asyncio; await _asyncio.sleep(8)
                path = await client.upscale_video("1080p", output_dir=OUTPUT)
            else:
                log.info("=== UPSCALE (generates T2V first, then upscales to 1080p) ===")
                base = await client.generate_video(PROMPT)
                if base.video:
                    path = await client.upscale_video("1080p", video_url=base.video.url, output_dir=OUTPUT)
                else:
                    results["upscale_1080p"] = "skipped (base generation failed)"
                    log.warning("UPSCALE skipped — base generation failed: %s", base.error)
                    path = None
            if path is not None:
                results["upscale_1080p"] = str(path) if path else "failed"
                log.info("UPSCALE 1080p → %s", path or "failed")

    finally:
        await bm.close()

    print("\n" + "=" * 50)
    print("RESULTS:")
    for mode, status in results.items():
        icon = "✓" if "completed" in str(status) or ("\\" in str(status)) or ("/" in str(status)) else "✗"
        print(f"  {icon} {mode}: {status}")
    print("=" * 50)


if __name__ == "__main__":
    all_modes = ["t2v", "i2v", "i2v_dual", "r2v", "extend", "upscale"]

    parser = argparse.ArgumentParser(description="Test all FlowVideoClient generation modes")
    parser.add_argument("--img1", type=Path, default=None, help="First test image (I2V start / R2V ref)")
    parser.add_argument("--img2", type=Path, default=None, help="Second test image (I2V end / R2V ref)")
    parser.add_argument(
        "--modes", nargs="+", choices=all_modes, default=all_modes,
        help=f"Modes to test (default: all). Choices: {all_modes}",
    )
    parser.add_argument(
        "--project-url", default=None,
        help="Existing project URL to use for upscale/extend instead of generating a new video",
    )
    args = parser.parse_args()

    OUTPUT.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(args.img1, args.img2, args.modes, project_url=args.project_url))
