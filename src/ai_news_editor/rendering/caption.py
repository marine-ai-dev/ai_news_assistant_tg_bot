"""Deterministic single-message caption construction — Step 5 (AI News Agent v2),
sections 19-20; redesigned in Step 6C for the single-post invariant.

Telegram media captions have a smaller limit (``MAX_CAPTION_CHARS``, 1024 UTF-16 code
units) than a normal message. Earlier this module split an over-long post into a short
caption *plus a separate follow-up text message* — real-channel visual review showed
that as two published messages for one story, which is exactly the duplicate-post
behaviour the channel must never produce. There is no follow-up any more: every post
is exactly one Telegram message, so a caption that doesn't fit is shortened until it
does, never split across two sends.

Three-step escalation, never truncating a fact silently past what the reader can tell
happened:
1. The full rendered post, if it already fits.
2. ``render_short_summary`` (headline + one highlight + source) — a real, deterministic
   summary derived from the same content record, not an independently-written one.
3. A hard character truncation of that summary, only if even the short summary
   somehow doesn't fit (should not happen given the body-length caps in
   ``rendering.content``/``rendering.style``, but a single-message guarantee has to
   hold even in that edge case, not just usually).
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_news_editor.publishing.message import telegram_length
from ai_news_editor.publishing.plan import MAX_CAPTION_CHARS
from ai_news_editor.rendering.content import EditorialContent
from ai_news_editor.rendering.render import render_editorial_post, render_short_summary

#: Purely descriptive buckets for reporting/observability (e.g. the soak report) — not
#: a second length-control mechanism. What actually controls length is the generation
#: contract's own caps (rendering.style.TARGET_BODY_CHARS_*); this just names where the
#: resulting caption landed.
SHORT_BUCKET_MAX_CHARS = 300
MEDIUM_BUCKET_MAX_CHARS = 650


@dataclass(frozen=True, slots=True)
class CaptionPlan:
    """The one and only text payload a post sends — always a single message's worth."""

    caption: str
    #: True if the full post did not fit and this caption is the shortened summary
    #: (or, in the rare last-resort case, a hard-truncated version of it) instead.
    shortened: bool
    #: "short" / "medium" / "long" — see the module-level bucket constants.
    length_bucket: str
    warnings: tuple[str, ...] = ()


def length_bucket(chars: int) -> str:
    if chars <= SHORT_BUCKET_MAX_CHARS:
        return "short"
    if chars <= MEDIUM_BUCKET_MAX_CHARS:
        return "medium"
    return "long"


def plan_caption(content: EditorialContent) -> CaptionPlan:
    """The single caption ``content`` travels with — guaranteed to fit a Telegram
    media caption, so the post is always exactly one message."""
    full = render_editorial_post(content)
    full_chars = telegram_length(full.full_text)
    if full_chars <= MAX_CAPTION_CHARS:
        return CaptionPlan(
            caption=full.full_text,
            shortened=False,
            length_bucket=length_bucket(full_chars),
            warnings=full.warnings,
        )

    short = render_short_summary(content)
    short_chars = telegram_length(short)
    if short_chars <= MAX_CAPTION_CHARS:
        return CaptionPlan(
            caption=short,
            shortened=True,
            length_bucket=length_bucket(short_chars),
            warnings=full.warnings,
        )

    # Last resort: the short summary itself is somehow still too long — truncate the
    # *highlight text specifically* (never a blind end-of-string cut, which could clip
    # off the source line entirely) so headline and source both always survive. A
    # single, slightly-clipped post beats two "complete" ones, per the single-post
    # invariant.
    truncated = _truncate_highlight_to_fit(content)
    return CaptionPlan(
        caption=truncated,
        shortened=True,
        length_bucket="short",
        warnings=(*full.warnings, "caption hard-truncated to fit a single message"),
    )


def _truncate_highlight_to_fit(content: EditorialContent) -> str:
    """Binary search the largest highlight length whose rendered caption still fits —
    guarantees headline and source survive, only the highlight shrinks."""
    lo, hi = 0, 4000
    best = render_short_summary(content, max_highlight_chars=0)
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = render_short_summary(content, max_highlight_chars=mid)
        if telegram_length(candidate) <= MAX_CAPTION_CHARS:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def fits_caption(text: str) -> bool:
    return telegram_length(text) <= MAX_CAPTION_CHARS


__all__ = [
    "MEDIUM_BUCKET_MAX_CHARS",
    "SHORT_BUCKET_MAX_CHARS",
    "CaptionPlan",
    "fits_caption",
    "length_bucket",
    "plan_caption",
]
