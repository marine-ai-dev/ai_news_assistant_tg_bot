"""Deterministic media-caption-vs-followup split — Step 5 (AI News Agent v2), sections
19-20.

Telegram media captions have a smaller limit (``MAX_CAPTION_CHARS``, 1024 UTF-16 code
units) than a normal message. When the full rendered post fits, it travels as the
caption on the photo/video itself. When it does not, nothing is truncated: the media
goes out with a short, deterministically-derived caption, and the full post follows as
its own message — the same "media first, full text always follows in full" discipline
``publishing/plan.py`` already uses for images.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_news_editor.publishing.message import telegram_length
from ai_news_editor.publishing.plan import MAX_CAPTION_CHARS
from ai_news_editor.rendering.content import EditorialContent
from ai_news_editor.rendering.render import (
    RenderedPost,
    render_editorial_post,
    render_short_summary,
)


@dataclass(frozen=True, slots=True)
class CaptionPlan:
    """What to send with the media, and whether a full-text follow-up is needed."""

    #: "single": the full post fits the caption. "split": a short caption plus a
    #: separate follow-up message carries the full, unabridged post.
    mode: str
    caption: str
    followup: str | None
    warnings: tuple[str, ...] = ()

    @property
    def needs_followup(self) -> bool:
        return self.mode == "split"


def plan_caption(content: EditorialContent) -> CaptionPlan:
    """Decide how ``content`` travels alongside media, without ever cutting a fact."""
    full = render_editorial_post(content)
    if telegram_length(full.full_text) <= MAX_CAPTION_CHARS:
        return CaptionPlan(
            mode="single", caption=full.full_text, followup=None, warnings=full.warnings
        )

    short = render_short_summary(content)
    return CaptionPlan(mode="split", caption=short, followup=full.full_text, warnings=full.warnings)


def fits_caption(rendered: RenderedPost) -> bool:
    return telegram_length(rendered.full_text) <= MAX_CAPTION_CHARS


__all__ = ["CaptionPlan", "fits_caption", "plan_caption"]
