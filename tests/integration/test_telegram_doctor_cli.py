"""``ai-news telegram doctor`` — and specifically its ``--test`` flag.

The production and test channels are unrelated Telegram destinations with unrelated
bot-membership state: the bot being an admin of one proves nothing about the other.
Before this flag existed, this command could only ever check the production channel —
there was no way to cheaply verify the test channel is reachable without running the
entire automation pipeline against it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from ai_news_editor.cli.main import app
from ai_news_editor.settings import get_settings

runner = CliRunner()

PROD_CHANNEL = "@prod_channel"
TEST_CHANNEL = "@test_channel"
#: Assembled at runtime, not written as a literal — see the project-wide convention.
TOKEN = "123456789:" + "A" * 35


def output_of(result: object) -> str:
    import contextlib

    parts = [getattr(result, "output", "") or ""]
    with contextlib.suppress(AttributeError, ValueError):
        parts.append(result.stderr or "")  # type: ignore[attr-defined]
    return " ".join(" ".join(parts).split())


def _patch_telegram(monkeypatch: pytest.MonkeyPatch, *, channel: str) -> list[dict[str, Any]]:
    """A Telegram that only resolves the given channel — the other one, if ever
    queried by mistake, would hit getChat for the wrong destination and fail loudly
    rather than silently succeeding against the wrong channel."""
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append({"url": str(request.url)})
        if request.url.path.endswith("getMe"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "News",
                                             "username": "test_bot"}},
            )
        if request.url.path.endswith("getChat"):
            body = json.loads(request.content) if request.content else {}
            if body.get("chat_id") != channel:
                return httpx.Response(
                    400, json={"ok": False, "description": "Bad Request: chat not found"}
                )
            return httpx.Response(
                200,
                json={"ok": True, "result": {"id": -100777, "type": "channel",
                                             "title": "Channel", "username": channel.lstrip("@")}},
            )
        if request.url.path.endswith("getChatMember"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"status": "administrator",
                                             "can_post_messages": True}},
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    from ai_news_editor.publishing import telegram as telegram_module

    original = telegram_module.TelegramClient.__init__

    def patched(self, token, *, transport=None, **kwargs):  # type: ignore[no-untyped-def]
        original(self, token, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(telegram_module.TelegramClient, "__init__", patched)
    monkeypatch.setattr("ai_news_editor.cli.publish.TelegramClient", telegram_module.TelegramClient)
    return requests


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AI_NEWS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AI_NEWS_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("AI_NEWS_TELEGRAM_CHANNEL", PROD_CHANNEL)
    monkeypatch.setenv("AI_NEWS_TEST_CHANNEL", TEST_CHANNEL)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestTelegramDoctorChannelTarget:
    def test_without_the_flag_checks_the_production_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests = _patch_telegram(monkeypatch, channel=PROD_CHANNEL)
        result = runner.invoke(app, ["telegram", "doctor"])
        assert result.exit_code == 0, output_of(result)
        assert f"Destination: {PROD_CHANNEL}" in output_of(result)
        assert requests  # the mock was actually reached

    def test_the_test_flag_checks_the_test_channel_instead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests = _patch_telegram(monkeypatch, channel=TEST_CHANNEL)
        result = runner.invoke(app, ["telegram", "doctor", "--test"])
        assert result.exit_code == 0, output_of(result)
        assert f"Destination: {TEST_CHANNEL}" in output_of(result)
        assert requests

    def test_the_test_flag_never_resolves_the_production_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mock only resolves TEST_CHANNEL — if --test accidentally checked
        production instead, this would fail with 'chat not found', not silently
        report success against the wrong destination."""
        _patch_telegram(monkeypatch, channel=TEST_CHANNEL)
        result = runner.invoke(app, ["telegram", "doctor", "--test"])
        assert result.exit_code == 0, output_of(result)
        assert "FAIL" not in output_of(result)

    def test_the_test_flag_without_a_test_channel_configured_fails_clearly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AI_NEWS_TEST_CHANNEL", raising=False)
        get_settings.cache_clear()
        _patch_telegram(monkeypatch, channel=TEST_CHANNEL)
        result = runner.invoke(app, ["telegram", "doctor", "--test"])
        assert result.exit_code == 2
        assert "AI_NEWS_TEST_CHANNEL" in output_of(result)

    def test_never_sends_a_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        requests = _patch_telegram(monkeypatch, channel=TEST_CHANNEL)
        runner.invoke(app, ["telegram", "doctor", "--test"])
        paths = {r["url"] for r in requests}
        assert not any("sendMessage" in path for path in paths)
