"""FFmpeg service for video trim and merge operations."""

import asyncio
import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional

import httpx

from ..core.config import TMP_DIR
from ..core.logger import logger

# Timeout for FFmpeg/FFprobe subprocess calls (seconds)
_FFMPEG_TIMEOUT = 300


class FFmpegError(Exception):
    """FFmpeg operation error."""

    pass


class FFmpegService:
    """Service for video manipulation using FFmpeg."""

    def __init__(self, tmp_dir: Path = TMP_DIR):
        self.tmp_dir = tmp_dir
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    async def download_video(self, url: str, output_path: Path) -> None:
        """Download a video from URL."""
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("GET", url, follow_redirects=True) as response:
                response.raise_for_status()
                with open(output_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        f.write(chunk)

        logger.debug(f"Downloaded video to {output_path}")

    async def trim_video(
        self,
        input_url: str,
        start_offset_sec: Optional[float] = None,
        end_offset_sec: Optional[float] = None,
    ) -> Path:
        """
        Trim a video by cutting from start and/or end.

        Args:
            input_url: URL of the video to trim
            start_offset_sec: Seconds to cut from the BEGINNING (optional)
            end_offset_sec: Seconds to cut from the END (optional)

        Returns:
            Path to the trimmed video file
        """
        job_id = str(uuid.uuid4())[:8]
        input_path = self.tmp_dir / f"trim_input_{job_id}.mp4"
        output_path = self.tmp_dir / f"trim_output_{job_id}.mp4"

        try:
            # Download input video
            await self.download_video(input_url, input_path)

            # Get video duration to calculate end position
            duration = await self.get_video_duration(input_path)

            # Calculate actual start and end times
            start_time = round(start_offset_sec or 0, 2)
            end_time = round(duration - (end_offset_sec or 0), 2)

            if start_time >= end_time:
                raise FFmpegError(
                    f"Invalid trim: start ({start_time}s) >= end ({end_time}s)"
                )

            # Build FFmpeg command
            # Note: -ss before -i for fast seeking, -t for duration (not -to for absolute time)
            trim_duration = end_time - start_time
            cmd = [
                "ffmpeg",
                "-y",  # Overwrite output
                "-ss",
                str(start_time),
                "-i",
                str(input_path),
                "-t",
                str(trim_duration),
                "-c",
                "copy",  # Copy without re-encoding
                str(output_path),
            ]

            logger.info(
                f"Trimming video: cutting {start_offset_sec or 0}s from start, "
                f"{end_offset_sec or 0}s from end (duration: {duration}s)"
            )

            # Run FFmpeg (using subprocess.run in thread for Windows compatibility)
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    capture_output=True,
                    timeout=_FFMPEG_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                raise FFmpegError(f"FFmpeg trim timed out after {_FFMPEG_TIMEOUT}s")

            if result.returncode != 0:
                error_msg = result.stderr.decode(errors='replace') if result.stderr else "Unknown error"
                logger.error(f"FFmpeg trim failed: {error_msg}")
                raise FFmpegError(f"Trim failed: {error_msg}")

            if not output_path.exists():
                raise FFmpegError("Output file not created")

            logger.info(f"Video trimmed successfully: {output_path}")
            return output_path

        finally:
            # Cleanup input file
            if input_path.exists():
                os.remove(input_path)

    async def merge_videos(
        self,
        video_urls: list[str],
        end_offset_sec: Optional[float] = None,
        per_video_end_offset_sec: Optional[list[float]] = None,
    ) -> Path:
        """
        Merge multiple videos into one, with optional end trimming.

        Args:
            video_urls: List of video URLs to merge
            end_offset_sec: Global end offset in seconds to trim from each clip
            per_video_end_offset_sec: Per-video end offsets (overrides global)

        Returns:
            Path to the merged video file
        """
        if len(video_urls) < 2:
            raise FFmpegError("At least 2 videos required for merge")

        job_id = str(uuid.uuid4())[:8]
        input_paths: list[Path] = []
        trimmed_paths: list[Path] = []
        concat_file = self.tmp_dir / f"merge_list_{job_id}.txt"
        output_path = self.tmp_dir / f"merge_output_{job_id}.mp4"

        try:
            # Download all input videos
            for i, url in enumerate(video_urls):
                input_path = self.tmp_dir / f"merge_input_{job_id}_{i}.mp4"
                await self.download_video(url, input_path)
                input_paths.append(input_path)

            # Determine offsets for each video
            offsets = []
            for i in range(len(video_urls)):
                if per_video_end_offset_sec and i < len(per_video_end_offset_sec):
                    offset = per_video_end_offset_sec[i]
                elif end_offset_sec is not None:
                    offset = end_offset_sec
                else:
                    offset = 0
                offsets.append(max(0, round(offset, 2)))

            # Trim videos if needed (apply end offsets)
            for i, (input_path, offset) in enumerate(zip(input_paths, offsets)):
                if offset > 0:
                    # Trim this video
                    duration = await self.get_video_duration(input_path)
                    end_time = round(duration - offset, 2)

                    if end_time <= 0:
                        logger.warning(
                            f"Video {i} offset {offset}s >= duration {duration}s, skipping"
                        )
                        # Use empty file or skip - for now just use original
                        trimmed_paths.append(input_path)
                        continue

                    trimmed_path = self.tmp_dir / f"merge_trimmed_{job_id}_{i}.mp4"

                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(input_path),
                        "-ss",
                        "0",
                        "-to",
                        str(end_time),
                        "-c",
                        "copy",
                        str(trimmed_path),
                    ]

                    try:
                        result = await asyncio.to_thread(
                            subprocess.run,
                            cmd,
                            capture_output=True,
                            timeout=_FFMPEG_TIMEOUT,
                        )
                    except subprocess.TimeoutExpired:
                        logger.error(f"FFmpeg trim timed out for video {i}")
                        trimmed_paths.append(input_path)
                        continue

                    if result.returncode == 0 and trimmed_path.exists():
                        trimmed_paths.append(trimmed_path)
                        logger.debug(f"Trimmed video {i}: cut {offset}s from end")
                    else:
                        # Fallback to original
                        trimmed_paths.append(input_path)
                else:
                    trimmed_paths.append(input_path)

            # Create concat file
            with open(concat_file, "w") as f:
                for path in trimmed_paths:
                    escaped_path = str(path).replace("\\", "/")
                    f.write(f"file '{escaped_path}'\n")

            # Build FFmpeg command
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                str(output_path),
            ]

            logger.info(
                f"Merging {len(video_urls)} videos with offsets: {offsets}"
            )

            # Run FFmpeg (using subprocess.run in thread for Windows compatibility)
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    capture_output=True,
                    timeout=_FFMPEG_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                raise FFmpegError(f"FFmpeg merge timed out after {_FFMPEG_TIMEOUT}s")

            if result.returncode != 0:
                error_msg = result.stderr.decode(errors='replace') if result.stderr else "Unknown error"
                logger.error(f"FFmpeg merge failed: {error_msg}")
                raise FFmpegError(f"Merge failed: {error_msg}")

            if not output_path.exists():
                raise FFmpegError("Output file not created")

            logger.info(f"Videos merged successfully: {output_path}")
            return output_path

        finally:
            # Cleanup input files
            for path in input_paths:
                if path.exists():
                    os.remove(path)
            # Cleanup trimmed files (excluding those that are same as input)
            for path in trimmed_paths:
                if path.exists() and path not in input_paths:
                    os.remove(path)
            if concat_file.exists():
                os.remove(concat_file)

    async def process_reference_video(self, input_path: Path) -> Path:
        """
        Validate and optionally trim/compress a reference video.
        - Duration limit: 30.0s max
        - File size limit: 1.0 GB max (1024 * 1024 * 1024 bytes)
        
        Args:
            input_path: Path to the local video file.
            
        Returns:
            Path to the processed video file, or original path if no changes are needed.
        """
        if not input_path.exists():
            return input_path

        try:
            duration = await self.get_video_duration(input_path)
        except Exception as e:
            logger.warning(f"Could not read reference video duration: {e}")
            duration = 0.0

        size = input_path.stat().st_size
        max_duration = 30.0
        max_size = 1024 * 1024 * 1024  # 1.0 GB

        needs_trim = duration > max_duration
        needs_compress = size > max_size

        if not needs_trim and not needs_compress:
            logger.debug(f"Reference video {input_path.name} is within limits (duration={duration:.1f}s, size={size / (1024*1024):.1f}MB).")
            return input_path

        job_id = str(uuid.uuid4())[:8]
        output_path = self.tmp_dir / f"processed_ref_{job_id}.mp4"

        cmd = ["ffmpeg", "-y"]
        
        # If we need to trim, use -ss before -i for fast seek
        if needs_trim:
            cmd.extend(["-ss", "0"])

        cmd.extend(["-i", str(input_path)])

        if needs_trim:
            cmd.extend(["-t", str(max_duration)])

        if needs_compress:
            logger.info(
                f"Compressing reference video {input_path.name} (size={size / (1024*1024):.1f}MB > 1.0GB)"
            )
            # Re-encode to H.264 with CRF 28 for good compression and AAC audio
            cmd.extend([
                "-c:v", "libx264",
                "-crf", "28",
                "-preset", "faster",
                "-c:a", "aac",
                "-pix_fmt", "yuv420p"  # Ensure compatibility
            ])
        else:
            logger.info(
                f"Trimming reference video {input_path.name} (duration={duration:.1f}s > 30s)"
            )
            # Fast cut without re-encoding
            cmd.extend(["-c", "copy"])

        cmd.append(str(output_path))

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                timeout=_FFMPEG_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise FFmpegError(f"Reference video processing timed out after {_FFMPEG_TIMEOUT}s")

        if result.returncode != 0:
            error_msg = result.stderr.decode(errors='replace') if result.stderr else "Unknown error"
            logger.error(f"FFmpeg reference video processing failed: {error_msg}")
            raise FFmpegError(f"Reference video processing failed: {error_msg}")

        if not output_path.exists():
            raise FFmpegError("Processed reference video file not created")

        logger.info(f"Reference video processed successfully: {output_path}")
        return output_path

    async def get_video_duration(self, video_path: Path) -> float:
        """Get video duration in seconds using FFprobe."""
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]

        # Use subprocess.run in thread for Windows compatibility
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                timeout=_FFMPEG_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise FFmpegError(f"FFprobe timed out after {_FFMPEG_TIMEOUT}s")

        if result.returncode != 0:
            raise FFmpegError("Failed to get video duration")

        try:
            return float(result.stdout.decode().strip())
        except ValueError:
            raise FFmpegError("Invalid duration value")

    def cleanup_file(self, file_path: Path) -> None:
        """Remove a temporary file."""
        if file_path.exists():
            os.remove(file_path)
            logger.debug(f"Cleaned up: {file_path}")

    async def cleanup_old_files(self, max_age_hours: int = 24) -> int:
        """
        Remove temporary files older than max_age_hours.

        Returns:
            Number of files removed
        """
        import time

        count = 0
        now = time.time()
        max_age_seconds = max_age_hours * 3600

        for file_path in self.tmp_dir.iterdir():
            if file_path.is_file():
                age = now - file_path.stat().st_mtime
                if age > max_age_seconds:
                    file_path.unlink()
                    count += 1

        if count > 0:
            logger.info(f"Cleaned up {count} old temporary files")

        return count


# Global instance
ffmpeg_service = FFmpegService()
