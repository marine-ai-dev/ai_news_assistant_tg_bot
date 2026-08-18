"""Turning an approved draft version into a Telegram sendMessage payload.

The rule this module exists to keep: **what Telegram displays must be exactly what the
human approved.** Everything here is transport encoding — deciding a parse mode,
escaping characters that would otherwise be read as markup, measuring length the way
Telegram measures it. Nothing here is editorial. No rewriting, no truncating, no
"improving" the text on the way out.

Verified against the official Bot API documentation (Bot API 10.2, 14 July 2026):

* ``text`` is 1-4096 characters.
* HTML parse mode requires every ``<``, ``>`` and ``&`` that is not part of a tag or
  entity to be written as ``&lt;``, ``&gt;`` and ``&amp;``.
* Link previews are configured with ``link_preview_options``; the old
  ``disable_web_page_preview`` boolean is the legacy spelling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape

from ai_news_editor.domain.models import DraftVersion
from ai_news_editor.writing.format import (
    ALLOWED_TAGS,
    render_version,
    source_url_of,
    unescape_markdown_v2,
)

#: Bot API sendMessage text limit, in UTF-16 code units. See :func:`telegram_length`.
MAX_MESSAGE_CHARS = 4096

_TAG = re.compile(r"</?([a-zA-Z0-9-]+)(\s[^>]*)?>")


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    """A ready-to-send payload plus the approved text it was built from."""

    #: Exactly the approved rendering — what the content hash covers.
    approved_text: str
    #: What goes in the ``text`` parameter. Differs from ``approved_text`` only by
    #: HTML escaping, and only when a parse mode is in use.
    payload_text: str
    parse_mode: str | None

    def to_payload(self, chat_id: str) -> dict[str, object]:
        """The JSON body for sendMessage."""
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": self.payload_text,
            # Deterministic policy: no link preview. Posts already carry a visible
            # attribution line, and a preview card would put a headline this channel
            # did not write, in a language it does not publish in, under every post.
            "link_preview_options": {"is_disabled": True},
        }
        if self.parse_mode is not None:
            payload["parse_mode"] = self.parse_mode
        return payload


class MessageTooLongError(ValueError):
    """The approved post exceeds what Telegram will accept in one message."""


def telegram_length(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units.

    Telegram's limits and entity offsets are expressed in UTF-16, so an emoji outside
    the basic plane counts as two. Python's ``len`` would under-count exactly the posts
    most likely to be near the limit — this channel's headlines start with an emoji.
    """
    return len(text.encode("utf-16-le")) // 2


def uses_markup(text: str) -> bool:
    """Whether the text carries any of the markup subset Phase 5 permits."""
    return any(tag.lower() in ALLOWED_TAGS for tag, _attrs in _TAG.findall(text))


def escape_html(text: str) -> str:
    """Escape ``&``, ``<`` and ``>`` outside the permitted tags.

    Telegram requires bare angle brackets and ampersands to be entities. Escaping them
    is invisible to a reader — "Q&A" still renders as "Q&A" — which is precisely why it
    is safe to do to approved content, and why the alternative (sending an unescaped
    ``&`` and having Telegram reject or mangle the post) is not.
    """
    out: list[str] = []
    position = 0
    for match in _TAG.finditer(text):
        if match.group(1).lower() not in ALLOWED_TAGS:
            continue
        out.append(_escape_run(text[position : match.start()]))
        out.append(match.group(0))
        position = match.end()
    out.append(_escape_run(text[position:]))
    return "".join(out)


def _escape_run(run: str) -> str:
    return run.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def approved_text(version: DraftVersion) -> str:
    """Re-render the post exactly as the reviewer saw it.

    The same renderer the review screen used, called on the stored version rather than
    on anything carried along from the review session. Callers verify the content hash
    separately; this function must not be the only check.
    """
    return render_version(version)


def build_message(version: DraftVersion) -> TelegramMessage:
    """Build the payload for an approved version.

    Three cases, in order:

    * a NEWS post (has a source — see ``writing.format.render_post``) is always
      MarkdownV2. Its text is already fully escaped by the renderer itself at the point
      each headline/paragraph/source-label fragment was inserted, so ``payload_text``
      is exactly ``approved_text`` — there is nothing left for this function to do.
    * editorial-original content that uses one of the permitted HTML tags (a writer's
      own deliberate ``<b>``/``<a>``) still uses HTML mode, escaping everything outside
      those tags.
    * anything else is sent as plain text. Plain text cannot be misparsed: there is no
      escaping to get wrong and no way for a stray character to change how a sentence
      reads.

    Raises:
        MessageTooLongError: the post will not fit in one Telegram message. Phase 7
            sends one message or none — splitting a post across two is an editorial
            decision, not a transport detail.
    """
    text = approved_text(version)

    if source_url_of(version):
        payload_text = text
        parse_mode: str | None = "MarkdownV2"
    elif uses_markup(text):
        payload_text = escape_html(text)
        parse_mode = "HTML"
    else:
        payload_text = text
        parse_mode = None

    length = telegram_length(payload_text)
    if length > MAX_MESSAGE_CHARS:
        raise MessageTooLongError(
            f"the approved post is {length} characters as Telegram counts them, over the "
            f"{MAX_MESSAGE_CHARS} limit for one message. It is not truncated or split "
            "automatically; shorten it in review and approve the new version."
        )

    return TelegramMessage(
        approved_text=text, payload_text=payload_text, parse_mode=parse_mode
    )


def unescape_html(text: str) -> str:
    """The exact inverse of :func:`escape_html`.

    Exists so a test can assert the round trip rather than trust it: whatever encoding
    happens on the way out has to give the approved text back, character for character.
    """
    out: list[str] = []
    position = 0
    for match in _TAG.finditer(text):
        if match.group(1).lower() not in ALLOWED_TAGS:
            continue
        out.append(unescape(text[position : match.start()]))
        out.append(match.group(0))
        position = match.end()
    out.append(unescape(text[position:]))
    return "".join(out)


def displayed_text(message: TelegramMessage) -> str:
    """The approved text recovered from the payload that will be sent."""
    if message.parse_mode is None:
        return message.payload_text
    if message.parse_mode == "MarkdownV2":
        return unescape_markdown_v2(message.payload_text)
    return unescape_html(message.payload_text)
