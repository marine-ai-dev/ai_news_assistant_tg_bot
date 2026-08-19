"""Image decode/normalize/compress — Step 4 (AI News Agent v2).

Pillow is the only image dependency (see ``pyproject.toml``): mature, already the
de-facto standard for exactly this job, no reason to hand-roll JPEG encoding.

Every step here is deliberately conservative about not destroying quality for no
reason: resize only when the source is actually larger than the target, never upscale
a small source image, and compress iteratively (step the JPEG quality down, and only
resize further if quality alone cannot reach the target) rather than jumping straight
to an aggressive fixed setting.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps
from PIL import UnidentifiedImageError as PillowUnidentifiedImageError

from ai_news_editor.domain.errors import FatalError
from ai_news_editor.media.limits import MAX_PHOTO_DIMENSION, TARGET_PHOTO_BYTES
from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

#: A generous but finite pixel-count ceiling, checked before Pillow decodes the full
#: image — the guard against a "decompression bomb" (a tiny file that decodes to an
#: enormous bitmap and exhausts memory). Comfortably above any real photo a news
#: article would embed; comfortably below what would actually hurt a CI runner.
MAX_DECODED_PIXELS = 40_000_000  # e.g. ~7100x5600

#: JPEG quality steps tried in order, highest first, until the file fits the target.
_QUALITY_STEPS = (85, 75, 65, 55, 45, 35)


class CorruptImageError(FatalError):
    """The file does not decode as a supported image at all."""


class ImageTooLargeError(FatalError):
    """The decoded image exceeds the pixel-count safety ceiling."""


class ImageProcessingError(FatalError):
    """A decodable image could not be turned into a Telegram-safe file."""


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    path: Path
    width: int
    height: int
    size_bytes: int


def process_image(
    src: Path,
    dest: Path,
    *,
    target_bytes: int = TARGET_PHOTO_BYTES,
    max_dimension: int = MAX_PHOTO_DIMENSION,
) -> ProcessedImage:
    """Decode, normalize and compress ``src`` into a Telegram-safe JPEG at ``dest``.

    Raises:
        CorruptImageError: the file does not decode as an image Pillow understands.
        ImageTooLargeError: the decoded image exceeds ``MAX_DECODED_PIXELS`` — checked
            before any resize is attempted, so a decompression-bomb-shaped file never
            gets as far as being resized or re-encoded.
        ImageProcessingError: decoded and within bounds, but could not be compressed
            under ``target_bytes`` even at the lowest quality step tried.
    """
    try:
        with Image.open(src) as opened:
            opened.load()  # force full decode now, not lazily inside the with-block
            image = ImageOps.exif_transpose(opened) or opened
    except PillowUnidentifiedImageError as exc:
        raise CorruptImageError(f"{src} does not decode as an image: {exc}") from exc
    except OSError as exc:
        raise CorruptImageError(f"{src} could not be read as an image: {exc}") from exc

    pixels = image.width * image.height
    if pixels > MAX_DECODED_PIXELS:
        raise ImageTooLargeError(
            f"{src} decodes to {image.width}x{image.height} ({pixels} pixels), over the "
            f"{MAX_DECODED_PIXELS}-pixel safety ceiling"
        )

    image = _flatten_to_rgb(image)
    image = _resize_within(image, max_dimension)

    for quality in _QUALITY_STEPS:
        # No `exif=`/`icc_profile=` passed to save(): Pillow writes no metadata beyond
        # what encoding itself requires, satisfying "strip unnecessary metadata"
        # without extra code — omission is the strip.
        image.save(dest, format="JPEG", quality=quality, optimize=True)
        size = dest.stat().st_size
        if size <= target_bytes:
            logger.info(
                "media_processing",
                extra={
                    "input_bytes": src.stat().st_size,
                    "output_bytes": size,
                    "dimensions": f"{image.width}x{image.height}",
                },
            )
            return ProcessedImage(
                path=dest, width=image.width, height=image.height, size_bytes=size
            )

    raise ImageProcessingError(
        f"{src} could not be compressed under {target_bytes} bytes even at the lowest "
        f"quality step ({_QUALITY_STEPS[-1]})"
    )


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """JPEG has no alpha channel — an RGBA/P/LA source is composited onto white first,
    the same way a browser renders a transparent image over a white page, rather than
    letting Pillow silently drop the alpha and leave black where it was transparent."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        background = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _resize_within(image: Image.Image, max_dimension: int) -> Image.Image:
    """Downscale only, preserving aspect ratio; never upscale a small source.

    A source already at or under ``max_dimension`` — including anything at or below
    ``MIN_UPSCALE_GUARD_DIMENSION`` — is returned untouched: this function never grows
    an image, only shrinks one that is actually larger than the target.
    """
    largest_side = max(image.width, image.height)
    if largest_side <= max_dimension:
        return image
    ratio = max_dimension / largest_side
    new_size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
    return image.resize(new_size, Image.LANCZOS)


__all__ = [
    "MAX_DECODED_PIXELS",
    "CorruptImageError",
    "ImageProcessingError",
    "ImageTooLargeError",
    "ProcessedImage",
    "process_image",
]
