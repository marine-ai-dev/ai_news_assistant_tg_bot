"""automation.soak.run_soak — Step 6 sections 2, 7, 16, 17, 18.

Proves the real TEST-only soak entrypoint wires real DB candidates through the actual
v2 pipeline and actually calls TelegramClient.send_message — with everything except
Gemini and Telegram mocked (both via httpx.MockTransport, never real network).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ai_news_editor.automation.gemini import GeminiClient
from ai_news_editor.automation.soak import eligible_articles_v2, run_soak
from ai_news_editor.domain.enums import ArticleStatus
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.sources.config import SourcesConfig
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.storage import db
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    RawItemRepository,
    SourceRepository,
)
from tests.conftest import make_article, make_raw_item, make_source

FAKE_KEY = "fake-test-key"
TOKEN = "123456789:" + "A" * 35
CHANNEL = "@test_channel"

_ARTICLE_BODY = (
    "ExampleCorp сьогодні офіційно оголосила про новий інструмент. " * 20
)
_ARTICLE_HTML = f"<html><body><article><p>{_ARTICLE_BODY}</p></article></body></html>"

_GENERATION_PAYLOAD = {
    "headline": "ExampleCorp запускає новий інструмент",
    "body": [{"purpose": "what_happened", "text": "ExampleCorp представила новий інструмент."}],
    "detail_bullets": [],
    "source_label": "ExampleCorp",
    "prompt_origin": None,
    "prompt_text": None,
    "free_deal_kind": None,
    "research_framing": None,
    "research_independently_verified": None,
    "digest_items": [],
    "confidence": 90,
    "rejection_reason": None,
}

_CLASSIFICATION_PAYLOAD = {
    "content_type": "NEWS",
    "evidence_type": "PRIMARY_SOURCE",
    "reason": "Офіційне оголошення.",
    "rejection_reason": None,
}


def _gemini_transport() -> httpx.MockTransport:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        index = len(calls)
        calls.append(json.loads(request.content))
        # Alternate classification/generation per candidate: every even-indexed call
        # is a classification, every odd one a generation.
        payload = _CLASSIFICATION_PAYLOAD if index % 2 == 0 else _GENERATION_PAYLOAD
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


def _article_http_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=_ARTICLE_HTML.encode()
        )

    return httpx.MockTransport(handler)


def _telegram_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 555, "chat": {"id": -100777}}}
        )

    return httpx.MockTransport(handler)


def _registry() -> SourcesConfig:
    return SourcesConfig.model_validate(
        {
            "version": 1,
            "sources": [
                {
                    "id": "official_vendor",
                    "name": "ExampleCorp",
                    "adapter": "rss",
                    "url": "https://vendor.example.invalid/feed.xml",
                    "trust_tier": "OFFICIAL",
                    "editorial_role": "test",
                    "priority": "PRIMARY_NORMAL",
                    "content_types": ["NEWS"],
                },
                {
                    "id": "second_vendor",
                    "name": "SecondCorp",
                    "adapter": "rss",
                    "url": "https://second.example.invalid/feed.xml",
                    "trust_tier": "OFFICIAL",
                    "editorial_role": "test",
                    "priority": "PRIMARY_NORMAL",
                    "content_types": ["NEWS"],
                },
            ],
        }
    )


@pytest.fixture
def connection(tmp_path):  # type: ignore[no-untyped-def]
    conn = db.connect(tmp_path / "soak.db")
    db.migrate(conn)
    return conn


def _seed_article(connection, source_id: str, url: str, title: str) -> None:  # type: ignore[no-untyped-def]
    sources = SourceRepository(connection)
    raw_items = RawItemRepository(connection)
    articles = ArticleRepository(connection)
    sources.upsert(make_source(source_id))
    item = raw_items.add(make_raw_item(source_id, payload_raw=json.dumps({"title": title})))
    article = articles.add(
        make_article(item.id, source_id, title=title, canonical_url=url, clean_text="x" * 900)
    )
    articles.set_status(article.id, ArticleStatus.NORMALIZED)


class TestEligibleArticlesV2:
    def test_reads_normalized_articles_across_any_trust_tier(self, connection) -> None:  # type: ignore[no-untyped-def]
        _seed_article(connection, "official_vendor", "https://vendor.example.invalid/1", "Story A")
        eligible = eligible_articles_v2(connection)
        assert len(eligible) == 1
        assert eligible[0].title == "Story A"

    def test_excludes_already_used_articles(self, connection) -> None:  # type: ignore[no-untyped-def]
        _seed_article(connection, "official_vendor", "https://vendor.example.invalid/1", "Story A")
        eligible = eligible_articles_v2(connection)
        excluded = frozenset({eligible[0].id})
        assert eligible_articles_v2(connection, exclude_article_ids=excluded) == []


class TestRunSoakEndToEnd:
    def test_sends_real_posts_for_distinct_real_candidates(self, connection) -> None:  # type: ignore[no-untyped-def]
        _seed_article(
            connection, "official_vendor", "https://vendor.example.invalid/story-1", "First story"
        )
        _seed_article(
            connection, "second_vendor", "https://second.example.invalid/story-2", "Second story"
        )
        registry = _registry()

        gemini_transport = _gemini_transport()
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=gemini_transport)
        http = HttpClient(transport=_article_http_transport())
        telegram = TelegramClient(TOKEN, transport=_telegram_transport())

        results = run_soak(
            connection,
            client=client,
            registry=registry,
            http=http,
            telegram=telegram,
            target_channel=CHANNEL,
            count=2,
            delay_seconds=0.0,
        )

        assert len(results) == 2
        # Two distinct real articles were used — no repeat within the batch.
        assert results[0].article_id != results[1].article_id
        # Every post actually reached the (mocked) Telegram transport with a real
        # message id recorded.
        for result in results:
            assert all(c.message_id is not None for c in result.component_outcomes)

    def test_stops_when_candidates_are_exhausted_rather_than_looping_forever(
        self, connection
    ) -> None:  # type: ignore[no-untyped-def]
        _seed_article(
            connection, "official_vendor", "https://vendor.example.invalid/only-story", "Only story"
        )
        registry = _registry()
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_gemini_transport())
        http = HttpClient(transport=_article_http_transport())
        telegram = TelegramClient(TOKEN, transport=_telegram_transport())

        results = run_soak(
            connection,
            client=client,
            registry=registry,
            http=http,
            telegram=telegram,
            target_channel=CHANNEL,
            count=5,  # more than the one real candidate available
            delay_seconds=0.0,
        )

        assert len(results) == 1
