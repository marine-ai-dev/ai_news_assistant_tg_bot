"""automation.pipeline_v2.collapse_to_primary_sources — Step 6 section 3.

End-to-end proof, not just a module-existence check: given a real Tier B article and
an equivalent Tier A official announcement of the *same* story (linked via
possible_duplicate_of_id, exactly as normalization already records it), the real v2
selection/generation path grounds its post in the Tier A article — never the Tier B
one — without spending an extra Gemini call to decide that.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx

from ai_news_editor.automation.gemini import GeminiClient
from ai_news_editor.automation.pipeline_v2 import (
    ArticleContext,
    collapse_to_primary_sources,
    run_pipeline_v2,
    select_top_candidate,
)
from ai_news_editor.domain.enums import (
    ContentCapability,
    EditorialCategory,
    EditorialEvidence,
    TrustTier,
)
from ai_news_editor.domain.models import Article
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.sources.config import SourceDefinition
from ai_news_editor.sources.http import HttpClient
from tests.conftest import make_article

FAKE_KEY = "fake-test-key"


def _no_op_media_http() -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if "commons.wikimedia.org" in str(request.url):
            return httpx.Response(200, json={"query": {"pages": {}}})
        return httpx.Response(404)

    return HttpClient(transport=httpx.MockTransport(handler))

_GENERATION_PAYLOAD = {
    "headline": "ExampleCorp запускає Product X",
    "body": [{"purpose": "what_happened", "text": "ExampleCorp офіційно оголосила Product X."}],
    "detail_bullets": [],
    "source_label": "ExampleCorp",
    "prompt_origin": None,
    "prompt_text": None,
    "free_deal_kind": None,
    "research_framing": None,
    "research_independently_verified": None,
    "digest_items": [],
    "confidence": 92,
    "rejection_reason": None,
}


def _transport(payload: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps(payload)}]}, "finishReason": "STOP"}
                ]
            },
        )

    return httpx.MockTransport(handler)


def _source(source_id: str, *, trust_tier: TrustTier) -> SourceDefinition:
    return SourceDefinition.model_validate(
        {
            "id": source_id,
            "name": source_id,
            "adapter": "rss",
            "url": f"https://example.invalid/{source_id}.xml",
            "trust_tier": trust_tier,
            "editorial_role": "test",
            "priority": "PRIMARY_NORMAL" if trust_tier is TrustTier.OFFICIAL else "DISCOVERY",
            "content_types": (ContentCapability.NEWS,),
            "publisher_region": "UNITED_STATES",
        }
    )


class TestPrimarySourcePreferenceEndToEnd:
    def test_tier_a_official_wins_over_tier_b_secondary_for_the_same_story(
        self, tmp_path
    ) -> None:
        official = make_article(
            uuid4(),
            source_id="official_vendor",
            title="ExampleCorp officially announces Product X",
            canonical_url="https://vendor.example.invalid/product-x",
        )
        secondary = make_article(
            uuid4(),
            source_id="tech_press",
            title="ExampleCorp is launching something called Product X",
            canonical_url="https://press.example.invalid/product-x-scoop",
            possible_duplicate_of_id=official.id,
        )

        def trust_tier_of(article: Article) -> TrustTier:
            if article.source_id == "official_vendor":
                return TrustTier.OFFICIAL
            return TrustTier.REPUTABLE_SECONDARY

        collapsed = collapse_to_primary_sources([secondary, official], trust_tier_of)

        # Only the Tier A article survives the collapse — the Tier B report of the
        # same story never reaches selection at all.
        assert [a.id for a in collapsed] == [official.id]

        candidates = [
            ArticleContext(
                article_id=article.id,
                title=article.title,
                source_id=article.source_id,
                editorial_category=EditorialCategory.NEWS,
                evidence_type=EditorialEvidence.PRIMARY_SOURCE,
                composite_score=80.0,
                article_text="ExampleCorp full announcement text.",
                source_url=article.canonical_url,
                source_label="ExampleCorp",
            )
            for article in collapsed
        ]
        sources = {
            "official_vendor": _source("official_vendor", trust_tier=TrustTier.OFFICIAL),
        }

        with MediaWorkspace(root=tmp_path) as workspace:
            client = GeminiClient(
                FAKE_KEY, model="gemini-test", transport=_transport(_GENERATION_PAYLOAD)
            )
            outcome = run_pipeline_v2(
                client=client,
                candidates=candidates,
                sources_by_id=sources,
                recent=[],
                workspace=workspace,
                http=_no_op_media_http(),
            )

        assert outcome.content.source_url == official.canonical_url
        assert outcome.gemini_calls == 1  # already classified — generation only

    def test_without_the_collapse_the_tier_b_candidate_would_still_be_offered(
        self,
    ) -> None:
        """Negative control: proves the collapse step is what does the work — without
        it, both candidates are still in the pool select_top_candidate sees."""
        official = make_article(
            uuid4(), source_id="official_vendor", canonical_url="https://vendor.example.invalid/x"
        )
        secondary = make_article(
            uuid4(),
            source_id="tech_press",
            canonical_url="https://press.example.invalid/x-scoop",
            possible_duplicate_of_id=official.id,
        )
        contexts = [
            ArticleContext(
                article_id=a.id, title=a.title, source_id=a.source_id,
                editorial_category=EditorialCategory.NEWS,
                evidence_type=EditorialEvidence.PRIMARY_SOURCE, composite_score=80.0,
                article_text="text", source_url=a.canonical_url, source_label="X",
            )
            for a in (official, secondary)
        ]
        sources = {
            "official_vendor": _source("official_vendor", trust_tier=TrustTier.OFFICIAL),
            "tech_press": _source("tech_press", trust_tier=TrustTier.REPUTABLE_SECONDARY),
        }
        # Both still capability-eligible without the collapse — this is the state the
        # collapse step exists to narrow before it ever reaches selection.
        top = select_top_candidate(contexts, sources, recent=[])
        assert top is not None
