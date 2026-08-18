"""Video probe/transcode via ffmpeg/ffprobe — Step 4 (AI News Agent v2).

No Python video library is used: ffmpeg is the de-facto standard for exactly this job,
and re-implementing any part of video decoding/encoding in Python would be both slower
and far more likely to be wrong. Availability is never assumed — ``ffmpeg_available()``
checks the actual runner before anything here is invoked, and ``media.pipeline`` falls
back to an image (or text-only) when it is missing, per the module's own fallback
contract (see section 13 of the Step 4 spec / docs/media.md).

Every subprocess call passes a fixed, fully-qualified argument list — never a shell
string, never string-interpolated user input — and every call has a bounded timeout.
There is no infinite ffmpeg process: a hang is treated exactly like any other
processing failure.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ai_news_editor.domain.errors import FatalError
from ai_news_editor.media.limits import (
    FFMPEG_TIMEOUT_SECONDS,
    FFPROBE_TIMEOUT_SECONDS,
    MAX_TELEGRAM_VIDEO_BYTES,
    MAX_VIDEO_DIMENSION,
    MAX_VIDEO_DURATION_SECONDS,
    TARGET_VIDEO_BYTES,
)
from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

#: A conservative floor so a target so small ffmpeg cannot produce anything watchable
#: is rejected up front rather than producing a technically-valid, useless file.
_MIN_VIDEO_BITRATE_KBPS = 100
_AUDIO_BITRATE_KBPS = 96


class VideoUnavailableError(FatalError):
    """ffmpeg/ffprobe are not installed on this machine."""


class CorruptVideoError(FatalError):
    """The file does not probe as a readable video at all."""


class VideoTooLargeError(FatalError):
    """The source's duration exceeds what this channel ever posts, checked via
    ffprobe before any transcoding is attempted."""


class VideoProcessingError(FatalError):
    """ffmpeg ran and exited non-zero, or produced a file still over the hard cap."""


class VideoProcessingTimeoutError(FatalError):
    """ffmpeg exceeded its wall-clock budget and was killed."""


@dataclass(frozen=True, slots=True)
class VideoProbe:
    duration_seconds: float
    width: int
    height: int
    has_audio: bool


@dataclass(frozen=True, slots=True)
class ProcessedVideo:
    path: Path
    width: int
    height: int
    size_bytes: int
    duration_seconds: float


def ffmpeg_available() -> bool:
    """Whether both ``ffmpeg`` and ``ffprobe`` are on ``PATH`` on this machine."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe(src: Path, *, timeout_seconds: float = FFPROBE_TIMEOUT_SECONDS) -> VideoProbe:
    """Read a video's duration, dimensions and audio presence via ffprobe.

    Raises:
        VideoUnavailableError: ffprobe is not installed.
        CorruptVideoError: the file does not probe as a readable video.
        VideoProcessingTimeoutError: ffprobe did not finish within its budget.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise VideoUnavailableError("ffprobe is not installed on this machine")

    command = [
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(src),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed arg list, no shell, resolved executable
            command, capture_output=True, text=True, timeout=timeout_seconds, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoProcessingTimeoutError(f"ffprobe on {src} exceeded its time budget") from exc

    if completed.returncode != 0:
        raise CorruptVideoError(f"{src} does not probe as a readable video: {completed.stderr}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CorruptVideoError(f"{src}: ffprobe produced unparseable output: {exc}") from exc

    video_stream = next(
        (s for s in payload.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if video_stream is None:
        raise CorruptVideoError(f"{src} has no readable video stream")
    has_audio = any(s.get("codec_type") == "audio" for s in payload.get("streams", []))

    duration_raw = payload.get("format", {}).get("duration") or video_stream.get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError) as exc:
        raise CorruptVideoError(f"{src}: no usable duration in ffprobe output") from exc

    try:
        width = int(video_stream["width"])
        height = int(video_stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptVideoError(f"{src}: no usable dimensions in ffprobe output") from exc

    return VideoProbe(duration_seconds=duration, width=width, height=height, has_audio=has_audio)


def process_video(
    src: Path,
    dest: Path,
    *,
    target_bytes: int = TARGET_VIDEO_BYTES,
    max_dimension: int = MAX_VIDEO_DIMENSION,
    max_duration_seconds: float = MAX_VIDEO_DURATION_SECONDS,
    timeout_seconds: float = FFMPEG_TIMEOUT_SECONDS,
) -> ProcessedVideo:
    """Probe, sanity-check and transcode ``src`` into a Telegram-safe MP4 at ``dest``.

    Raises:
        VideoUnavailableError: ffmpeg/ffprobe are not installed.
        CorruptVideoError: the file does not probe as a readable video.
        VideoTooLargeError: the source is longer than ``max_duration_seconds`` —
            rejected before transcoding is even attempted, so a multi-hour source never
            spends this job's time budget on one file.
        VideoProcessingTimeoutError: ffmpeg did not finish within its budget.
        VideoProcessingError: ffmpeg exited non-zero, or the output is still over the
            hard Telegram size cap even after a bitrate-targeted encode.
    """
    if not ffmpeg_available():
        raise VideoUnavailableError("ffmpeg is not installed on this machine")

    info = probe(src)
    if info.duration_seconds > max_duration_seconds:
        raise VideoTooLargeError(
            f"{src} is {info.duration_seconds:.0f}s, over the {max_duration_seconds:.0f}s "
            "this channel posts — rejected before transcoding"
        )

    width, height = _scaled_dimensions(info.width, info.height, max_dimension)
    video_kbps = max(
        _MIN_VIDEO_BITRATE_KBPS,
        int((target_bytes * 8 / 1000) / max(info.duration_seconds, 1)) - _AUDIO_BITRATE_KBPS,
    )

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None  # ffmpeg_available() already confirmed this
    command = [
        ffmpeg, "-y",
        "-i", str(src),
        "-vf", f"scale={width}:{height}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-b:v", f"{video_kbps}k",
        "-maxrate", f"{video_kbps}k",
        "-bufsize", f"{video_kbps * 2}k",
        "-movflags", "+faststart",
    ]
    command += ["-c:a", "aac", "-b:a", f"{_AUDIO_BITRATE_KBPS}k"] if info.has_audio else ["-an"]
    command.append(str(dest))

    try:
        completed = subprocess.run(  # noqa: S603 - fixed arg list, no shell, resolved executable
            command, capture_output=True, text=True, timeout=timeout_seconds, check=False
        )
    except subprocess.TimeoutExpired as exc:
        dest.unlink(missing_ok=True)
        raise VideoProcessingTimeoutError(f"ffmpeg on {src} exceeded its time budget") from exc

    if completed.returncode != 0 or not dest.exists():
        dest.unlink(missing_ok=True)
        raise VideoProcessingError(f"ffmpeg failed on {src}: {completed.stderr}")

    size = dest.stat().st_size
    if size > MAX_TELEGRAM_VIDEO_BYTES:
        dest.unlink(missing_ok=True)
        raise VideoProcessingError(
            f"{src} still produced {size} bytes, over the {MAX_TELEGRAM_VIDEO_BYTES} byte "
            "Telegram limit even after a bitrate-targeted encode"
        )

    logger.info(
        "media_processing",
        extra={
            "input_bytes": src.stat().st_size,
            "output_bytes": size,
            "dimensions": f"{width}x{height}",
        },
    )
    return ProcessedVideo(
        path=dest, width=width, height=height, size_bytes=size,
        duration_seconds=info.duration_seconds,
    )


def _scaled_dimensions(width: int, height: int, max_dimension: int) -> tuple[int, int]:
    """Downscale only, preserving aspect ratio, rounded to even numbers — libx264
    requires even width/height for the default (non-4:4:4) chroma subsampling."""
    largest = max(width, height)
    if largest > max_dimension:
        ratio = max_dimension / largest
        width, height = round(width * ratio), round(height * ratio)
    return (width if width % 2 == 0 else width - 1, height if height % 2 == 0 else height - 1)


__all__ = [
    "CorruptVideoError",
    "ProcessedVideo",
    "VideoProbe",
    "VideoProcessingError",
    "VideoProcessingTimeoutError",
    "VideoTooLargeError",
    "VideoUnavailableError",
    "ffmpeg_available",
    "probe",
    "process_video",
]
