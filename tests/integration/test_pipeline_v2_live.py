"""automation.pipeline_v2_live.run_pass_v2 — the scheduled, live-only v2 entrypoint.

Real migrated SQLite connection, real collection (a mocked RSS feed + article page),
real repositories. Gemini and Telegram are the only mocked boundaries.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_news_editor.automation.gemini import GeminiClient
from ai_news_editor.automation.pipeline import (
    AUTOMATION_ACTOR,
    Outcome,
    production_publications_today,
)
from ai_news_editor.automation.pipeline_v2_live import run_pass_v2
from ai_news_editor.domain.clock import now_utc
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.settings import Settings
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.storage import db
from ai_news_editor.storage.repositories import DraftRepository

FAKE_GEMINI_KEY = "AIzaSy" + "z" * 33
FAKE_TELEGRAM_TOKEN = "123456789:" + "A" * 35
PRODUCTION_CHANNEL = "@real_production_channel"

_FEED_URL = "https://vendor.example.invalid/feed.xml"
_ARTICLE_URL = "https://vendor.example.invalid/posts/new-tool"
_FEED_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Vendor Blog</title>
    <link>https://vendor.example.invalid/</link>
    <description>Synthetic feed for run_pass_v2 end-to-end tests.</description>
    <item>
      <title>New tool announced</title>
      <link>{_ARTICLE_URL}</link>
      <guid isPermaLink="false">test-guid-v2-0001</guid>
      <author>editor@vendor.example.invalid (A. Editor)</author>
      <pubDate>Mon, 03 Aug 2026 10:30:00 +0000</pubDate>
      <description>A summary of a new tool announced today.</description>
      <content:encoded><![CDATA[<p>A vendor announced a new AI tool today, aimed at
      developers automating their workflows.</p>]]></content:encoded>
    </item>
  </channel>
</rss>"""
_ARTICLE_HTML = (
    "<html><body><article><p>"
    + ("A vendor announced a new AI tool today. " * 30)
    + "</p></article></body></html>"
)

_AI_TOOL_CLASSIFICATION = {
    "content_type": "AI_TOOL",
    "evidence_type": "OFFICIAL_PRODUCT_PAGE",
    "reason": "Product page.",
    "rejection_reason": None,
    "is_speculative_doom": False,
    "is_about_forbidden_geography": False,
}
_NEWS_CLASSIFICATION = {
    "content_type": "NEWS",
    "evidence_type": "PRIMARY_SOURCE",
    "reason": "News item.",
    "rejection_reason": None,
    "is_speculative_doom": False,
    "is_about_forbidden_geography": False,
}
_AI_TOOL_GENERATION = {
    "headline": "Vendor запускає новий інструмент",
    "body": [{"purpose": "what_it_is", "text": "Vendor представив новий інструмент."}],
    "detail_bullets": [],
    "source_label": "Vendor",
    "prompt_origin": None,
    "prompt_text": None,
    "free_deal_kind": None,
    "research_framing": None,
    "research_independently_verified": None,
    "digest_items": [],
    "confidence": 90,
    "rejection_reason": None,
}
_NEWS_GENERATION = {
    "headline": "Vendor оголошує зміну",
    "body": [{"purpose": "what_happened", "text": "Vendor оголосив про зміну."}],
    "detail_bullets": [],
    "source_label": "Vendor",
    "prompt_origin": None,
    "prompt_text": None,
    "free_deal_kind": None,
    "research_framing": None,
    "research_independently_verified": None,
    "digest_items": [],
    "confidence": 90,
    "rejection_reason": None,
}


def sources_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(
        f"""\
version: 1
defaults:
  timeout_seconds: 20
  max_items_per_fetch: 50
sources:
  - id: vendor_blog
    name: Vendor Blog
    adapter: rss
    url: {_FEED_URL}
    trust_tier: OFFICIAL
    editorial_role: Primary official source for run_pass_v2 end-to-end tests.
    priority: PRIMARY_NORMAL
    content_types: [NEWS, AI_TOOL]
    publisher_region: UNITED_STATES
""",
        encoding="utf-8",
    )
    return path


def build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    data: dict[str, Any] = {
        "_env_file": None,
        "data_dir": tmp_path,
        "media_dir": tmp_path / "media",
        "automation_enabled": True,
        "gemini_api_key": FAKE_GEMINI_KEY,
        "llm_model": "gemini-test",
        "telegram_bot_token": FAKE_TELEGRAM_TOKEN,
        "telegram_channel": PRODUCTION_CHANNEL,
        "test_channel": "@test_channel",
        "daily_post_limit": 4,
        "sources_config_path": sources_yaml(tmp_path),
    }
    data.update(overrides)
    return Settings(**data)  # type: ignore[arg-type]


def gemini_transport(*payloads: dict[str, Any]) -> httpx.MockTransport:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        index = len(calls)
        calls.append(json.loads(request.content))
        payload = payloads[min(index, len(payloads) - 1)] if payloads else {}
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps(payload)}]}, "finishReason": "STOP"}
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


def http_transport() -> httpx.MockTransport:
    """Answers the RSS feed and article fetches for real; everything else (Wikimedia
    Commons open-license media search, and any other discovery/media call the v2
    pipeline makes past collection) gets a harmless empty JSON response, so media
    selection falls through to the branded-card fallback exactly like a real "no
    usable open-license media found" outcome — never an assertion failure for a call
    this test doesn't care about the specifics of.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _FEED_URL:
            return httpx.Response(
                200, content=_FEED_XML.encode(), headers={"content-type": "application/rss+xml"}
            )
        if url == _ARTICLE_URL:
            return httpx.Response(
                200, content=_ARTICLE_HTML.encode(), headers={"content-type": "text/html"}
            )
        return httpx.Response(200, json={"query": {"pages": {}}})

    return httpx.MockTransport(handler)


def telegram_transport() -> httpx.MockTransport:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("getChat"):
            return httpx.Response(
                200, json={"ok": True, "result": {"id": -100777, "type": "channel"}}
            )
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 555, "chat": {"id": -100777}}}
        )

    transport = httpx.MockTransport(handler)
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


_ORIGINAL_INITS = {
    GeminiClient: GeminiClient.__init__,
    TelegramClient: TelegramClient.__init__,
    HttpClient: HttpClient.__init__,
}


def patch_clients(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gemini: httpx.MockTransport | None,
    telegram: httpx.MockTransport | None,
    http: httpx.MockTransport | None,
) -> None:
    if gemini is not None:

        def patched_gemini(self, api_key, *, model, transport=None, **kwargs):  # type: ignore[no-untyped-def]
            _ORIGINAL_INITS[GeminiClient](self, api_key, model=model, transport=gemini, **kwargs)

        monkeypatch.setattr(GeminiClient, "__init__", patched_gemini)

    if telegram is not None:

        def patched_telegram(self, token, *, transport=None, **kwargs):  # type: ignore[no-untyped-def]
            _ORIGINAL_INITS[TelegramClient](self, token, transport=telegram, **kwargs)

        monkeypatch.setattr(TelegramClient, "__init__", patched_telegram)

    if http is not None:

        def patched_http(self, *, transport=None, **kwargs):  # type: ignore[no-untyped-def]
            _ORIGINAL_INITS[HttpClient](self, transport=http, **kwargs)

        monkeypatch.setattr(HttpClient, "__init__", patched_http)


@pytest.fixture
def connection(tmp_path):  # type: ignore[no-untyped-def]
    conn = db.connect(tmp_path / "production.db")
    db.migrate(conn)
    return conn


class TestKillSwitchAndConfig:
    def test_disabled_makes_no_network_calls_at_all(
        self, tmp_path: Path, connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        settings = build_settings(tmp_path, automation_enabled=False)

        def fail(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("no HTTP call should happen when disabled")

        monkeypatch.setattr(HttpClient, "__init__", lambda self, **kw: fail())
        result = run_pass_v2(connection, settings)
        assert result.outcome is Outcome.DISABLED

    def test_missing_gemini_key_is_a_config_error(self, tmp_path: Path, connection) -> None:  # type: ignore[no-untyped-def]
        settings = build_settings(tmp_path, gemini_api_key=None)
        result = run_pass_v2(connection, settings)
        assert result.outcome is Outcome.CONFIG_ERROR
        assert "GEMINI_API_KEY" in result.detail


class TestSuccessfulPublication:
    def test_an_ai_tool_candidate_is_collected_classified_and_published(
        self, tmp_path: Path, connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        settings = build_settings(tmp_path)
        patch_clients(
            monkeypatch,
            gemini=gemini_transport(_AI_TOOL_CLASSIFICATION, _AI_TOOL_GENERATION),
            telegram=telegram_transport(),
            http=http_transport(),
        )

        result = run_pass_v2(connection, settings)

        assert result.outcome is Outcome.PUBLISHED, result.detail
        assert result.message_id == 555
        assert result.channel == PRODUCTION_CHANNEL

        version = DraftRepository(connection).current_version(result.draft_id)
        assert version.created_by == AUTOMATION_ACTOR  # not the manual smoke-test actor

        # The shared daily-limit ledger (automation.pipeline's own function) sees it.
        assert production_publications_today(connection, settings, now_utc()) == 1

    def test_news_is_no_longer_excluded_on_the_live_schedule(
        self, tmp_path: Path, connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The manual smoke test hard-excluded NEWS/RESEARCH; the real scheduled v2
        pipeline (a full cutover from v1) must not — NEWS is a normal v2 category."""
        settings = build_settings(tmp_path)
        patch_clients(
            monkeypatch,
            gemini=gemini_transport(_NEWS_CLASSIFICATION, _NEWS_GENERATION),
            telegram=telegram_transport(),
            http=http_transport(),
        )

        result = run_pass_v2(connection, settings)

        assert result.outcome is Outcome.PUBLISHED, result.detail


class TestNoEligibleCandidate:
    def test_an_empty_pool_is_a_quiet_no_candidate_outcome(
        self, tmp_path: Path, connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        settings = build_settings(tmp_path)

        def empty_feed(request: httpx.Request) -> httpx.Response:
            if str(request.url) == _FEED_URL:
                return httpx.Response(
                    200,
                    content=b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>',
                    headers={"content-type": "application/rss+xml"},
                )
            raise AssertionError(f"unexpected request: {request.url}")

        telegram = telegram_transport()
        patch_clients(
            monkeypatch, gemini=gemini_transport(), telegram=telegram,
            http=httpx.MockTransport(empty_feed),
        )

        result = run_pass_v2(connection, settings)

        assert result.outcome is Outcome.NO_CANDIDATE
        assert telegram.calls == []  # type: ignore[attr-defined]


class TestGeminiQuotaFailure:
    def test_a_429_fails_the_slot_cleanly_with_no_send(
        self, tmp_path: Path, connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        settings = build_settings(tmp_path)

        def quota_exhausted(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "quota exceeded"}})

        telegram = telegram_transport()
        patch_clients(
            monkeypatch, gemini=httpx.MockTransport(quota_exhausted), telegram=telegram,
            http=http_transport(),
        )

        result = run_pass_v2(connection, settings)

        assert result.outcome is Outcome.GEMINI_ERROR
        assert telegram.calls == []  # type: ignore[attr-defined]


class TestDailyLimitSharedAcrossPipelineVersions:
    def test_a_v2_publish_reaching_the_limit_blocks_a_second_v2_run_the_same_day(
        self, tmp_path: Path, connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Proves the daily cap is a property of the production channel, not of which
        pipeline version published — the exact concern behind sharing
        automation.pipeline.AUTOMATION_ACTOR and production_publications_today."""
        settings = build_settings(tmp_path, daily_post_limit=1)
        patch_clients(
            monkeypatch,
            gemini=gemini_transport(_AI_TOOL_CLASSIFICATION, _AI_TOOL_GENERATION),
            telegram=telegram_transport(),
            http=http_transport(),
        )
        first = run_pass_v2(connection, settings)
        assert first.outcome is Outcome.PUBLISHED, first.detail

        telegram_second = telegram_transport()
        patch_clients(
            monkeypatch, gemini=gemini_transport(), telegram=telegram_second, http=http_transport()
        )
        second = run_pass_v2(connection, settings)

        assert second.outcome is Outcome.DAILY_LIMIT_REACHED
        assert telegram_second.calls == []  # type: ignore[attr-defined]
