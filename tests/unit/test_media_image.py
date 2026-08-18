"""media.image.process_image — Step 4 section 26. Small generated fixtures only."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from ai_news_editor.media.image import (
    CorruptImageError,
    ImageProcessingError,
    ImageTooLargeError,
    process_image,
)


def _save(tmp_path: Path, name: str, image: Image.Image, **save_kwargs: object) -> Path:
    path = tmp_path / name
    image.save(path, **save_kwargs)
    return path


class TestFormats:
    def test_a_jpeg_input_is_processed(self, tmp_path: Path) -> None:
        src = _save(tmp_path, "in.jpg", Image.new("RGB", (400, 300), "red"), format="JPEG")
        result = process_image(src, tmp_path / "out.jpg")
        assert result.path.exists()
        assert Image.open(result.path).format == "JPEG"

    def test_a_png_input_is_processed(self, tmp_path: Path) -> None:
        src = _save(tmp_path, "in.png", Image.new("RGB", (400, 300), "blue"), format="PNG")
        result = process_image(src, tmp_path / "out.jpg")
        assert result.path.exists()

    def test_a_webp_input_is_processed(self, tmp_path: Path) -> None:
        src = _save(tmp_path, "in.webp", Image.new("RGB", (400, 300), "green"), format="WEBP")
        result = process_image(src, tmp_path / "out.jpg")
        assert result.path.exists()


class TestAlpha:
    def test_an_rgba_image_is_flattened_without_error(self, tmp_path: Path) -> None:
        image = Image.new("RGBA", (200, 200), (255, 0, 0, 128))
        src = _save(tmp_path, "in.png", image, format="PNG")
        result = process_image(src, tmp_path / "out.jpg")
        output = Image.open(result.path)
        assert output.mode == "RGB"


class TestExifOrientation:
    def test_exif_orientation_is_normalized(self, tmp_path: Path) -> None:
        image = Image.new("RGB", (300, 200), "purple")
        exif = image.getexif()
        exif[0x0112] = 6  # "rotate 90 CW" orientation tag
        src = tmp_path / "in.jpg"
        image.save(src, format="JPEG", exif=exif)

        result = process_image(src, tmp_path / "out.jpg")

        # Orientation 6 swaps width/height once applied.
        assert (result.width, result.height) == (200, 300)


class TestResize:
    def test_a_large_image_is_downscaled_preserving_aspect_ratio(self, tmp_path: Path) -> None:
        image = Image.new("RGB", (4000, 2000), "orange")
        src = _save(tmp_path, "in.jpg", image, format="JPEG")

        result = process_image(src, tmp_path / "out.jpg", max_dimension=1000)

        assert result.width == 1000
        assert result.height == 500  # aspect ratio 2:1 preserved

    def test_a_small_image_is_never_upscaled(self, tmp_path: Path) -> None:
        image = Image.new("RGB", (100, 80), "cyan")
        src = _save(tmp_path, "in.jpg", image, format="JPEG")

        result = process_image(src, tmp_path / "out.jpg", max_dimension=2000)

        assert (result.width, result.height) == (100, 80)


class TestCompression:
    def test_output_size_is_under_the_target(self, tmp_path: Path) -> None:
        image = Image.new("RGB", (1500, 1500), "magenta")
        src = _save(tmp_path, "in.jpg", image, format="JPEG", quality=100)

        result = process_image(src, tmp_path / "out.jpg", target_bytes=200_000)

        assert result.size_bytes <= 200_000
        assert result.size_bytes == (tmp_path / "out.jpg").stat().st_size

    def test_metadata_is_not_required_and_not_carried_over(self, tmp_path: Path) -> None:
        image = Image.new("RGB", (300, 300), "yellow")
        exif = image.getexif()
        exif[0x010E] = "a secret description that must not survive processing"
        src = tmp_path / "in.jpg"
        image.save(src, format="JPEG", exif=exif)

        result = process_image(src, tmp_path / "out.jpg")

        output_exif = Image.open(result.path).getexif()
        assert 0x010E not in output_exif


class TestCorruptInput:
    def test_a_non_image_file_raises_corrupt_image_error(self, tmp_path: Path) -> None:
        src = tmp_path / "in.jpg"
        src.write_bytes(b"this is not an image, just text pretending to be one" * 10)
        with pytest.raises(CorruptImageError):
            process_image(src, tmp_path / "out.jpg")

    def test_a_truncated_jpeg_raises_corrupt_image_error(self, tmp_path: Path) -> None:
        good = tmp_path / "good.jpg"
        Image.new("RGB", (300, 300), "red").save(good, format="JPEG")
        truncated = tmp_path / "truncated.jpg"
        truncated.write_bytes(good.read_bytes()[:200])
        with pytest.raises(CorruptImageError):
            process_image(truncated, tmp_path / "out.jpg")


class TestDecompressionBomb:
    def test_an_image_over_the_pixel_ceiling_is_rejected(self, tmp_path: Path) -> None:
        from ai_news_editor.media import image as image_module

        original_ceiling = image_module.MAX_DECODED_PIXELS
        image_module.MAX_DECODED_PIXELS = 1000  # tiny ceiling for a fast, real test
        try:
            src = _save(tmp_path, "in.jpg", Image.new("RGB", (200, 200), "red"), format="JPEG")
            with pytest.raises(ImageTooLargeError):
                process_image(src, tmp_path / "out.jpg")
        finally:
            image_module.MAX_DECODED_PIXELS = original_ceiling


class TestUnreachableTargetSize:
    def test_a_target_too_small_to_ever_reach_raises_processing_error(self, tmp_path: Path) -> None:
        image = Image.new("RGB", (1000, 1000), "red")
        src = _save(tmp_path, "in.jpg", image, format="JPEG")
        with pytest.raises(ImageProcessingError):
            process_image(src, tmp_path / "out.jpg", target_bytes=1)
