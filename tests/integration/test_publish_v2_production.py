"""automation.publish_v2_production.publish_one_v2_post_to_production.

Everything except Gemini and Telegram is real: a real migrated SQLite connection (the
same shape as the canonical production database), real repositories, real DB writes.
Gemini and Telegram are mocked via httpx.MockTransport, never real network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ai_news_editor.automation.gemini import GeminiClient
from ai_news_editor.automation.publish_v2_production import (
    MANUAL_V2_ACTOR,
    NoEligibleCandidateError,
    publish_one_v2_post_to_production,
)
from ai_news_editor.domain.enums import ArticleStatus, DraftStatus, EditorialCategory
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.sources.config import SourcesConfig
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.storage import db
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    DraftRepository,
    PublicationRepository,
    RawItemRepository,
    SourceRepository,
)
from tests.conftest import make_article, make_raw_item, make_source

FAKE_KEY = "fake-test-key"
TOKEN = "123456789:" + "A" * 35
PRODUCTION_CHANNEL = "@real_production_channel"

_ARTICLE_BODY = "Автор ділиться практичним досвідом використання AI. " * 20
_ARTICLE_HTML = f"<html><body><article><p>{_ARTICLE_BODY}</p></article></body></html>"

_AI_TOOL_CLASSIFICATION = {
    "content_type": "AI_TOOL",
    "evidence_type": "OFFICIAL_PRODUCT_PAGE",
    "reason": "Product page.",
    "rejection_reason": None,
    "is_speculative_doom": False,
    "is_about_forbidden_geography": False,
    "is_ai_primary": True,
}

_NEWS_CLASSIFICATION = {
    "content_type": "NEWS",
    "evidence_type": "PRIMARY_SOURCE",
    "reason": "News item.",
    "rejection_reason": None,
    "is_speculative_doom": False,
    "is_about_forbidden_geography": False,
    "is_ai_primary": True,
}

def _news_generation_payload() -> dict[str, Any]:
    return {
        "headline": "ExampleCorp оголошує зміну",
        "body": [{"purpose": "what_happened", "text": "ExampleCorp оголосила про зміну."}],
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


_GENERATION_PAYLOAD = {
    "headline": "ExampleCorp запускає новий інструмент",
    "body": [{"purpose": "what_it_is", "text": "ExampleCorp представила новий інструмент."}],
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


def _gemini_transport(*payloads: dict[str, Any]) -> httpx.MockTransport:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        index = len(calls)
        calls.append(json.loads(request.content))
        payload = payloads[min(index, len(payloads) - 1)]
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
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 777, "chat": {"id": -100999}}}
        )

    transport = httpx.MockTransport(handler)
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


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
                    "content_types": ["NEWS", "AI_TOOL"],
                    "publisher_region": "UNITED_STATES",
                },
                {
                    "id": "second_vendor",
                    "name": "SecondCorp",
                    "adapter": "rss",
                    "url": "https://second.example.invalid/feed.xml",
                    "trust_tier": "OFFICIAL",
                    "editorial_role": "test",
                    "priority": "PRIMARY_NORMAL",
                    "content_types": ["NEWS", "AI_TOOL"],
                    "publisher_region": "UNITED_STATES",
                },
            ],
        }
    )


@pytest.fixture
def connection(tmp_path):  # type: ignore[no-untyped-def]
    conn = db.connect(tmp_path / "production.db")
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


class TestPublishesNonNewsCandidateAndPersists:
    def test_a_real_ai_tool_candidate_is_published_once_and_recorded(
        self, connection
    ) -> None:  # type: ignore[no-untyped-def]
        _seed_article(
            connection, "official_vendor", "https://vendor.example.invalid/tool-1", "Tool story"
        )
        registry = _registry()
        client = GeminiClient(
            FAKE_KEY, model="gemini-test",
            transport=_gemini_transport(_AI_TOOL_CLASSIFICATION, _GENERATION_PAYLOAD),
        )
        http = HttpClient(transport=_article_http_transport())
        telegram = TelegramClient(TOKEN, transport=_telegram_transport())

        result = publish_one_v2_post_to_production(
            connection,
            client=client,
            registry=registry,
            http=http,
            telegram=telegram,
            target_channel=PRODUCTION_CHANNEL,
            category_preference=(
                EditorialCategory.AI_LIFEHACK,
                EditorialCategory.AI_TOOL,
                EditorialCategory.PROMPT_WORKFLOW,
                EditorialCategory.FREE_DEAL,
                EditorialCategory.EXPLAINER,
            ),
        )

        assert result.outcome.content.category is EditorialCategory.AI_TOOL
        assert len(result.component_outcomes) == 1  # single-message invariant
        assert result.component_outcomes[0].message_id == 777

        draft = DraftRepository(connection).get(result.draft_id)
        assert draft.status is DraftStatus.PUBLISHED

        publication = PublicationRepository(connection).get(result.publication_id)
        assert publication.status.value == "SUCCEEDED"
        assert publication.message_id == 777
        assert publication.channel == PRODUCTION_CHANNEL

    def test_the_review_decision_actor_is_the_distinguishable_manual_actor(
        self, connection
    ) -> None:  # type: ignore[no-untyped-def]
        _seed_article(
            connection, "official_vendor", "https://vendor.example.invalid/tool-2", "Tool story 2"
        )
        registry = _registry()
        client = GeminiClient(
            FAKE_KEY, model="gemini-test",
            transport=_gemini_transport(_AI_TOOL_CLASSIFICATION, _GENERATION_PAYLOAD),
        )
        http = HttpClient(transport=_article_http_transport())
        telegram = TelegramClient(TOKEN, transport=_telegram_transport())

        result = publish_one_v2_post_to_production(
            connection,
            client=client,
            registry=registry,
            http=http,
            telegram=telegram,
            target_channel=PRODUCTION_CHANNEL,
            category_preference=(EditorialCategory.AI_TOOL,),
        )

        drafts = DraftRepository(connection)
        version = drafts.current_version(result.draft_id)
        assert version.created_by == MANUAL_V2_ACTOR


class TestNewsIsHardExcluded:
    def test_a_news_classified_candidate_is_never_published(
        self, connection
    ) -> None:  # type: ignore[no-untyped-def]
        """Real classification is not bound by the source-capability narrowing (it
        can still land on NEWS/RESEARCH) — this proves the post-classification hard
        filter actually blocks publication rather than only narrowing the pool."""
        _seed_article(
            connection, "official_vendor", "https://vendor.example.invalid/news-1", "News story"
        )
        registry = _registry()
        # Classifies NEWS -> generates a NEWS-shaped post -> the hard category filter
        # must reject it before anything is sent, exhausting the (one-article) pool.
        client = GeminiClient(
            FAKE_KEY, model="gemini-test",
            transport=_gemini_transport(_NEWS_CLASSIFICATION, _news_generation_payload()),
        )
        http = HttpClient(transport=_article_http_transport())
        telegram_transport = _telegram_transport()
        telegram = TelegramClient(TOKEN, transport=telegram_transport)

        with pytest.raises(NoEligibleCandidateError):
            publish_one_v2_post_to_production(
                connection,
                client=client,
                registry=registry,
                http=http,
                telegram=telegram,
                target_channel=PRODUCTION_CHANNEL,
                category_preference=(EditorialCategory.AI_TOOL,),
            )

        # Nothing was ever sent, and the NEWS-classified article never got a Draft row.
        assert telegram_transport.calls == []  # type: ignore[attr-defined]
        articles = ArticleRepository(connection)
        news_article = next(
            a for a in articles.list_by_status(ArticleStatus.NORMALIZED, limit=10)
            if a.canonical_url == "https://vendor.example.invalid/news-1"
        )
        assert DraftRepository(connection).find_by_article(news_article.id) is None


class TestNoEligibleCandidate:
    def test_an_empty_pool_raises_and_sends_nothing(self, connection) -> None:  # type: ignore[no-untyped-def]
        registry = _registry()
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_gemini_transport({}))
        http = HttpClient(transport=_article_http_transport())
        telegram_transport = _telegram_transport()
        telegram = TelegramClient(TOKEN, transport=telegram_transport)

        with pytest.raises(NoEligibleCandidateError):
            publish_one_v2_post_to_production(
                connection,
                client=client,
                registry=registry,
                http=http,
                telegram=telegram,
                target_channel=PRODUCTION_CHANNEL,
                category_preference=(EditorialCategory.AI_TOOL,),
            )

        assert telegram_transport.calls == []  # type: ignore[attr-defined]


class TestDedupAgainstExistingProductionDrafts:
    def test_an_article_that_already_has_a_draft_is_never_selected_again(
        self, connection
    ) -> None:  # type: ignore[no-untyped-def]
        _seed_article(
            connection, "official_vendor", "https://vendor.example.invalid/tool-3", "Tool story 3"
        )
        registry = _registry()

        client1 = GeminiClient(
            FAKE_KEY, model="gemini-test",
            transport=_gemini_transport(_AI_TOOL_CLASSIFICATION, _GENERATION_PAYLOAD),
        )
        http = HttpClient(transport=_article_http_transport())
        telegram = TelegramClient(TOKEN, transport=_telegram_transport())

        publish_one_v2_post_to_production(
            connection, client=client1, registry=registry, http=http, telegram=telegram,
            target_channel=PRODUCTION_CHANNEL, category_preference=(EditorialCategory.AI_TOOL,),
        )

        # A second call, same connection: the only article is already drafted, so
        # nothing is eligible.
        client2 = GeminiClient(FAKE_KEY, model="gemini-test", transport=_gemini_transport({}))
        with pytest.raises(NoEligibleCandidateError):
            publish_one_v2_post_to_production(
                connection, client=client2, registry=registry, http=http, telegram=telegram,
                target_channel=PRODUCTION_CHANNEL,
                category_preference=(EditorialCategory.AI_TOOL,),
            )
