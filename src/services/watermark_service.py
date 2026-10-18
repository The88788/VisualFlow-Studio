"""Watermark removal service for Veo videos.

Detects and removes the semi-transparent watermark in the bottom-right corner.
Strategy:
  1. Fast detection: brightness check on a few frames
  2. If detected: temporal alpha-map (percentile 80, 30 sample frames) → only ROI stored in RAM
  3. Reverse alpha blending + inpainting per frame
  4. Output piped directly to FFmpeg (libx264 ultrafast crf18) to avoid double-encoding
     Audio is copied as-is from the original.

Tuned parameters (from test_alpha.py best-result run):
  --feather 20 --no-color-correct --region-blend 10 --alpha-threshold 0.05 --inpaint-radius 3
"""

import subprocess
from pathlib import Path
from typing import Optional

import asyncio

from ..core.logger import logger

# ── Watermark removal parameters ─────────────────────────────────────────────
_ALPHA_THRESHOLD = 0.05   # pixels below this alpha are treated as no-WM
_ALPHA_BOOST     = 1.8    # multiply detected alpha before removal
_FEATHER_RADIUS  = 20     # gaussian blur on alpha mask (px)
_DILATE_RADIUS   = 3      # dilate WM mask before removal (px)
_REMOVAL_PASSES  = 2      # reverse alpha blend passes
_INPAINT_RADIUS  = 3      # cv2.inpaint radius (0 = disabled)
_COLOR_CORRECT   = False  # brightness equalisation after removal
_REGION_BLEND    = 10     # smoothstep gradient at region border (px)
_TEMPORAL_PCTILE = 80     # percentile for temporal alpha extraction
_TEMPORAL_SAMPLES = 30    # frames to sample for alpha map (30 is enough for ~8s Veo video)

# Veo watermark location — bottom-right corner (% of frame dimensions)
_WM_XPCT = 0.88
_WM_YPCT = 0.90
_WM_WPCT = 0.12
_WM_HPCT = 0.10

# Detection: how much brighter the WM region must be vs its local background
# (relative excess in grayscale, 0-255 scale)
_EXCESS_THRESHOLD = 15   # ROI pixels must exceed background by this much
_EXCESS_PX_MIN    = 30   # at least this many pixels must exceed the threshold

# Minimum WM pixels in alpha map to proceed with removal
_MIN_WM_PIXELS = 10

# FFmpeg encoding settings — ultrafast preset to minimise CPU load
_FFMPEG_PRESET = "ultrafast"
_FFMPEG_CRF    = "18"      # visually lossless; original Veo video is already lossy anyway


# ── Detection ─────────────────────────────────────────────────────────────────

def _detect_watermark_sync(video_path: Path) -> bool:
    """Quick watermark check using relative excess over local background.

    The Veo watermark is semi-transparent — its pixels are NOT purely white,
    but they ARE consistently brighter than the surrounding background.
    We measure how many ROI pixels exceed (local_bg_mean + _EXCESS_THRESHOLD)
    and require at least 2 out of 5 frames to confirm.
    """
    try:
        import cv2
        import numpy as np

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return False

        total  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        if total < 1 or width < 100 or height < 100:
            cap.release()
            return False

        x = int(width  * _WM_XPCT)
        y = int(height * _WM_YPCT)
        w = min(int(width  * _WM_WPCT), width  - x)
        h = min(int(height * _WM_HPCT), height - y)
        margin = 10

        n_check    = min(5, total)
        step       = max(1, total // n_check)
        detections = 0

        for idx in range(n_check):
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx * step)
            ret, frame = cap.read()
            if not ret:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            roi  = gray[y:y+h, x:x+w].astype(np.float32)

            # Local background: thin border around the region
            border = []
            if x >= margin:           border.append(gray[y:y+h,    x-margin:x      ].ravel())
            if y >= margin:           border.append(gray[y-margin:y, x:x+w          ].ravel())
            if y+h+margin <= height:  border.append(gray[y+h:y+h+margin, x:x+w     ].ravel())
            if x+w+margin <= width:   border.append(gray[y:y+h,    x+w:x+w+margin   ].ravel())

            if not border:
                continue

            bg_mean = float(np.median(np.concatenate(border)))
            excess_px = int(np.sum(roi > bg_mean + _EXCESS_THRESHOLD))

            if excess_px >= _EXCESS_PX_MIN:
                detections += 1

        cap.release()
        return detections >= 2

    except Exception as exc:
        logger.warning(f"[watermark] Detection error: {exc}")
        return False


# ── Alpha-map extraction ──────────────────────────────────────────────────────

def _extract_alpha_map(
    video_path: Path,
    region: tuple[int, int, int, int],
) -> Optional[tuple]:
    """Build a temporal alpha-map for the watermark region.

    Only the ROI pixels are kept in memory (not full frames) — low RAM footprint.
    Returns (alpha_map, median_bg) or None on error.
    """
    try:
        import cv2
        import numpy as np

        x, y, w, h = region

        cap    = cv2.VideoCapture(str(video_path))
        total  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fh     = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fw     = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        margin = 10

        step      = max(1, total // _TEMPORAL_SAMPLES)
        roi_stack = []
        bg_stack  = []

        for i in range(0, total, step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ret, frame = cap.read()
            if not ret:
                continue

            roi_stack.append(frame[y:y+h, x:x+w].astype(np.float32))

            # Background: median of a thin border around the WM region
            samples = []
            if x >= margin:
                samples.append(frame[y:y+h,       x-margin:x      ].reshape(-1, 3))
            if y >= margin:
                samples.append(frame[y-margin:y,  x:x+w           ].reshape(-1, 3))
            if y + h + margin <= fh:
                samples.append(frame[y+h:y+h+margin, x:x+w        ].reshape(-1, 3))
            if x + w + margin <= fw:
                samples.append(frame[y:y+h,       x+w:x+w+margin  ].reshape(-1, 3))

            bg = (
                np.median(np.concatenate(samples, axis=0).astype(np.float32), axis=0)
                if samples else np.zeros(3, dtype=np.float32)
            )
            bg_stack.append(bg)

        cap.release()

        if not roi_stack:
            return None

        roi_arr = np.stack(roi_stack)   # (N, h, w, 3)
        bg_arr  = np.stack(bg_stack)    # (N, 3)

        excess       = np.clip(roi_arr - bg_arr[:, np.newaxis, np.newaxis, :], 0, None)
        pctile_exc   = np.percentile(excess, _TEMPORAL_PCTILE, axis=0)
        median_bg    = np.median(bg_arr, axis=0)

        denom        = np.clip(255.0 - median_bg, 1.0, None)
        alpha_map    = np.clip(np.max(pctile_exc / denom, axis=2), 0.0, 1.0).astype(np.float32)

        return alpha_map, median_bg

    except Exception as exc:
        logger.warning(f"[watermark] Alpha map error: {exc}")
        return None


def _postprocess_alpha(alpha_map):
    """Threshold → dilate → boost → feather."""
    import cv2
    import numpy as np

    amap = alpha_map.copy()
    amap[amap < _ALPHA_THRESHOLD] = 0.0

    if _DILATE_RADIUS > 0:
        binary = (amap > 0).astype(np.uint8)
        k      = _DILATE_RADIUS * 2 + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        dilated = cv2.dilate(binary, kernel)
        new_px  = (dilated > 0) & (amap == 0)
        if np.any(new_px):
            blur_k       = _DILATE_RADIUS * 4 + 1
            fill         = cv2.GaussianBlur(amap, (blur_k, blur_k), 0)
            amap[new_px] = fill[new_px]

    amap = np.clip(amap * _ALPHA_BOOST, 0.0, 1.0)

    if _FEATHER_RADIUS > 0:
        k    = _FEATHER_RADIUS * 2 + 1
        amap = cv2.GaussianBlur(amap, (k, k), 0)

    return np.clip(amap, 0.0, 1.0)


# ── Per-frame removal ─────────────────────────────────────────────────────────

def _remove_frame(
    frame,
    region: tuple[int, int, int, int],
    alpha_map,
):
    """Apply reverse alpha blending + inpainting + edge blending to one frame."""
    import cv2
    import numpy as np

    MAX_ALPHA = 0.98
    x, y, w, h = region
    fh, fw     = frame.shape[:2]
    result     = frame.copy()

    # Reverse alpha blend (up to _REMOVAL_PASSES passes)
    for p in range(_REMOVAL_PASSES):
        result_f = result.astype(np.float32)
        roi      = result_f[y:y+h, x:x+w]

        a        = alpha_map if p == 0 else alpha_map * 0.35
        alpha    = np.clip(a, 0.0, MAX_ALPHA)[:, :, np.newaxis]

        restored = np.clip((roi - alpha * 255.0) / (1.0 - alpha), 0, 255)
        blend    = alpha_map[:, :, np.newaxis].clip(0, 1)
        result_f[y:y+h, x:x+w] = blend * restored + (1.0 - blend) * roi
        result   = result_f.astype(np.uint8)

    # Inpainting residuals
    if _INPAINT_RADIUS > 0:
        inpaint_mask               = (alpha_map > _ALPHA_THRESHOLD).astype(np.uint8) * 255
        full_mask                  = np.zeros((fh, fw), dtype=np.uint8)
        full_mask[y:y+h, x:x+w]   = inpaint_mask
        result                     = cv2.inpaint(result, full_mask, _INPAINT_RADIUS, cv2.INPAINT_NS)

    # Smooth border between processed and original region
    if _REGION_BLEND > 0:
        m             = _REGION_BLEND
        processed_roi = result[y:y+h, x:x+w].astype(np.float32)
        original_roi  = frame[y:y+h,  x:x+w].astype(np.float32)
        mask          = np.ones((h, w), dtype=np.float32)

        for i in range(min(m, h // 2)):
            t = (i + 1) / (m + 1)
            t = t * t * (3 - 2 * t)                    # smoothstep
            mask[i,      :]  = np.minimum(mask[i,      :],  t)
            mask[h-1-i,  :]  = np.minimum(mask[h-1-i,  :],  t)
        for i in range(min(m, w // 2)):
            t = (i + 1) / (m + 1)
            t = t * t * (3 - 2 * t)
            mask[:,   i    ]  = np.minimum(mask[:,   i    ], t)
            mask[:, w-1-i  ]  = np.minimum(mask[:, w-1-i  ], t)

        mask3                = mask[:, :, np.newaxis]
        blended              = mask3 * processed_roi + (1.0 - mask3) * original_roi
        result[y:y+h, x:x+w] = np.clip(blended, 0, 255).astype(np.uint8)

    return result


# ── Full removal pipeline ─────────────────────────────────────────────────────

def _remove_watermark_sync(input_path: Path, output_path: Path) -> bool:
    """Detect watermark and remove it.  Returns True if removed, False if no WM found.

    Pipeline:
      detect (5 frames) → build alpha map (30 frames, ROI only) →
      per-frame removal (numpy/cv2) → pipe raw BGR24 to FFmpeg →
      libx264 ultrafast crf18 + copy audio from original
    """
    import cv2
    import numpy as np

    # ── 1. Fast detection ──────────────────────────────────────────────────────
    if not _detect_watermark_sync(input_path):
        return False

    # ── 2. Video properties ────────────────────────────────────────────────────
    cap    = cv2.VideoCapture(str(input_path))
    total  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps    = cap.get(cv2.CAP_PROP_FPS) or 24.0
    width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    if total < 1:
        return False

    x = int(width  * _WM_XPCT)
    y = int(height * _WM_YPCT)
    w = min(int(width  * _WM_WPCT), width  - x)
    h = min(int(height * _WM_HPCT), height - y)
    region = (x, y, w, h)

    # ── 3. Build alpha map ─────────────────────────────────────────────────────
    result = _extract_alpha_map(input_path, region)
    if result is None:
        return False

    alpha_map, _ = result
    alpha_map    = _postprocess_alpha(alpha_map)

    if int(np.sum(alpha_map > _ALPHA_THRESHOLD)) < _MIN_WM_PIXELS:
        logger.debug("[watermark] Alpha map too sparse — skipping removal")
        return False

    logger.info(
        f"[watermark] Removing from {input_path.name} "
        f"(region {x},{y} {w}x{h}, {int(np.sum(alpha_map > _ALPHA_THRESHOLD))} WM px)"
    )

    # ── 4. FFmpeg process (reads raw BGR24 from stdin, copies audio from original) ─
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        # Video input: raw frames from stdin
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{width}x{height}",
        "-pix_fmt", "bgr24",
        "-r", str(fps),
        "-i", "pipe:0",
        # Audio input: original file
        "-i", str(input_path),
        # Map processed video + original audio
        "-map", "0:v:0",
        "-map", "1:a?",
        # Encode video with libx264 — ultrafast for weak CPU, crf18 for near-lossless quality
        "-c:v", "libx264",
        "-preset", _FFMPEG_PRESET,
        "-crf", _FFMPEG_CRF,
        # Copy audio without re-encoding
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]

    try:
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,  # unbuffered — avoids "flush of closed file" on broken pipe
        )
    except FileNotFoundError:
        logger.error("[watermark] FFmpeg not found — cannot remove watermark")
        return False

    # ── 5. Process frames and pipe to FFmpeg ──────────────────────────────────
    cap            = cv2.VideoCapture(str(input_path))
    frames_written = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            processed = _remove_frame(frame, region, alpha_map)
            try:
                proc.stdin.write(processed.tobytes())
            except (BrokenPipeError, OSError, ValueError):
                # FFmpeg died mid-stream — stop feeding frames
                break
            frames_written += 1
    except Exception as exc:
        logger.error(f"[watermark] Frame processing error: {exc}")
    finally:
        cap.release()
        try:
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
        except Exception:
            pass
        proc.stdin = None  # prevent communicate() from re-flushing closed stdin

    try:
        _, stderr = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        logger.error("[watermark] FFmpeg timed out during watermark encoding")
        output_path.unlink(missing_ok=True)
        return False

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        logger.error(f"[watermark] FFmpeg failed (rc={proc.returncode}):\n{err[-600:]}")
        output_path.unlink(missing_ok=True)
        return False

    if not output_path.exists() or output_path.stat().st_size < 1024:
        output_path.unlink(missing_ok=True)
        return False

    logger.info(
        f"[watermark] Done: {frames_written} frames → {output_path.name} "
        f"({output_path.stat().st_size // 1024} KB)"
    )
    return True


# ── Async wrapper ─────────────────────────────────────────────────────────────

class WatermarkService:
    """Async wrapper around the synchronous watermark removal pipeline.

    All heavy work (OpenCV + FFmpeg) runs in a thread pool via asyncio.to_thread,
    so the event loop stays responsive.
    """

    async def process_if_needed(self, input_path: Path, output_path: Path) -> bool:
        """Check for watermark and remove it if found.

        Returns True if watermark was detected and successfully removed.
        The caller is responsible for swapping input_path → output_path.
        """
        try:
            return await asyncio.to_thread(_remove_watermark_sync, input_path, output_path)
        except Exception as exc:
            logger.error(f"[watermark] process_if_needed error: {exc}")
            return False


watermark_service = WatermarkService()
