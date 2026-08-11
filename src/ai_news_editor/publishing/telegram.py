"""The Telegram Bot API client and the publisher built on it.

Verified against the official documentation (Bot API 10.2, 14 July 2026). Four methods
are used, all of them outbound:

* ``getMe`` — confirm the token is valid and learn the bot's identity.
* ``getChat`` — confirm the destination exists and is the kind of chat we can post to.
* ``getChatMember`` — confirm the bot is an administrator with ``can_post_messages``.
* ``sendMessage`` — the only method that changes anything on a channel.

Phase 8 adds the incoming side (``getUpdates``, ``answerCallbackQuery``,
``editMessageText``) through :meth:`TelegramClient.request`. The review bot builds on
that; it is a separate module with separate responsibilities, and it cannot publish.

Deliberately no framework. python-telegram-bot, aiogram and Telethon all bring an
update loop, a dispatcher and a handler abstraction; this application receives nothing
and dispatches nothing. Four POSTs over ``httpx``, which the project already depends on,
is the whole requirement.

**Why this does not reuse** :class:`sources.http.HttpClient`. That client is built for
reading feeds, where a retry is free. Here a retry can produce a second real post in
front of an audience. The retry policy is therefore the opposite: narrow, small, and
absent entirely for anything that might already have been delivered.

This module knows nothing about drafts, approvals or story selection. It is handed
content that something else already authorized, and it sends it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from ai_news_editor import __version__
from ai_news_editor.domain.authorization import PublishAuthorization
from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.errors import (
    PublicationOutcomeUncertainError,
    TelegramApiError,
    TelegramAuthenticationError,
    TelegramContentError,
    TelegramDestinationError,
    TelegramError,
    TelegramPermissionError,
    TelegramRateLimitError,
)
from ai_news_editor.domain.models import DraftVersion
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.observability.redaction import redact
from ai_news_editor.publishing.base import PublicationReceipt
from ai_news_editor.publishing.message import build_message

logger = get_logger(__name__)

API_ROOT = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 20.0
USER_AGENT = f"AiNewsEditorBot/{__version__}"

#: Chat types a channel post can go to. A private chat with the bot is allowed because
#: it is the sanest destination for a first smoke test.
POSTABLE_CHAT_TYPES = frozenset({"channel", "supergroup", "group", "private"})

#: Retried at most this many times, and only for a failure that is definitely safe to
#: repeat — one where the request provably never reached Telegram, or where Telegram
#: itself asked us to wait. Small on purpose.
MAX_SEND_ATTEMPTS = 2

#: Longer than this and waiting is the wrong answer; a human should decide.
MAX_RETRY_AFTER_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class BotIdentity:
    """Who the token belongs to."""

    id: int
    username: str | None
    first_name: str


@dataclass(frozen=True, slots=True)
class ChatInfo:
    """The destination, as Telegram describes it."""

    id: int
    type: str
    title: str | None
    username: str | None

    @property
    def postable(self) -> bool:
        return self.type in POSTABLE_CHAT_TYPES


@dataclass(frozen=True, slots=True)
class PostingRights:
    """What the bot is allowed to do in the destination.

    ``can_post`` is ``None`` when the API did not say — reported as unknown rather than
    guessed. An optimistic guess here would turn a diagnostic into a lie.
    """

    status: str
    can_post: bool | None
    detail: str


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Telegram's own record that a message exists."""

    message_id: int
    chat_id: str


class TelegramClient:
    """A thin, synchronous Bot API client.

    The token is held here and nowhere else. It goes into the URL path because the Bot
    API requires it there, and every error raised by this class is scrubbed through
    :func:`redact` before it can reach a log or a terminal.
    """

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        api_root: str = API_ROOT,
    ) -> None:
        if not token or not token.strip():
            raise TelegramAuthenticationError(
                "no Telegram bot token configured. Set AI_NEWS_TELEGRAM_BOT_TOKEN."
            )
        self._token = token.strip()
        self._api_root = api_root.rstrip("/")
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- API methods ---------------------------------------------------------

    def get_me(self) -> BotIdentity:
        """Confirm the token works and learn who it belongs to."""
        result = self._call("getMe", {})
        return BotIdentity(
            id=int(result["id"]),
            username=result.get("username"),
            first_name=str(result.get("first_name", "")),
        )

    def get_chat(self, chat_id: str) -> ChatInfo:
        """Resolve the destination.

        Raises:
            TelegramDestinationError: no such chat, or the bot cannot see it.
        """
        result = self._call("getChat", {"chat_id": chat_id})
        return ChatInfo(
            id=int(result["id"]),
            type=str(result.get("type", "")),
            title=result.get("title"),
            username=result.get("username"),
        )

    def get_posting_rights(self, chat_id: str, bot_id: int) -> PostingRights:
        """Ask whether the bot may post, and say so honestly when the API does not tell.

        For a channel, ``ChatMemberAdministrator.can_post_messages`` is the field that
        matters. For a private chat there is no such concept, and pretending to have
        verified something the API never reported would be worse than saying "unknown".
        """
        try:
            result = self._call("getChatMember", {"chat_id": chat_id, "user_id": bot_id})
        except TelegramError as exc:
            # Any Telegram-side refusal here is an unanswered question, not a verdict.
            # getChatMember is not available for every chat type, and a diagnostic must
            # not turn "I could not check" into either a pass or a failure.
            return PostingRights(
                status="unknown",
                can_post=None,
                detail=f"getChatMember could not answer: {exc}",
            )

        status = str(result.get("status", "unknown"))
        if status == "creator":
            return PostingRights(status, True, "the bot owns the chat")
        if status == "administrator":
            can_post = result.get("can_post_messages")
            if can_post is None:
                return PostingRights(
                    status,
                    None,
                    "administrator, but the API did not report can_post_messages "
                    "(normal outside channels)",
                )
            return PostingRights(
                status,
                bool(can_post),
                "administrator with posting rights"
                if can_post
                else "administrator, but 'Post Messages' is off",
            )
        if status in {"left", "kicked"}:
            return PostingRights(status, False, f"the bot is {status} — add it to the chat")
        return PostingRights(status, None, f"member status is {status!r}")

    def send_message(self, payload: dict[str, Any]) -> SentMessage:
        """Send one message.

        Raises:
            PublicationOutcomeUncertainError: the request may have been delivered but
                the response was lost. Never retried — see the module docstring.
        """
        result = self._call("sendMessage", payload, sending=True)
        chat = result.get("chat") or {}
        return SentMessage(message_id=int(result["message_id"]), chat_id=str(chat.get("id", "")))

    def request(
        self, method: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Call any Bot API method that is not a publication.

        The review bot's UI calls come through here: getUpdates, answerCallbackQuery,
        editMessageText, and sendMessage to the *owner's private chat*. They get the
        ordinary error handling — no retry, no uncertain-outcome machinery — because a
        duplicated or missing UI message is an inconvenience, while a duplicated channel
        post is not. That distinction is the reason this is a separate entry point from
        :meth:`send_message`.

        ``timeout`` overrides the client's read budget, which long polling needs: it
        asks Telegram to hold the connection open, so the client must be willing to
        wait longer than the server was asked to.
        """
        return self._call(method, payload, timeout=timeout)

    # -- transport -----------------------------------------------------------

    def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        sending: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        url = f"{self._api_root}/bot{self._token}/{method}"
        attempts = MAX_SEND_ATTEMPTS if sending else 1

        for attempt in range(1, attempts + 1):
            try:
                # Long polling deliberately holds the connection open, so the read
                # budget has to exceed what the server was asked to wait. Without the
                # override the client gives up first and every poll looks like a
                # timeout — which is exactly what it did the first time this ran live.
                response = (
                    self._client.post(url, json=payload, timeout=timeout)
                    if timeout is not None
                    else self._client.post(url, json=payload)
                )
            except httpx.TimeoutException as exc:
                # A timeout on a read means the request was written. Telegram may have
                # created the post and we simply never heard. This is the one case where
                # retrying is actively dangerous.
                if sending:
                    raise PublicationOutcomeUncertainError(
                        "the send timed out after the request was sent, so it may or may "
                        "not have reached the channel. Nothing was retried. Check the "
                        f"channel and resolve the attempt by hand. ({redact(str(exc))})"
                    ) from exc
                raise TelegramApiError(f"{method} timed out: {redact(str(exc))}") from exc
            except httpx.TransportError as exc:
                # Connection never established: nothing was delivered, so a retry is safe.
                if attempt < attempts:
                    logger.warning(
                        "telegram request failed before delivery, retrying",
                        extra={"method": method, "attempt": attempt},
                    )
                    continue
                raise TelegramApiError(
                    f"could not reach Telegram for {method}: {redact(str(exc))}"
                ) from exc

            try:
                body = response.json()
            except ValueError as exc:
                if sending and response.status_code == httpx.codes.OK:
                    raise PublicationOutcomeUncertainError(
                        "Telegram returned 200 with a body this client could not parse, "
                        "so the post may exist. Nothing was retried."
                    ) from exc
                raise TelegramApiError(
                    f"{method} returned a non-JSON response (HTTP {response.status_code})"
                ) from exc

            if not isinstance(body, dict):
                raise TelegramApiError(f"{method} returned an unexpected response shape")

            if body.get("ok") is True:
                result = body.get("result")
                # Most methods return an object; getUpdates returns an array, and
                # answerCallbackQuery returns a bare true. All three are valid.
                if isinstance(result, dict):
                    return result
                if isinstance(result, list):
                    return {"result": result}
                if result is True:
                    return {}
                raise TelegramApiError(f"{method} returned an unusable result")

            error = self._classify(method, response.status_code, body)
            if (
                isinstance(error, TelegramRateLimitError)
                and sending
                and attempt < attempts
                and error.retry_after is not None
                and error.retry_after <= MAX_RETRY_AFTER_SECONDS
            ):
                logger.warning(
                    "telegram rate limited, waiting as instructed",
                    extra={"method": method, "retry_after": error.retry_after},
                )
                time.sleep(error.retry_after)
                continue
            raise error

        raise TelegramApiError(f"{method} exhausted its attempts")  # pragma: no cover

    def _classify(self, method: str, status_code: int, body: dict[str, Any]) -> Exception:
        """Turn a Bot API error into something the caller can act on.

        The description is scrubbed before it is ever attached to an exception: an
        upstream message can echo the request, and an exception is printed.
        """
        description = redact(str(body.get("description", "")))
        code = body.get("error_code")
        error_code = int(code) if isinstance(code, int) else status_code
        parameters = body.get("parameters") or {}
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None

        message = f"{method} failed ({error_code}): {description}"

        if error_code == 401:
            return TelegramAuthenticationError(
                f"{message}. The bot token is missing or wrong — check "
                "AI_NEWS_TELEGRAM_BOT_TOKEN."
            )
        if error_code == 403:
            return TelegramPermissionError(
                f"{message}. Add the bot to the channel as an administrator with "
                "'Post Messages'."
            )
        if error_code == 429:
            return TelegramRateLimitError(
                message, retry_after=float(retry_after) if retry_after is not None else None
            )
        if error_code == 400:
            lowered = description.lower()
            if "chat not found" in lowered or "chat_id is empty" in lowered:
                return TelegramDestinationError(
                    f"{message}. Check AI_NEWS_TELEGRAM_CHANNEL, and note that a bot can "
                    "only resolve a chat it has been added to."
                )
            return TelegramContentError(message)
        return TelegramApiError(message, error_code=error_code)


class TelegramPublisher:
    """Sends an approved draft version to a Telegram channel.

    Implements :class:`publishing.base.Publisher`. It cannot approve anything and it
    cannot construct a :class:`PublishAuthorization` — it can only receive one that the
    gate already issued and verified. That is not a convention: the authorization's
    constructor refuses to run outside the gate.
    """

    name = "telegram"

    def __init__(self, client: TelegramClient, channel: str) -> None:
        self._client = client
        self._channel = channel

    def publish(
        self, version: DraftVersion, authorization: PublishAuthorization
    ) -> PublicationReceipt:
        """Send ``version``. Called only after the gate has verified the authorization.

        Raises:
            ApprovalInvalidatedError: the authorization does not cover this version.
        """
        # Belt and braces. publish_with_gate has already checked this; checking again
        # here costs one comparison and closes the door on a future caller that reaches
        # the publisher directly.
        if not authorization.authorizes(version):
            from ai_news_editor.domain.errors import ApprovalInvalidatedError

            raise ApprovalInvalidatedError(
                "the authorization does not cover the version handed to the publisher"
            )

        message = build_message(version)
        sent = self._client.send_message(message.to_payload(self._channel))

        logger.info(
            "published to telegram",
            extra={
                "draft_id": str(authorization.draft_id),
                "draft_version_id": str(authorization.draft_version_id),
                "version_no": authorization.version_no,
                "channel": self._channel,
                "message_id": sent.message_id,
            },
        )
        return PublicationReceipt(
            draft_id=authorization.draft_id,
            draft_version_id=authorization.draft_version_id,
            external_id=str(sent.message_id),
            target=self._channel,
            published_at=now_utc(),
        )
