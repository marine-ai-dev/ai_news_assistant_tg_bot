"""Post assembly, length policy and link safety.

Python owns the rendered post. A writing session supplies a headline, a body and the
source it used; this module assembles the text that would actually be sent. Keeping
assembly here means the final form is deterministic and the content hash covers exactly
what a reviewer will read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from ai_news_editor.domain.enums import PostFormat

#: Editorial targets in characters of rendered post text, not hard limits. A post
#: outside its range is reported, never rewritten or cropped.
FORMAT_TARGETS: dict[PostFormat, tuple[int, int]] = {
    PostFormat.QUICK: (400, 800),
    PostFormat.STANDARD: (800, 1600),
    PostFormat.DEEP_DIVE: (1600, 3000),
}

#: Refused outright. Telegram's own limit is 4096 characters; stopping at 3500 leaves
#: room for the attribution line and any future additions without a post becoming
#: unsendable. Exceeding it is an error, never a silent truncation.
HARD_MAX_CHARS = 3500

#: A post shorter than this is not a post.
HARD_MIN_CHARS = 120

MAX_HEADLINE_CHARS = 160

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

#: The only markup a draft may carry. Telegram HTML rather than MarkdownV2, which needs
#: 18 characters escaped and is a reliable source of publishing bugs. Kept to the
#: minimum that editorial writing actually needs.
ALLOWED_TAGS = frozenset({"b", "i", "u", "s", "a", "code", "pre"})
_TAG_PATTERN = re.compile(r"</?([a-zA-Z0-9]+)[^>]*>")

SOURCE_PREFIX = "🔗 Джерело"


class UnsafeLinkError(ValueError):
    """A URL uses a scheme this application will not put in a post."""


@dataclass(frozen=True, slots=True)
class LengthCheck:
    """How a rendered post compares to its format's target."""

    chars: int
    post_format: PostFormat
    within_target: bool
    note: str | None = None


def validate_url(url: str) -> str:
    """Return the URL if it is safe to place in a post.

    Raises:
        UnsafeLinkError: empty, unparseable, non-HTTP scheme, or missing a host.
    """
    if not url or not url.strip():
        raise UnsafeLinkError("source URL is empty")
    candidate = url.strip()
    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise UnsafeLinkError(f"unparseable URL {candidate!r}: {exc}") from exc

    if parts.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        raise UnsafeLinkError(
            f"only http and https links may appear in a post, got {parts.scheme!r}"
        )
    if not parts.netloc:
        raise UnsafeLinkError(f"URL has no host: {candidate!r}")
    return candidate


def disallowed_tags(text: str) -> set[str]:
    """Markup tags in ``text`` outside the permitted subset."""
    return {tag.lower() for tag in _TAG_PATTERN.findall(text) if tag.lower() not in ALLOWED_TAGS}


def source_line(source_label: str, source_url: str) -> str:
    """The attribution line every post carries.

    Never optional: a post without a traceable source is not something this channel
    publishes, and the line is part of the hashed content so it cannot be dropped after
    review.
    """
    return f"{SOURCE_PREFIX}: {source_label.strip()}\n{validate_url(source_url)}"


def render_post(*, headline: str, body: str, source_label: str, source_url: str) -> str:
    """Assemble the text that would be sent.

    Deterministic and computed by Python, not supplied by the writer, so the stored
    content hash covers exactly the post a reviewer approves.
    """
    return "\n\n".join([headline.strip(), body.strip(), source_line(source_label, source_url)])


def check_length(text: str, post_format: PostFormat) -> LengthCheck:
    """Compare a rendered post against its format target.

    Being outside the target is a note, not a failure — editorial targets guide writing,
    they do not police it. Only the hard limits reject.
    """
    chars = len(text)
    low, high = FORMAT_TARGETS[post_format]
    if chars < low:
        return LengthCheck(
            chars,
            post_format,
            within_target=False,
            note=f"{chars} characters is short for {post_format.value} (target {low}-{high})",
        )
    if chars > high:
        return LengthCheck(
            chars,
            post_format,
            within_target=False,
            note=f"{chars} characters is long for {post_format.value} (target {low}-{high})",
        )
    return LengthCheck(chars, post_format, within_target=True)


def hard_limit_problem(text: str) -> str | None:
    """Return why a post is unsendable, or ``None`` if it is within hard limits."""
    chars = len(text)
    if chars < HARD_MIN_CHARS:
        return f"rendered post is {chars} characters, below the minimum of {HARD_MIN_CHARS}"
    if chars > HARD_MAX_CHARS:
        return (
            f"rendered post is {chars} characters, over the {HARD_MAX_CHARS} limit. "
            "Shorten it deliberately — nothing is cropped automatically."
        )
    return None
