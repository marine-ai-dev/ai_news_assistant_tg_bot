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

from ai_news_editor.domain.enums import Category, PostFormat
from ai_news_editor.domain.models import DraftVersion

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

#: One emoji per editorial category, for the bold headline of a NEWS post. Deterministic
#: and fixed rather than a model call: the channel's whole visual identity should not
#: depend on an extra Gemini round trip just to pick a symbol. Falls back to
#: ``_DEFAULT_HEADLINE_EMOJI`` for a category with no entry (there is none today, but a
#: future ``Category`` addition should degrade gracefully rather than raise).
_HEADLINE_EMOJI: dict[Category, str] = {
    Category.PRODUCT_UPDATE: "🚀",
    Category.USEFUL_TOOL: "🛠",
    Category.WOW: "🤯",
    Category.AI_FAIL: "⚠️",
    Category.DEEPFAKE_WATCH: "🕵️",
    Category.SCAM_MISINFO: "🔐",
    Category.CREATIVE_AI: "🎨",
    Category.AI_FOR_WORK: "💼",
    Category.AI_FOR_LEARNING: "🧠",
    Category.EVERYDAY_AI: "📱",
    Category.TRENDING: "🔥",
    Category.EXPLAINED_SIMPLY: "💡",
    Category.SCIENCE_LITE: "🔬",
    Category.AI_DRAMA: "🎭",
}
_DEFAULT_HEADLINE_EMOJI = "🧩"

#: A simple, stable rotation for body paragraphs — not per-paragraph NLP classification,
#: just enough visual rhythm that consecutive paragraphs do not look identical. Cycles
#: if a post ever has more paragraphs than emoji.
_PARAGRAPH_EMOJI: tuple[str, ...] = ("✨", "🛠", "🔍", "📌", "🌍", "💡")

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


class UnsafeLinkError(ValueError):
    """A URL uses a scheme this application will not put in a post."""


def has_any_markup(text: str) -> bool:
    """Whether ``text`` contains anything that looks like an HTML tag at all.

    Stricter than :func:`disallowed_tags`, which only flags tags outside the permitted
    subset — a human writer may deliberately use ``<b>`` or ``<a href="">`` for inline
    emphasis, and that stays allowed. Automation has no such allowance: a NEWS post's
    bold headline, its paragraph emoji and its hidden source link are entirely this
    renderer's decision, never the model's, so generated headline/body text must not
    contain markup of any kind, permitted or not.
    """
    return bool(_TAG_PATTERN.search(text))


def _split_paragraphs(body: str) -> list[str]:
    """Existing blank-line boundaries only — never a mid-sentence, character-count split."""
    parts = _PARAGRAPH_SPLIT.split(body.strip())
    paragraphs = [part.strip() for part in parts if part.strip()]
    return paragraphs or [body.strip()]


#: Every character Telegram's MarkdownV2 parser treats as syntax and therefore requires
#: escaped with a preceding backslash when it appears as literal text. See "Formatting
#: options" in the Bot API docs — this is the exact set listed there, not a guess.
_MARKDOWN_V2_SPECIAL = frozenset("_*[]()~`>#+-=|{}.!\\")


def escape_markdown_v2(text: str) -> str:
    """Make raw text safe to place in a MarkdownV2 message as plain text.

    Every one of Telegram's MarkdownV2 special characters becomes a literal, visible
    character instead of syntax — this is what stands between a headline that happens to
    contain ``!`` or ``(`` and a message Telegram refuses to parse (or worse, parses
    into something nobody wrote). Applied to every writer/Gemini-supplied string before
    it is placed inside this renderer's own ``*bold*`` / ``[text](url)`` — never to the
    ``*``, ``[``, ``]``, ``(``, ``)`` this renderer inserts itself, which are meant as
    syntax.
    """
    return "".join(f"\\{ch}" if ch in _MARKDOWN_V2_SPECIAL else ch for ch in text)


_MARKDOWN_V2_ESCAPE = re.compile(r"\\([_*\[\]()~`>#+\-=|{}.!\\])")


def unescape_markdown_v2(text: str) -> str:
    """The exact inverse of :func:`escape_markdown_v2`.

    Removes the backslash from an escaped special character, recovering the raw
    content that was there before escaping — the same role ``unescape_html`` plays for
    the HTML path. This renderer's own unescaped ``*`` / ``[`` / ``]`` / ``(`` / ``)``
    syntax is untouched (never preceded by a backslash in text this module produces),
    exactly as ``unescape_html`` leaves its own permitted tags alone.
    """
    return _MARKDOWN_V2_ESCAPE.sub(r"\1", text)


def _escape_markdown_v2_url(url: str) -> str:
    """The narrower escaping MarkdownV2 requires inside a link's ``(...)`` target.

    Per the Bot API docs, only ')' and '\\' need escaping there — the full special-set
    escaping :func:`escape_markdown_v2` does is for *display* text, not a link target.
    """
    return url.replace("\\", "\\\\").replace(")", "\\)")


def _headline_line(headline: str, category: Category | None) -> str:
    emoji = _DEFAULT_HEADLINE_EMOJI
    if category is not None:
        emoji = _HEADLINE_EMOJI.get(category, _DEFAULT_HEADLINE_EMOJI)
    return f"*{emoji} {escape_markdown_v2(headline.strip())}*"


def _body_lines(body: str) -> list[str]:
    paragraphs = _split_paragraphs(body)
    return [
        f"{_PARAGRAPH_EMOJI[index % len(_PARAGRAPH_EMOJI)]} {escape_markdown_v2(paragraph)}"
        for index, paragraph in enumerate(paragraphs)
    ]


def _source_hyperlink(source_label: str, source_url: str) -> str:
    """The attribution line: a hidden hyperlink, not a bare URL on its own line."""
    url = validate_url(source_url)
    label = source_label.strip() or "Джерело"
    link_text = escape_markdown_v2(f"Джерело: {label}")
    return f"🔗 [{link_text}]({_escape_markdown_v2_url(url)})"


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


def render_post(
    *,
    headline: str,
    body: str,
    source_label: str = "",
    source_url: str = "",
    category: Category | None = None,
) -> str:
    """Assemble the text that would be sent.

    Deterministic and computed by Python, not supplied by the writer, so the stored
    content hash covers exactly the post a reviewer approves.

    Presence of ``source_url`` is what distinguishes a NEWS post from editorial-original
    content (see ``Draft._exactly_one_origin`` — NEWS is the only content type that ever
    has one) and is the whole switch between the two styles below:

    * with a source: the channel's NEWS style — a bold, emoji-led headline; each
      paragraph on its own emoji-led line; a hidden hyperlink for attribution instead of
      a bare URL. Every tag here is inserted by this function, never by the writer —
      see ``has_any_markup``, which is what keeps automation-generated headline/body
      text out of this decision entirely.
    * without one: unchanged plain text. A prompt or an explainer was written here, and
      giving it a bold headline it never had, or appending "🔗 Джерело:" to it, would
      either invent a visual identity nobody approved or name a source that does not
      exist.
    """
    headline = headline.strip()
    body = body.strip()
    if not source_url:
        return "\n\n".join([headline, body])

    parts = [
        _headline_line(headline, category),
        *_body_lines(body),
        _source_hyperlink(source_label or "Джерело", source_url),
    ]
    return "\n\n".join(parts)


def source_label_of(source_attribution: str) -> str:
    """Recover the human-readable source name from a rendered attribution line."""
    first_line = source_attribution.split("\n")[0]
    return first_line.split(": ", 1)[-1].strip() or "Джерело"


def source_url_of(version: DraftVersion) -> str:
    """The version's source link.

    Falls back to the URL inside the rendered attribution line, for drafts written
    before ``source_url`` had its own column. The link has always been present in the
    attribution, so nothing ever has to be invented.
    """
    if version.source_url:
        return version.source_url
    for token in version.source_attribution.split():
        if token.startswith(("http://", "https://")):
            return token
    return ""


def render_version(version: DraftVersion) -> str:
    """The post text for a stored version.

    Editorial-original content carries no source URL and therefore no source line.

    One function, deliberately. The review screen shows this, the content hash covers
    the fields it is built from, and the publisher sends it — if each derived the text
    its own way, a reviewer could approve one string and a channel could receive
    another. That is the entire failure mode this project exists to prevent, and the
    cheapest defence is to have only one renderer.

    The channel call-to-action, when the version has one, closes the post. It is read
    from the version rather than rebuilt from configuration, so changing a setting
    cannot alter a post a human already approved. It is also kept visually separate
    from the source line: one says where the story came from, the other says where the
    reader is, and running them together would make the channel look like the source.
    """
    text = render_post(
        headline=version.title,
        body=version.body,
        source_label=source_label_of(version.source_attribution),
        source_url=source_url_of(version),
        category=version.category,
    )
    if version.footer_text:
        text = f"{text}\n\n{version.footer_text}"
    return text


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
