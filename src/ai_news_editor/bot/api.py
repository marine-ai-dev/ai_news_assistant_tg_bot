"""The incoming half of the Bot API, as the review bot needs it.

Four methods, verified against the official documentation (Bot API 10.2, 14 July 2026):

* ``getUpdates`` — long polling. ``offset`` must be one greater than the highest
  ``update_id`` already received; passing it is what confirms those updates and stops
  Telegram redelivering them.
* ``sendMessage`` — to the owner's private chat, with an inline keyboard.
* ``editMessageText`` — replace a card in place rather than filling the chat with copies.
* ``answerCallbackQuery`` — required after *every* button tap: until it is called the
  Telegram client shows a spinner on the button.

This talks to the same :class:`TelegramClient` the publisher uses, through its
``request`` path — the one without publication retry semantics. It cannot publish: it
has no authorization, and nothing here builds a channel payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_news_editor.domain.errors import TelegramError
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.publishing.telegram import TelegramClient

logger = get_logger(__name__)

#: Seconds Telegram holds a getUpdates request open when there is nothing to send.
#: Long enough that the loop is not a busy-wait, short enough to shut down promptly.
LONG_POLL_SECONDS = 25

#: How much longer than the poll the client waits for the response. Telegram answers at
#: the end of its own window, so the HTTP read budget must be larger — otherwise every
#: quiet poll ends in a client-side timeout and the bot never receives anything.
POLL_READ_MARGIN_SECONDS = 15

#: Only these update types are requested. Anything else Telegram might add later is not
#: silently delivered to a bot that has no idea what to do with it.
ALLOWED_UPDATES = ("message", "callback_query")


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A text message sent to the bot."""

    update_id: int
    message_id: int
    chat_id: int
    user_id: int
    text: str


@dataclass(frozen=True, slots=True)
class IncomingCallback:
    """A button tap."""

    update_id: int
    callback_id: str
    chat_id: int
    message_id: int
    user_id: int
    data: str | None


class BotApi:
    """Telegram calls the review bot needs, and nothing else."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    def get_updates(
        self, offset: int | None, *, timeout: int = LONG_POLL_SECONDS, limit: int = 50
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "limit": limit,
            "allowed_updates": list(ALLOWED_UPDATES),
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._client.request(
            "getUpdates", payload, timeout=timeout + POLL_READ_MARGIN_SECONDS
        )
        updates = result.get("result", [])
        return [u for u in updates if isinstance(u, dict)]

    def send_message(
        self, chat_id: int, text: str, *, keyboard: dict[str, Any] | None = None
    ) -> int | None:
        """Send to the owner's private chat. Returns the message id, or None on failure.

        Failures are logged and swallowed: a UI message that did not arrive must never
        turn into an exception that unwinds past a decision the human already made and
        the database already committed. See the module docstring of the dispatcher.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "link_preview_options": {"is_disabled": True},
        }
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        try:
            result = self._client.request("sendMessage", payload)
        except TelegramError as exc:
            logger.warning("review bot could not send a message", extra={"error": str(exc)})
            return None
        return int(result["message_id"]) if "message_id" in result else None

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        keyboard: dict[str, Any] | None = None,
    ) -> bool:
        """Replace a card in place. Returns whether it worked.

        Telegram refuses an edit that would change nothing ("message is not modified").
        That is a normal outcome here — a double tap on the same button produces it —
        so it is not treated as an error.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
            "link_preview_options": {"is_disabled": True},
        }
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        try:
            self._client.request("editMessageText", payload)
        except TelegramError as exc:
            logger.warning("review bot could not edit a message", extra={"error": str(exc)})
            return False
        return True

    def answer_callback(
        self, callback_id: str, text: str | None = None, *, alert: bool = False
    ) -> None:
        """Acknowledge a tap. Always called, even when the answer is nothing."""
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            # Telegram truncates long toasts; keep them short enough to read anyway.
            payload["text"] = text[:190]
        if alert:
            payload["show_alert"] = True
        try:
            self._client.request("answerCallbackQuery", payload)
        except TelegramError as exc:
            logger.warning("review bot could not answer a callback", extra={"error": str(exc)})


def parse_update(update: dict[str, Any]) -> IncomingMessage | IncomingCallback | None:
    """Turn one raw update into something typed, or None if it is not ours to handle."""
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return None

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        sender = callback.get("from") or {}
        if not isinstance(chat.get("id"), int) or not isinstance(sender.get("id"), int):
            return None
        return IncomingCallback(
            update_id=update_id,
            callback_id=str(callback.get("id", "")),
            chat_id=int(chat["id"]),
            message_id=int(message.get("message_id", 0)),
            user_id=int(sender["id"]),
            data=callback.get("data"),
        )

    message = update.get("message")
    if isinstance(message, dict) and isinstance(message.get("text"), str):
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if not isinstance(chat.get("id"), int) or not isinstance(sender.get("id"), int):
            return None
        return IncomingMessage(
            update_id=update_id,
            message_id=int(message.get("message_id", 0)),
            chat_id=int(chat["id"]),
            user_id=int(sender["id"]),
            text=message["text"],
        )

    return None
