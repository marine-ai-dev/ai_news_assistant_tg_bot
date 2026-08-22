"""automation.pipeline_v2 — Step 5 sections 24-32: the wired v2 pipeline.

Mocked Gemini transport only (a single shared handler answers classification then
generation, in that order) — never a real network or Telegram call.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from ai_news_editor.automation.gemini import GeminiClient
from ai_news_editor.automation.pipeline_v2 import (
    ArticleContext,
    OrchestrationRejected,
    run_pipeline_v2,
    select_top_candidate,
)
from ai_news_editor.domain.enums import (
    ContentCapability,
    EditorialCategory,
    EditorialEvidence,
    TrustTier,
)
from ai_news_editor.editorial.diversity import RecentPost
from ai_news_editor.media.models import DiscoveryMethod
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.sources.config import SourceDefinition
from ai_news_editor.sources.http import HttpClient

FAKE_KEY = "fake-test-key"


def _no_op_media_http() -> HttpClient:
    """No Google source and no Commons match in these tests, so this transport is
    never expected to be asked for anything relevant — it exists only so the media
    strategy's own network calls (layers A/B) have somewhere safe and offline to land,
    never a real request during a unit test."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "commons.wikimedia.org" in str(request.url):
            return httpx.Response(200, json={"query": {"pages": {}}})
        return httpx.Response(404)

    return HttpClient(transport=httpx.MockTransport(handler))


_CLASSIFICATION_PAYLOAD = {
    "content_type": "NEWS",
    "evidence_type": "PRIMARY_SOURCE",
    "reason": "Офіційне оголошення.",
    "rejection_reason": None,
    "is_ai_primary": True,
}

_GENERATION_PAYLOAD = {
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
    "confidence": 90,
    "rejection_reason": None,
}


def _sequenced_transport(*payloads: dict[str, Any]) -> httpx.MockTransport:
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


def _source(source_id: str, **overrides: object) -> SourceDefinition:
    data: dict[str, object] = {
        "id": source_id,
        "name": source_id,
        "adapter": "rss",
        "url": f"https://example.invalid/{source_id}.xml",
        "trust_tier": TrustTier.OFFICIAL,
        "editorial_role": "test",
        "priority": "PRIMARY_NORMAL",
        "content_types": (ContentCapability.NEWS,),
        "publisher_region": "UNITED_STATES",
    }
    data.update(overrides)
    return SourceDefinition.model_validate(data)


def _candidate(**overrides: object) -> ArticleContext:
    data: dict[str, object] = {
        "article_id": uuid4(),
        "title": "Google представила новий інструмент",
        "source_id": "openai_blog",
        "editorial_category": None,
        "evidence_type": None,
        "composite_score": 80.0,
        "article_text": "Повний текст статті про новий інструмент Google.",
        "source_url": "https://blog.google/example",
        "source_label": "Google",
    }
    data.update(overrides)
    return ArticleContext(**data)  # type: ignore[arg-type]


class TestSelectTopCandidate:
    def test_picks_the_only_capable_candidate(self) -> None:
        candidate = _candidate()
        sources = {"openai_blog": _source("openai_blog")}
        top = select_top_candidate([candidate], sources, recent=[])
        assert top is candidate

    def test_returns_none_when_no_candidate_is_capable(self) -> None:
        candidate = _candidate(source_id="missing_source")
        top = select_top_candidate([candidate], {}, recent=[])
        assert top is None

    def test_diversity_prefers_the_non_repeating_candidate(self) -> None:
        repeated = _candidate(
            source_id="openai_blog",
            editorial_category=EditorialCategory.NEWS,
            evidence_type=EditorialEvidence.PRIMARY_SOURCE,
        )
        fresh = _candidate(
            source_id="anthropic_blog",
            editorial_category=EditorialCategory.AI_TOOL,
            evidence_type=EditorialEvidence.PRIMARY_SOURCE,
            composite_score=80.0,
        )
        sources = {
            "openai_blog": _source("openai_blog", source_family="OpenAI"),
            "anthropic_blog": _source(
                "anthropic_blog",
                source_family="Anthropic",
                content_types=(ContentCapability.AI_TOOL,),
            ),
        }
        recent = [RecentPost(EditorialCategory.NEWS, "OpenAI")] * 3
        top = select_top_candidate([repeated, fresh], sources, recent)
        assert top is fresh


class TestRunPipelineV2GeminiCallBudget:
    def test_an_already_classified_candidate_spends_only_one_gemini_call(
        self, tmp_path
    ) -> None:
        """Section 27: known category/evidence must skip classification entirely."""
        candidate = _candidate(
            editorial_category=EditorialCategory.NEWS,
            evidence_type=EditorialEvidence.PRIMARY_SOURCE,
        )
        sources = {"openai_blog": _source("openai_blog")}
        transport = _sequenced_transport(_GENERATION_PAYLOAD)
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=transport)

        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = run_pipeline_v2(
                client=client,
                candidates=[candidate],
                sources_by_id=sources,
                recent=[],
                workspace=workspace,
                http=_no_op_media_http(),
            )

        assert outcome.gemini_calls == 1
        assert outcome.classification is None
        assert outcome.content.category is EditorialCategory.NEWS

    def test_an_unclassified_candidate_spends_exactly_two_gemini_calls(self, tmp_path) -> None:
        """Section 27's target: classification + generation, never more."""
        candidate = _candidate()
        sources = {"openai_blog": _source("openai_blog")}
        transport = _sequenced_transport(_CLASSIFICATION_PAYLOAD, _GENERATION_PAYLOAD)
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=transport)

        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = run_pipeline_v2(
                client=client,
                candidates=[candidate],
                sources_by_id=sources,
                recent=[],
                workspace=workspace,
                http=_no_op_media_http(),
            )

        assert outcome.gemini_calls == 2
        assert outcome.classification is not None
        assert outcome.classification.content_type is EditorialCategory.NEWS
        assert len(transport.calls) == 2  # type: ignore[attr-defined]

    def test_no_source_discovered_media_still_succeeds_via_the_branded_card_fallback(
        self, tmp_path
    ) -> None:
        """Step 6B: a NO_MEDIA source no longer means a text-only post — the branded
        card is the universal safe fallback, so media.ok is True here, drawn locally
        rather than discovered from anywhere."""
        candidate = _candidate(
            editorial_category=EditorialCategory.NEWS,
            evidence_type=EditorialEvidence.PRIMARY_SOURCE,
        )
        sources = {"openai_blog": _source("openai_blog")}  # NO_MEDIA by default
        transport = _sequenced_transport(_GENERATION_PAYLOAD)
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=transport)

        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = run_pipeline_v2(
                client=client,
                candidates=[candidate],
                sources_by_id=sources,
                recent=[],
                workspace=workspace,
                http=_no_op_media_http(),
            )

        assert outcome.media.ok is True
        assert outcome.media.media.source_method is DiscoveryMethod.GENERATED_CARD
        assert outcome.rendered.full_text  # text still renders regardless


class TestRejection:
    def test_no_capable_candidate_raises_orchestration_rejected(self, tmp_path) -> None:
        candidate = _candidate(source_id="missing_source")
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_sequenced_transport({}))

        with MediaWorkspace(root=tmp_path) as workspace, pytest.raises(
            OrchestrationRejected, match="capability"
        ):
            run_pipeline_v2(
                client=client,
                candidates=[candidate],
                sources_by_id={},
                recent=[],
                workspace=workspace,
                http=_no_op_media_http(),
            )

    def test_a_generation_rejection_propagates_as_orchestration_rejected(self, tmp_path) -> None:
        candidate = _candidate(
            editorial_category=EditorialCategory.NEWS,
            evidence_type=EditorialEvidence.PRIMARY_SOURCE,
        )
        sources = {"openai_blog": _source("openai_blog")}
        rejection_payload = {**_GENERATION_PAYLOAD}
        rejection_payload.update(
            headline=None, body=[], source_label=None, confidence=None,
            rejection_reason="Недостатньо матеріалу.",
        )
        transport = _sequenced_transport(rejection_payload)
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=transport)

        with MediaWorkspace(root=tmp_path) as workspace, pytest.raises(
            OrchestrationRejected, match="generation failed"
        ):
            run_pipeline_v2(
                client=client,
                candidates=[candidate],
                sources_by_id=sources,
                recent=[],
                workspace=workspace,
                http=_no_op_media_http(),
            )
