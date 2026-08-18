"""media.video — Step 4 section 27.

Uses ffmpeg's own `lavfi` synthetic source to generate a tiny local test clip at test
time — nothing is committed to the repository, matching "do NOT commit large video
assets." Skipped outright when ffmpeg/ffprobe are not on the machine running the
tests, so this suite stays environment-sensitive by design rather than by accident.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_news_editor.media.video import (
    CorruptVideoError,
    VideoProcessingError,
    VideoProcessingTimeoutError,
    VideoTooLargeError,
    ffmpeg_available,
    probe,
    process_video,
)

pytestmark = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not installed")


def _make_clip(
    path: Path, *, duration: float = 2.0, size: str = "320x240", with_audio: bool = True
) -> Path:
    command = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate=15",
    ]
    if with_audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    command += ["-c:v", "libx264", "-preset", "ultrafast"]
    command += ["-c:a", "aac"] if with_audio else ["-an"]
    command += [str(path)]
    subprocess.run(command, capture_output=True, timeout=30, check=True)  # noqa: S603
    return path


class TestProbe:
    def test_probes_duration_dimensions_and_audio(self, tmp_path: Path) -> None:
        clip = _make_clip(tmp_path / "clip.mp4", duration=2.0, size="320x240", with_audio=True)
        info = probe(clip)
        assert 1.5 <= info.duration_seconds <= 3.0
        assert info.width == 320
        assert info.height == 240
        assert info.has_audio is True

    def test_probes_a_silent_clip_as_no_audio(self, tmp_path: Path) -> None:
        clip = _make_clip(tmp_path / "clip.mp4", with_audio=False)
        info = probe(clip)
        assert info.has_audio is False

    def test_a_corrupt_file_raises_corrupt_video_error(self, tmp_path: Path) -> None:
        fake = tmp_path / "fake.mp4"
        fake.write_bytes(b"not a real video file" * 20)
        with pytest.raises(CorruptVideoError):
            probe(fake)


class TestProcessVideo:
    def test_a_valid_clip_is_transcoded(self, tmp_path: Path) -> None:
        clip = _make_clip(tmp_path / "clip.mp4", duration=2.0, size="640x480")
        result = process_video(clip, tmp_path / "out.mp4", target_bytes=5_000_000)
        assert result.path.exists()
        assert result.size_bytes > 0
        assert result.width % 2 == 0
        assert result.height % 2 == 0

    def test_output_respects_the_max_dimension(self, tmp_path: Path) -> None:
        clip = _make_clip(tmp_path / "clip.mp4", duration=1.0, size="800x600")
        result = process_video(clip, tmp_path / "out.mp4", max_dimension=320)
        assert max(result.width, result.height) <= 320

    def test_a_too_long_source_is_rejected_before_transcoding(self, tmp_path: Path) -> None:
        clip = _make_clip(tmp_path / "clip.mp4", duration=2.0)
        with pytest.raises(VideoTooLargeError):
            process_video(clip, tmp_path / "out.mp4", max_duration_seconds=1.0)
        assert not (tmp_path / "out.mp4").exists()

    def test_a_corrupt_source_raises_corrupt_video_error(self, tmp_path: Path) -> None:
        fake = tmp_path / "fake.mp4"
        fake.write_bytes(b"garbage" * 50)
        with pytest.raises(CorruptVideoError):
            process_video(fake, tmp_path / "out.mp4")

    def test_a_silent_source_produces_a_silent_output_without_error(self, tmp_path: Path) -> None:
        clip = _make_clip(tmp_path / "clip.mp4", duration=1.0, with_audio=False)
        result = process_video(clip, tmp_path / "out.mp4")
        info = probe(result.path)
        assert info.has_audio is False


class TestTimeout:
    def test_an_unreasonably_short_timeout_raises_processing_timeout(self, tmp_path: Path) -> None:
        clip = _make_clip(tmp_path / "clip.mp4", duration=2.0, size="640x480")
        with pytest.raises(VideoProcessingTimeoutError):
            process_video(clip, tmp_path / "out.mp4", timeout_seconds=0.001)
        assert not (tmp_path / "out.mp4").exists()

    def test_probe_itself_respects_a_short_timeout(self, tmp_path: Path) -> None:
        clip = _make_clip(tmp_path / "clip.mp4", duration=1.0)
        with pytest.raises(VideoProcessingTimeoutError):
            probe(clip, timeout_seconds=0.0001)


class TestHardSizeCap:
    def test_a_target_that_cannot_fit_the_telegram_cap_raises(self, tmp_path: Path) -> None:
        """target_bytes so large the computed bitrate blows through the real Telegram
        hard cap — process_video must still catch it, not just trust the target."""
        from ai_news_editor.media import limits as limits_module

        clip = _make_clip(tmp_path / "clip.mp4", duration=1.0, size="640x480")
        original_cap = limits_module.MAX_TELEGRAM_VIDEO_BYTES
        import ai_news_editor.media.video as video_module

        video_module.MAX_TELEGRAM_VIDEO_BYTES = 1  # impossible to satisfy
        try:
            with pytest.raises(VideoProcessingError):
                process_video(clip, tmp_path / "out.mp4")
        finally:
            video_module.MAX_TELEGRAM_VIDEO_BYTES = original_cap
