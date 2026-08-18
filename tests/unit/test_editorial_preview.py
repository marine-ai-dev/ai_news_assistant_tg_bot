"""editorial.preview.build_preview — Step 3 section 29 (pure logic)."""

from __future__ import annotations

from uuid import uuid4

from ai_news_editor.domain.enums import (
    ContentCapability,
    EditorialCategory,
    EditorialEvidence,
    TrustTier,
)
from ai_news_editor.editorial.diversity import RecentPost
from ai_news_editor.editorial.preview import PreviewCandidate, build_preview
from ai_news_editor.sources.config import SourceDefinition

NEWS = EditorialCategory.NEWS
AI_TOOL = EditorialCategory.AI_TOOL


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
    }
    data.update(overrides)
    return SourceDefinition.model_validate(data)


def _candidate(**overrides: object) -> PreviewCandidate:
    data: dict[str, object] = {
        "article_id": uuid4(),
        "title": "A story",
        "source_id": "openai_blog",
        "editorial_category": NEWS,
        "evidence_type": None,
        "composite_score": 80.0,
    }
    data.update(overrides)
    return PreviewCandidate(**data)  # type: ignore[arg-type]


class TestBuildPreview:
    def test_a_known_source_with_valid_evidence_is_capability_ok(self) -> None:
        candidate = _candidate(evidence_type=EditorialEvidence.PRIMARY_SOURCE)
        sources = {"openai_blog": _source("openai_blog", source_family="OpenAI")}

        rows = build_preview([candidate], sources, recent=[])

        assert len(rows) == 1
        assert rows[0].capability_ok is True
        assert rows[0].capability_reason is None
        assert rows[0].source_family == "OpenAI"
        assert rows[0].trust_tier == TrustTier.OFFICIAL

    def test_a_missing_source_is_shown_not_dropped(self) -> None:
        candidate = _candidate(source_id="unknown_source")
        rows = build_preview([candidate], {}, recent=[])

        assert len(rows) == 1
        assert rows[0].capability_ok is False
        assert "not found in the registry" in rows[0].capability_reason
        assert rows[0].source_family is None

    def test_evidence_the_source_cannot_supply_is_flagged(self) -> None:
        """A Tier C source claiming PRIMARY_SOURCE evidence — the exact rule
        sources.capability exists to enforce, surfaced here for the editor to see."""
        candidate = _candidate(
            source_id="hn",
            editorial_category=AI_TOOL,
            evidence_type=EditorialEvidence.PRIMARY_SOURCE,
        )
        sources = {
            "hn": _source(
                "hn",
                trust_tier=TrustTier.COMMUNITY_SIGNAL,
                priority="COMMUNITY",
                signal_only=True,
                content_types=(ContentCapability.AI_TOOL,),
            )
        }

        rows = build_preview([candidate], sources, recent=[])

        assert rows[0].capability_ok is False
        assert "cannot supply" in rows[0].capability_reason

    def test_no_evidence_type_is_always_capability_ok(self) -> None:
        """An evaluation made before Step 3's classification existed carries no
        evidence_type — the preview should not flag that as a capability failure."""
        candidate = _candidate(evidence_type=None)
        sources = {"openai_blog": _source("openai_blog")}

        rows = build_preview([candidate], sources, recent=[])

        assert rows[0].capability_ok is True

    def test_rows_are_ordered_by_diversity_adjusted_score(self) -> None:
        repeated = _candidate(source_id="openai_blog", composite_score=80.0)
        fresh = _candidate(
            source_id="anthropic_blog", editorial_category=AI_TOOL, composite_score=80.0
        )
        sources = {
            "openai_blog": _source("openai_blog", source_family="OpenAI"),
            "anthropic_blog": _source("anthropic_blog", source_family="Anthropic"),
        }
        recent = [RecentPost(NEWS, "OpenAI")] * 3

        rows = build_preview([repeated, fresh], sources, recent)

        assert rows[0].candidate.source_id == "anthropic_blog"
        assert rows[0].diversity_adjustment == 0.0
        assert rows[1].diversity_adjustment < 0.0

    def test_an_empty_candidate_list_returns_an_empty_preview(self) -> None:
        assert build_preview([], {}, recent=[]) == []
