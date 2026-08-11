"""The channel call-to-action that closes a post.

    👉 Запросити друзів: @learn_ai_easy

It exists for one reason: when somebody forwards a useful post to a friend, the friend
sees where it came from and can subscribe. A forwarded post without it is a screenshot
of an orphan.

Kept here, in configuration, rather than in the prose a writing session produces. Two
things follow from that, and both matter:

**The handle is not writable by a writing session.** It comes from settings, is checked
against the configured value, and a post whose footer names a different channel is
refused. A model with the handle in its context is a model that can typo it.

**The footer is frozen onto the version at creation.** It is stored on the draft
version, hashed with everything else, and rendered from storage — not rebuilt from
configuration at send time. Otherwise editing a setting would silently alter a post a
human already approved.

The leading emoji varies, because four hundred consecutive posts ending in the identical
line is what a template looks like. The variation is deliberate and bounded.
"""

from __future__ import annotations

import random

#: The emoji that may lead the call-to-action. Small, warm, and none of them shouty.
FOOTER_EMOJI: tuple[str, ...] = ("👉", "📲", "💌", "✨", "🚀")

DEFAULT_FOOTER_TEXT = "Запросити друзів"


class FooterError(ValueError):
    """A footer that does not name the configured channel."""


def render_footer(handle: str, *, text: str = DEFAULT_FOOTER_TEXT, emoji: str | None = None) -> str:
    """Build the call-to-action line.

    ``emoji`` is chosen at random when not given, which is what keeps the feed from
    reading like a template. The choice is made once, at draft creation, and then stored
    — a footer that re-randomised at send time would not be the one that was approved.
    """
    chosen = emoji or random.choice(FOOTER_EMOJI)  # noqa: S311 - editorial variety, not security
    return f"{chosen} {text}: {handle}"


def check_footer(footer: str, handle: str) -> None:
    """Verify a footer names the configured channel.

    Raises:
        FooterError: the handle is missing or different. The forwarding value of the
            footer is the whole point of it, and a wrong handle sends readers nowhere —
            or worse, somewhere else.
    """
    if handle not in footer:
        raise FooterError(
            f"the footer does not name the configured channel {handle!r}: {footer!r}"
        )
