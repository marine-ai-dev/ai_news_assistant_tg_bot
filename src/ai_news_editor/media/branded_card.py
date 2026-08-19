"""The branded card generator — Step 6B (AI News Agent v2), the universal safe fallback.

Layer (C) of the four-layer media strategy: (A) explicit first-party licensed media,
(B) verified open-license media, **(C) this module**, (D) text/link-preview. Reached
only when neither A nor B produced anything — and unlike them, this layer can never
come back empty, because it does not depend on the article having any media at all.

Pillow only, no generative AI, no network access, no third-party brand artwork:
- The background is an abstract linear gradient between two flat colors, picked
  deterministically from an expanded named palette (``_PALETTE`` — blue, teal, green,
  purple, pink, orange, red, yellow, indigo; Step 6C) keyed by category *and*
  headline, not category alone — a category-only key means one color forever no
  matter how many times that category posts in a row, which real-channel visual
  review flagged as "just blue and green" once NEWS/RESEARCH dominated a real soak's
  volume. Still fully deterministic and reproducible (same category+headline always
  picks the same entry), just not fixed per category.
- Each category still gets one fixed, recognizable *icon drawn with Pillow's own
  vector primitives* (circles, lines, polygons), independent of the rotating color —
  this module does not reuse ``rendering.style``'s per-category emoji as a font glyph:
  the only font committed to this repo (DejaVu Sans, see ``media/assets/fonts/``) has
  no emoji coverage at all — rendering U+1F680 etc. with it produces empty tofu boxes,
  verified directly before writing this module.
- The only font used is the committed DejaVu Sans/DejaVu Sans Bold (Bitstream Vera
  license, see ``media/assets/fonts/LICENSE_DEJAVU.txt``) — loaded from a path relative
  to this module's own file, so it works regardless of the process's working directory
  and needs no network access at draw time.
- The headline is truncated to a short, scannable excerpt (not the full post body) and
  auto-fit to the canvas by trying progressively smaller font sizes, never by
  overflowing or clipping text off-canvas.

``ProcessedMedia.source_url`` is set to ``""`` here — there is no source URL, because
nothing was downloaded from anywhere. A caller building a `MediaAsset` from this result
should use ``MediaOrigin.EDITORIAL_ASSET`` (not ``SOURCE_MEDIA``) and leave its own
``source_url`` unset, exactly as that enum member's docstring already describes ("made
for the channel — a diagram, a cover").
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ai_news_editor.domain.enums import EditorialCategory
from ai_news_editor.media.limits import BRANDED_CARD_HEIGHT, BRANDED_CARD_WIDTH
from ai_news_editor.media.models import DiscoveryMethod, MediaKind, ProcessedMedia
from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

_FONTS_DIR = Path(__file__).parent / "assets" / "fonts"
_REGULAR_FONT_PATH = _FONTS_DIR / "DejaVuSans.ttf"
_BOLD_FONT_PATH = _FONTS_DIR / "DejaVuSans-Bold.ttf"

_MARGIN = 60
_ICON_BADGE_RADIUS = 56
_HEADLINE_FONT_SIZES = (64, 56, 48, 42, 36)
_MAX_HEADLINE_LINES = 4
_MAX_HEADLINE_CHARS = 140
_MAX_SOURCE_LABEL_CHARS = 60
_WHITE = (255, 255, 255)
_TEXT_SHADOW = (0, 0, 0)

#: Step 6C: an expanded, named (top, bottom) gradient pool — an abstract identity, not
#: a copy of any real product's or competitor's palette. Real-channel visual review of
#: the first soak found the cards read as "just blue and green" because NEWS (blue) and
#: RESEARCH (green) happen to dominate real story volume; a purely category-keyed
#: palette can never fix that on its own, since one category always gets one color no
#: matter how many times it posts in a row. Every one of the seven requested hues
#: (blue, green, purple, pink, orange, red, yellow) is present at least once below.
_PALETTE: tuple[tuple[str, tuple[int, int, int], tuple[int, int, int]], ...] = (
    ("blue", (30, 64, 175), (59, 130, 246)),
    ("teal", (13, 94, 84), (20, 184, 166)),
    ("green", (6, 78, 59), (16, 185, 129)),
    ("purple", (76, 29, 149), (168, 85, 247)),
    ("pink", (131, 24, 67), (244, 114, 182)),
    ("orange", (146, 64, 14), (251, 146, 60)),
    ("red", (127, 29, 29), (248, 113, 113)),
    ("yellow", (113, 84, 6), (250, 204, 21)),
    ("indigo", (30, 41, 99), (99, 102, 241)),
)


def _pick_palette_entry(
    category: EditorialCategory, headline: str
) -> tuple[str, tuple[int, int, int], tuple[int, int, int]]:
    """A deterministic, headline-varying pick from the full palette — "rotating" in
    the sense that two different stories in the same category get different colors,
    but reproducible: the same category+headline always picks the same entry, which
    is what keeps this module's own determinism guarantee (see its test suite) and
    keeps a retried/resumed publish from ever changing a card's color mid-flight.
    """
    digest = hashlib.sha256(f"{category.value}:{headline}".encode()).hexdigest()
    index = int(digest, 16) % len(_PALETTE)
    return _PALETTE[index]


#: Short, plain-language Ukrainian label per category — shown next to the icon.
#: Deliberately not the emoji and not the English enum name: a reader who never sees
#: the post's own text still gets a category name in the language the channel writes.
_CATEGORY_LABELS: dict[EditorialCategory, str] = {
    EditorialCategory.NEWS: "НОВИНИ",
    EditorialCategory.AI_TOOL: "AI-ІНСТРУМЕНТ",
    EditorialCategory.FREE_DEAL: "БЕЗКОШТОВНО",
    EditorialCategory.AI_LIFEHACK: "ЛАЙФХАК",
    EditorialCategory.PROMPT_WORKFLOW: "PROMPT",
    EditorialCategory.EXPLAINER: "ПОЯСНЮЄМО",
    EditorialCategory.RESEARCH: "ДОСЛІДЖЕННЯ",
    EditorialCategory.WEEKLY_DIGEST: "ОГЛЯД ТИЖНЯ",
}


class BrandedCardError(Exception):
    """Card generation failed. Should be treated exactly like any other media
    rejection by the caller — fall back to text-only, never raise past ``select_media``."""


def generate_branded_card(
    *, category: EditorialCategory, headline: str, source_label: str, dest: Path
) -> ProcessedMedia:
    """Draw an original, locally-generated card for one post and save it to ``dest``.

    Deterministic given the same inputs — no randomness, so re-running for the same
    post produces the same visual identity. Never touches the network.

    Raises:
        BrandedCardError: the committed font could not be loaded, or the finished
            image could not be written to ``dest`` — both should be rare (a packaging
            or disk-space problem, not an ordinary per-post failure), but the caller
            should still treat this exactly like any other media rejection.
    """
    try:
        bold_30 = ImageFont.truetype(str(_BOLD_FONT_PATH), 30)
        regular_26 = ImageFont.truetype(str(_REGULAR_FONT_PATH), 26)
    except OSError as exc:
        raise BrandedCardError(f"could not load the committed font: {exc}") from exc

    color_name, top, bottom = _pick_palette_entry(category, headline)
    image = Image.new("RGB", (BRANDED_CARD_WIDTH, BRANDED_CARD_HEIGHT))
    _paint_gradient(image, top, bottom)
    draw = ImageDraw.Draw(image)

    badge_center = (_MARGIN + _ICON_BADGE_RADIUS, _MARGIN + _ICON_BADGE_RADIUS)
    _draw_icon_badge(draw, category, top, badge_center, _ICON_BADGE_RADIUS)

    label_x = badge_center[0] + _ICON_BADGE_RADIUS + 24
    label_y = badge_center[1] - 15
    _draw_text_with_shadow(draw, (label_x, label_y), _CATEGORY_LABELS[category], bold_30, _WHITE)

    headline_top = badge_center[1] + _ICON_BADGE_RADIUS + 50
    headline_area_width = BRANDED_CARD_WIDTH - 2 * _MARGIN
    headline_area_height = BRANDED_CARD_HEIGHT - headline_top - 90
    try:
        _draw_wrapped_headline(
            draw,
            _shorten(headline, _MAX_HEADLINE_CHARS),
            origin=(_MARGIN, headline_top),
            max_width=headline_area_width,
            max_height=headline_area_height,
        )
    except OSError as exc:
        raise BrandedCardError(f"could not load the committed font: {exc}") from exc

    source_text = f"Джерело: {_shorten(source_label, _MAX_SOURCE_LABEL_CHARS)}"
    source_y = BRANDED_CARD_HEIGHT - _MARGIN - 26
    _draw_text_with_shadow(draw, (_MARGIN, source_y), source_text, regular_26, _WHITE)

    try:
        image.save(dest, format="JPEG", quality=90, optimize=True)
        size_bytes = dest.stat().st_size
    except OSError as exc:
        raise BrandedCardError(f"could not write the card to {dest}: {exc}") from exc

    logger.info(
        "branded_card_generated",
        extra={"category": category.value, "size_bytes": size_bytes, "card_color": color_name},
    )
    return ProcessedMedia(
        path=str(dest),
        kind=MediaKind.IMAGE,
        width=BRANDED_CARD_WIDTH,
        height=BRANDED_CARD_HEIGHT,
        size_bytes=size_bytes,
        source_url="",
        source_method=DiscoveryMethod.GENERATED_CARD,
    )


def _paint_gradient(
    image: Image.Image, top: tuple[int, int, int], bottom: tuple[int, int, int]
) -> None:
    """A simple top-to-bottom linear gradient — abstract, not a photo, not a logo."""
    height = image.height
    for y in range(height):
        t = y / max(1, height - 1)
        row = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        ImageDraw.Draw(image).line([(0, y), (image.width, y)], fill=row)


def _draw_icon_badge(
    draw: ImageDraw.ImageDraw,
    category: EditorialCategory,
    icon_color: tuple[int, int, int],
    center: tuple[int, int],
    radius: int,
) -> None:
    """A translucent-looking circular badge with a small deterministic vector icon
    inside — never a font glyph (see module docstring for why), never a third-party
    logo or brand mark. ``icon_color`` is the card's own picked gradient color (Step
    6C), not a fixed per-category one, so the icon always reads against the card it is
    actually drawn on."""
    cx, cy = center
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(255, 255, 255, 255),
        outline=None,
    )
    _ICON_DRAWERS[category](draw, center, radius - 20, icon_color)


_RGB = tuple[int, int, int]


def _draw_rocket(draw: ImageDraw.ImageDraw, c: tuple[int, int], r: int, color: _RGB) -> None:
    cx, cy = c
    draw.polygon(
        [(cx, cy - r), (cx - r * 0.5, cy + r * 0.6), (cx + r * 0.5, cy + r * 0.6)], fill=color
    )
    draw.ellipse((cx - r * 0.18, cy - r * 0.1, cx + r * 0.18, cy + r * 0.26), fill=(255, 255, 255))


def _draw_wrench(draw: ImageDraw.ImageDraw, c: tuple[int, int], r: int, color: _RGB) -> None:
    cx, cy = c
    width = max(6, r // 4)
    draw.line((cx - r * 0.7, cy + r * 0.7, cx + r * 0.7, cy - r * 0.7), fill=color, width=width)
    ring_width = max(4, r // 6)
    draw.ellipse(
        (cx - r * 0.95, cy - r * 0.95, cx - r * 0.35, cy - r * 0.35),
        outline=color,
        width=ring_width,
    )
    draw.ellipse(
        (cx + r * 0.35, cy + r * 0.35, cx + r * 0.95, cy + r * 0.95),
        outline=color,
        width=ring_width,
    )


def _draw_gift(draw: ImageDraw.ImageDraw, c: tuple[int, int], r: int, color: _RGB) -> None:
    cx, cy = c
    draw.rectangle((cx - r * 0.7, cy - r * 0.2, cx + r * 0.7, cy + r * 0.8), fill=color)
    draw.rectangle((cx - r * 0.7, cy - r * 0.45, cx + r * 0.7, cy - r * 0.2), fill=color)
    draw.line((cx, cy - r * 0.45, cx, cy + r * 0.8), fill=(255, 255, 255), width=max(4, r // 8))


def _draw_lightbulb(draw: ImageDraw.ImageDraw, c: tuple[int, int], r: int, color: _RGB) -> None:
    cx, cy = c
    draw.ellipse((cx - r * 0.55, cy - r * 0.8, cx + r * 0.55, cy + r * 0.3), fill=color)
    draw.rectangle((cx - r * 0.25, cy + r * 0.2, cx + r * 0.25, cy + r * 0.55), fill=color)


def _draw_flask(draw: ImageDraw.ImageDraw, c: tuple[int, int], r: int, color: _RGB) -> None:
    cx, cy = c
    draw.polygon(
        [
            (cx - r * 0.2, cy - r * 0.9),
            (cx + r * 0.2, cy - r * 0.9),
            (cx + r * 0.2, cy - r * 0.1),
            (cx + r * 0.75, cy + r * 0.85),
            (cx - r * 0.75, cy + r * 0.85),
            (cx - r * 0.2, cy - r * 0.1),
        ],
        fill=color,
    )


def _draw_brain(draw: ImageDraw.ImageDraw, c: tuple[int, int], r: int, color: _RGB) -> None:
    cx, cy = c
    draw.ellipse((cx - r * 0.85, cy - r * 0.6, cx + r * 0.05, cy + r * 0.6), fill=color)
    draw.ellipse((cx - r * 0.05, cy - r * 0.6, cx + r * 0.85, cy + r * 0.6), fill=color)


def _draw_microscope(draw: ImageDraw.ImageDraw, c: tuple[int, int], r: int, color: _RGB) -> None:
    cx, cy = c
    width = max(8, r // 4)
    draw.line((cx + r * 0.3, cy - r * 0.9, cx - r * 0.4, cy + r * 0.5), fill=color, width=width)
    draw.ellipse((cx + r * 0.05, cy - r * 0.95, cx + r * 0.55, cy - r * 0.45), fill=color)
    draw.rectangle((cx - r * 0.75, cy + r * 0.55, cx + r * 0.35, cy + r * 0.8), fill=color)


def _draw_books(draw: ImageDraw.ImageDraw, c: tuple[int, int], r: int, color: _RGB) -> None:
    cx, cy = c
    widths = (0.9, 0.7, 0.8)
    y = cy - r * 0.5
    for i, w in enumerate(widths):
        top = y + i * r * 0.45
        draw.rectangle((cx - r * w, top, cx + r * w, top + r * 0.35), fill=color)


_ICON_DRAWERS = {
    EditorialCategory.NEWS: _draw_rocket,
    EditorialCategory.AI_TOOL: _draw_wrench,
    EditorialCategory.FREE_DEAL: _draw_gift,
    EditorialCategory.AI_LIFEHACK: _draw_lightbulb,
    EditorialCategory.PROMPT_WORKFLOW: _draw_flask,
    EditorialCategory.EXPLAINER: _draw_brain,
    EditorialCategory.RESEARCH: _draw_microscope,
    EditorialCategory.WEEKLY_DIGEST: _draw_books,
}


def _shorten(text: str, max_chars: int) -> str:
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[: max_chars - 1].rstrip() + "…"


def _draw_text_with_shadow(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> None:
    x, y = origin
    draw.text((x + 2, y + 2), text, font=font, fill=_TEXT_SHADOW)
    draw.text((x, y), text, font=font, fill=fill)


def _wrap_to_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _draw_wrapped_headline(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    origin: tuple[int, int],
    max_width: int,
    max_height: int,
) -> None:
    """Auto-fits ``text`` by trying progressively smaller font sizes until it wraps
    within ``max_height`` at ``_MAX_HEADLINE_LINES`` lines or fewer — never overflows
    or clips text off-canvas."""
    chosen_font = None
    chosen_lines: list[str] = []
    for size in _HEADLINE_FONT_SIZES:
        font = ImageFont.truetype(str(_BOLD_FONT_PATH), size)
        lines = _wrap_to_width(draw, text, font, max_width)
        line_height = font.getbbox("Ag")[3] + 12
        total_height = line_height * len(lines)
        if len(lines) <= _MAX_HEADLINE_LINES and total_height <= max_height:
            chosen_font, chosen_lines = font, lines
            break
    if chosen_font is None:
        # Even the smallest size doesn't fully fit — use it anyway, truncated to the
        # max line count, rather than fail the whole card over a very long headline.
        font = ImageFont.truetype(str(_BOLD_FONT_PATH), _HEADLINE_FONT_SIZES[-1])
        chosen_font = font
        chosen_lines = _wrap_to_width(draw, text, font, max_width)[:_MAX_HEADLINE_LINES]

    line_height = chosen_font.getbbox("Ag")[3] + 12
    x, y = origin
    for line in chosen_lines:
        _draw_text_with_shadow(draw, (x, y), line, chosen_font, _WHITE)
        y += line_height


__all__ = ["BrandedCardError", "generate_branded_card"]
