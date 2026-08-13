"""Encoding and decoding inline-button callback data.

Telegram sends ``callback_data`` back to the bot verbatim when a button is tapped, which
makes it look like a convenient place to carry state. It is not. The string travelled to
a client and back, and the only thing the bot knows about it is that it is a string it
once produced — or one that looks like it.

So callbacks carry the smallest possible thing: an action, a draft id and the version
number that was on screen. Everything else is re-read from the database, and the version
number exists purely so a tap on a stale card can be *detected* rather than obeyed.

Never in here: post text, source URLs, content hashes, owner ids, tokens.

``callback_data`` is limited to a small number of bytes by the Bot API. The exact figure
could not be re-read from the documentation in this session — the page truncates before
the InlineKeyboardButton table — so the budget below is set conservatively at the
long-standing documented value and enforced by a test on every string this module emits.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

#: Bot API limit for callback_data. See the module docstring on how this was chosen.
MAX_CALLBACK_BYTES = 64

_SEPARATOR = ":"


class Action(StrEnum):
    """What a button asks for. Short values: every byte competes with the draft id."""

    APPROVE = "a"
    APPROVE_CONFIRM = "ac"
    REJECT = "r"
    REJECT_CONFIRM = "rc"
    REWRITE = "w"
    REWRITE_CONFIRM = "wc"
    EDIT = "e"
    SKIP = "s"
    NEXT = "n"
    HISTORY = "h"
    CANCEL = "x"
    REFRESH = "f"

    # Phase 9. Scheduling is deliberately several taps: the preset chooses a time, and
    # a separate confirmation creates the queue item. One accidental tap schedules
    # nothing.
    SCHEDULE = "sc"
    SCHEDULE_MORNING = "sm"
    SCHEDULE_AFTERNOON = "sa"
    SCHEDULE_EVENING = "se"
    SCHEDULE_CUSTOM = "su"
    SCHEDULE_CONFIRM = "sk"
    QUEUE_SHOW = "qs"
    QUEUE_CANCEL = "qc"
    QUEUE_CANCEL_CONFIRM = "qk"
    QUEUE_RESCHEDULE = "qr"


@dataclass(frozen=True, slots=True)
class Callback:
    """A decoded button tap. Not yet trusted — only parsed."""

    action: Action
    #: First 8 characters of the draft id. Enough to find one draft among a queue a
    #: human is realistically reviewing, and the full id is looked up in the database.
    draft_prefix: str
    #: The version number the card was rendered from, so a stale tap is visible.
    version_no: int


class CallbackError(ValueError):
    """Callback data that this application did not produce, or cannot parse."""


def encode(action: Action, draft_id: UUID, version_no: int) -> str:
    """Build the callback_data for one button."""
    data = f"{action.value}{_SEPARATOR}{str(draft_id)[:8]}{_SEPARATOR}{version_no}"
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:  # pragma: no cover - defensive
        raise CallbackError(f"callback data too long: {data!r}")
    return data


def decode(data: str | None) -> Callback:
    """Parse callback data.

    Raises:
        CallbackError: anything unparseable. The caller answers the tap politely and
            does nothing else — a malformed callback is not an error worth a traceback,
            it is a button from an older version of the bot or somebody poking at it.
    """
    if not data:
        raise CallbackError("empty callback data")
    parts = data.split(_SEPARATOR)
    if len(parts) != 3:
        raise CallbackError(f"unrecognised callback data: {data!r}")

    raw_action, prefix, raw_version = parts
    try:
        action = Action(raw_action)
    except ValueError as exc:
        raise CallbackError(f"unknown action {raw_action!r}") from exc

    if not prefix or len(prefix) > 8 or not all(c in "0123456789abcdef-" for c in prefix):
        raise CallbackError(f"unusable draft reference {prefix!r}")

    try:
        version_no = int(raw_version)
    except ValueError as exc:
        raise CallbackError(f"unusable version {raw_version!r}") from exc
    if version_no < 1:
        raise CallbackError(f"unusable version {raw_version!r}")

    return Callback(action=action, draft_prefix=prefix, version_no=version_no)
