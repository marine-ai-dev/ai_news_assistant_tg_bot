"""automation.generation_v2 — Step 5 section 24: structured category-aware generation.

Standalone mock-transport harness, matching test_editorial_classification.py's own
pattern — this call is not wired into the live pipeline either, so it needs none of
test_automation.py's heavier fixtures.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ai_news_editor.automation.gemini import GeminiClient
from ai_news_editor.automation.generation_v2 import (
    GenerationV2Invalid,
    GenerationV2Rejected,
    build_editorial_content,
    generate_editorial_content,
)
from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence

FAKE_KEY = "fake-test-key"


def _transport(payload: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": json.dumps(payload)}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    return httpx.MockTransport(handler)


_NEWS_PAYLOAD = {
    "headline": "Google представила новий інструмент",
    "body": [{"purpose": "what_happened", "text": "Google випустила нову функцію."}],
    "detail_bullets": [],
    "source_label": "Google",
    "prompt_origin": None,
    "prompt_text": None,
    "free_deal_kind": None,
    "research_framing": None,
    "research_independently_verified": None,
    "digest_items": [],
    "confidence": 92,
    "rejection_reason": None,
}


class TestGenerateEditorialContent:
    def test_a_valid_response_is_returned(self) -> None:
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(_NEWS_PAYLOAD))
        result = generate_editorial_content(
            client,
            category=EditorialCategory.NEWS,
            article_text="Повний текст статті.",
            source_label="Google",
        )
        assert result.headline == "Google представила новий інструмент"
        assert result.body[0].purpose == "what_happened"
        assert result.confidence == 92

    def test_a_rejection_raises_generation_v2_rejected(self) -> None:
        payload = dict(_NEWS_PAYLOAD)
        payload.update(
            headline=None, body=[], source_label=None, confidence=None,
            rejection_reason="Матеріалу недостатньо для повноцінного посту.",
        )
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))
        with pytest.raises(GenerationV2Rejected, match="Матеріалу недостатньо"):
            generate_editorial_content(
                client, category=EditorialCategory.NEWS, article_text="x", source_label="Google"
            )

    def test_an_invented_body_purpose_is_rejected_by_the_schema(self) -> None:
        payload = dict(_NEWS_PAYLOAD)
        payload["body"] = [{"purpose": "totally_made_up", "text": "..."}]
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))
        with pytest.raises(GenerationV2Invalid):
            generate_editorial_content(
                client, category=EditorialCategory.NEWS, article_text="x", source_label="Google"
            )

    def test_malformed_json_is_generation_v2_invalid(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": "not json"}]}, "finishReason": "STOP"}
                    ]
                },
            )

        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=httpx.MockTransport(handler))
        with pytest.raises(GenerationV2Invalid, match="unusable"):
            generate_editorial_content(
                client, category=EditorialCategory.NEWS, article_text="x", source_label="Google"
            )

    def test_a_post_missing_required_fields_is_invalid_not_a_silent_partial(self) -> None:
        payload = dict(_NEWS_PAYLOAD)
        payload["headline"] = None  # body/source_label present, headline missing
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))
        with pytest.raises(GenerationV2Invalid):
            generate_editorial_content(
                client, category=EditorialCategory.NEWS, article_text="x", source_label="Google"
            )


class TestBuildEditorialContent:
    def test_source_url_always_comes_from_the_caller_never_gemini(self) -> None:
        """Gemini's schema does not even carry a source_url field — this proves the
        final EditorialContent's source_url is exactly what the caller supplied."""
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(_NEWS_PAYLOAD))
        generated = generate_editorial_content(
            client, category=EditorialCategory.NEWS, article_text="x", source_label="Google"
        )
        content = build_editorial_content(
            generated,
            category=EditorialCategory.NEWS,
            evidence=EditorialEvidence.PRIMARY_SOURCE,
            source_url="https://blog.google/our-own-record",
        )
        assert content.source_url == "https://blog.google/our-own-record"
        assert content.category is EditorialCategory.NEWS
        assert content.evidence is EditorialEvidence.PRIMARY_SOURCE
