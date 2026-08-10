"""The Telegram Bot API client, driven entirely by httpx.MockTransport.

Nothing here touches the network. Every response is one this test wrote, including the
malformed ones — those are the interesting cases, because a publisher that mishandles a
weird response is a publisher that either double-posts or loses a post.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ai_news_editor.domain.errors import (
    PublicationOutcomeUncertainError,
    TelegramApiError,
    TelegramAuthenticationError,
    TelegramContentError,
    TelegramDestinationError,
    TelegramPermissionError,
    TelegramRateLimitError,
)
from ai_news_editor.publishing.telegram import TelegramClient

TOKEN = "123456789:" + "A" * 35


def client_with(handler: Any) -> TelegramClient:
    return TelegramClient(TOKEN, transport=httpx.MockTransport(handler))


def responds(payload: dict[str, Any], status: int = 200) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def fails(code: int, description: str, parameters: dict[str, Any] | None = None) -> Any:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return responds(body, status=code)


class TestConstruction:
    def test_an_empty_token_is_refused_before_any_request(self) -> None:
        with pytest.raises(TelegramAuthenticationError, match="AI_NEWS_TELEGRAM_BOT_TOKEN"):
            TelegramClient("")

    def test_a_whitespace_token_is_refused(self) -> None:
        with pytest.raises(TelegramAuthenticationError):
            TelegramClient("   ")


class TestGetMe:
    def test_a_valid_token_returns_the_bot_identity(self) -> None:
        with client_with(
            responds({"ok": True, "result": {"id": 42, "is_bot": True,
                                             "first_name": "News", "username": "ai_news_bot"}})
        ) as client:
            identity = client.get_me()
        assert identity.id == 42
        assert identity.username == "ai_news_bot"

    def test_an_invalid_token_is_an_authentication_error(self) -> None:
        with client_with(fails(401, "Unauthorized")) as client, pytest.raises(
            TelegramAuthenticationError, match="AI_NEWS_TELEGRAM_BOT_TOKEN"
        ):
            client.get_me()

    def test_the_token_reaches_the_url_path(self) -> None:
        """It has to — the Bot API puts it there. The test is that nothing else does."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "first_name": "B"}})

        with client_with(handler) as client:
            client.get_me()
        assert seen[0].endswith(f"/bot{TOKEN}/getMe")


class TestGetChat:
    def test_a_channel_resolves(self) -> None:
        with client_with(
            responds({"ok": True, "result": {"id": -1001234567890, "type": "channel",
                                             "title": "AI новини", "username": "ai_news_ua"}})
        ) as client:
            chat = client.get_chat("@ai_news_ua")
        assert chat.id == -1001234567890
        assert chat.postable

    def test_an_unknown_chat_is_a_destination_error(self) -> None:
        with client_with(fails(400, "Bad Request: chat not found")) as client, pytest.raises(
            TelegramDestinationError, match="AI_NEWS_TELEGRAM_CHANNEL"
        ):
            client.get_chat("@nope")

    def test_a_forbidden_chat_is_a_permission_error(self) -> None:
        with client_with(
            fails(403, "Forbidden: bot is not a member of the channel chat")
        ) as client, pytest.raises(TelegramPermissionError, match="administrator"):
            client.get_chat("@private")

    def test_an_unpostable_chat_type_is_reported_not_raised(self) -> None:
        with client_with(
            responds({"ok": True, "result": {"id": 7, "type": "sender"}})
        ) as client:
            assert client.get_chat("@x").postable is False


class TestPostingRights:
    def test_an_administrator_with_posting_rights(self) -> None:
        with client_with(
            responds({"ok": True, "result": {"status": "administrator",
                                             "can_post_messages": True}})
        ) as client:
            rights = client.get_posting_rights("@c", 42)
        assert rights.can_post is True

    def test_an_administrator_without_posting_rights(self) -> None:
        with client_with(
            responds({"ok": True, "result": {"status": "administrator",
                                             "can_post_messages": False}})
        ) as client:
            rights = client.get_posting_rights("@c", 42)
        assert rights.can_post is False
        assert "Post Messages" in rights.detail

    def test_the_owner_may_post(self) -> None:
        with client_with(responds({"ok": True, "result": {"status": "creator"}})) as client:
            assert client.get_posting_rights("@c", 42).can_post is True

    def test_a_bot_that_was_removed_may_not_post(self) -> None:
        with client_with(responds({"ok": True, "result": {"status": "left"}})) as client:
            rights = client.get_posting_rights("@c", 42)
        assert rights.can_post is False

    def test_an_unanswerable_question_is_reported_as_unknown(self) -> None:
        """Better an honest 'unknown' than a diagnostic that invents a pass."""
        with client_with(fails(400, "Bad Request: method is available for supergroup")) as client:
            rights = client.get_posting_rights("@c", 42)
        assert rights.can_post is None
        assert "could not answer" in rights.detail

    def test_an_administrator_with_no_reported_flag_is_unknown(self) -> None:
        with client_with(
            responds({"ok": True, "result": {"status": "administrator"}})
        ) as client:
            assert client.get_posting_rights("@c", 42).can_post is None


class TestSendMessage:
    def test_a_successful_send_returns_the_message_id(self) -> None:
        with client_with(
            responds({"ok": True, "result": {"message_id": 991, "chat": {"id": -100999}}})
        ) as client:
            sent = client.send_message({"chat_id": "@c", "text": "привіт"})
        assert sent.message_id == 991
        assert sent.chat_id == "-100999"

    def test_the_payload_is_posted_as_json(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1,
                                                                    "chat": {"id": 2}}})

        with client_with(handler) as client:
            client.send_message({"chat_id": "@c", "text": "привіт", "parse_mode": "HTML"})
        assert seen == [{"chat_id": "@c", "text": "привіт", "parse_mode": "HTML"}]

    def test_a_bad_request_about_content_is_a_content_error(self) -> None:
        with client_with(
            fails(400, "Bad Request: can't parse entities")
        ) as client, pytest.raises(TelegramContentError):
            client.send_message({"chat_id": "@c", "text": "<b>oops"})

    def test_a_forbidden_send_is_a_permission_error(self) -> None:
        with client_with(
            fails(403, "Forbidden: bot can't initiate conversation")
        ) as client, pytest.raises(TelegramPermissionError):
            client.send_message({"chat_id": "@c", "text": "x"})

    def test_a_server_error_is_a_generic_api_error(self) -> None:
        with client_with(fails(500, "Internal Server Error")) as client, pytest.raises(
            TelegramApiError
        ):
            client.send_message({"chat_id": "@c", "text": "x"})

    def test_a_malformed_json_body_is_reported(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"<html>gateway</html>")

        with client_with(handler) as client, pytest.raises(TelegramApiError, match="non-JSON"):
            client.send_message({"chat_id": "@c", "text": "x"})

    def test_a_json_array_instead_of_an_object_is_reported(self) -> None:
        with client_with(responds([1, 2, 3])) as client, pytest.raises(  # type: ignore[arg-type]
            TelegramApiError, match="unexpected response shape"
        ):
            client.send_message({"chat_id": "@c", "text": "x"})

    def test_an_ok_response_with_no_result_is_reported(self) -> None:
        with client_with(responds({"ok": True})) as client, pytest.raises(
            TelegramApiError, match="no result object"
        ):
            client.send_message({"chat_id": "@c", "text": "x"})


class TestRateLimiting:
    def test_a_429_with_a_short_retry_after_is_waited_out_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []
        monkeypatch.setattr("time.sleep", slept.append)
        calls: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, json={"ok": False, "error_code": 429,
                                                 "description": "Too Many Requests",
                                                 "parameters": {"retry_after": 3}})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 5,
                                                                    "chat": {"id": 1}}})

        with client_with(handler) as client:
            sent = client.send_message({"chat_id": "@c", "text": "x"})
        assert sent.message_id == 5
        assert slept == [3.0]
        assert len(calls) == 2

    def test_a_long_retry_after_is_not_waited_out(self) -> None:
        """Sleeping for ten minutes inside a CLI is not error handling."""
        with client_with(
            fails(429, "Too Many Requests", {"retry_after": 600})
        ) as client, pytest.raises(TelegramRateLimitError) as excinfo:
            client.send_message({"chat_id": "@c", "text": "x"})
        assert excinfo.value.retry_after == 600.0

    def test_retries_are_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("time.sleep", lambda _s: None)
        calls: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, json={"ok": False, "error_code": 429,
                                             "description": "slow down",
                                             "parameters": {"retry_after": 1}})

        with client_with(handler) as client, pytest.raises(TelegramRateLimitError):
            client.send_message({"chat_id": "@c", "text": "x"})
        assert len(calls) == 2

    def test_a_read_only_call_is_never_retried(self) -> None:
        calls: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, json={"ok": False, "error_code": 429,
                                             "description": "slow down",
                                             "parameters": {"retry_after": 1}})

        with client_with(handler) as client, pytest.raises(TelegramRateLimitError):
            client.get_me()
        assert len(calls) == 1


class TestNetworkAmbiguity:
    """The hardest case: did the message go out or not?"""

    def test_a_timeout_during_a_send_is_uncertain_not_failed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        with client_with(handler) as client, pytest.raises(
            PublicationOutcomeUncertainError, match="may or may not"
        ):
            client.send_message({"chat_id": "@c", "text": "x"})

    def test_an_uncertain_send_is_never_retried(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.ReadTimeout("timed out", request=request)

        with client_with(handler) as client, pytest.raises(PublicationOutcomeUncertainError):
            client.send_message({"chat_id": "@c", "text": "x"})
        assert len(calls) == 1, "a possibly-delivered message must not be sent twice"

    def test_a_connection_that_never_opened_is_a_definite_failure(self) -> None:
        """Nothing was delivered, so this one is safe to retry and safe to call failed."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.ConnectError("connection refused", request=request)

        with client_with(handler) as client, pytest.raises(
            TelegramApiError, match="could not reach"
        ):
            client.send_message({"chat_id": "@c", "text": "x"})
        assert len(calls) == 2

    def test_a_two_hundred_with_an_unparseable_body_is_uncertain(self) -> None:
        """Telegram said OK; we could not read it. The post may exist."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        with client_with(handler) as client, pytest.raises(
            PublicationOutcomeUncertainError, match="may exist"
        ):
            client.send_message({"chat_id": "@c", "text": "x"})

    def test_a_timeout_on_a_read_only_call_is_an_ordinary_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        with client_with(handler) as client, pytest.raises(TelegramApiError, match="timed out"):
            client.get_me()


class TestTokenNeverLeaks:
    def test_the_token_is_absent_from_an_authentication_error(self) -> None:
        with (
            client_with(fails(401, "Unauthorized")) as client,
            pytest.raises(TelegramAuthenticationError) as excinfo,
        ):
            client.get_me()
        assert TOKEN not in str(excinfo.value)

    def test_the_token_is_absent_from_a_transport_error(self) -> None:
        """httpx puts the request URL in its exception message. The URL holds the token."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed to connect to {request.url}", request=request)

        with client_with(handler) as client, pytest.raises(TelegramApiError) as excinfo:
            client.get_me()
        assert TOKEN not in str(excinfo.value)
        assert "[REDACTED]" in str(excinfo.value)

    def test_the_token_is_absent_when_telegram_echoes_it_back(self) -> None:
        with (
            client_with(fails(400, f"Bad Request: token {TOKEN} is wrong")) as client,
            pytest.raises(Exception) as excinfo,
        ):
            client.send_message({"chat_id": "@c", "text": "x"})
        assert TOKEN not in str(excinfo.value)

    def test_the_token_is_absent_from_an_uncertain_outcome(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(f"timeout on {request.url}", request=request)

        with (
            client_with(handler) as client,
            pytest.raises(PublicationOutcomeUncertainError) as excinfo,
        ):
            client.send_message({"chat_id": "@c", "text": "x"})
        assert TOKEN not in str(excinfo.value)


class TestUnusualMembership:
    def test_an_unrecognised_member_status_is_unknown_not_a_pass(self) -> None:
        with client_with(responds({"ok": True, "result": {"status": "restricted"}})) as client:
            rights = client.get_posting_rights("@c", 42)
        assert rights.can_post is None
        assert "restricted" in rights.detail
