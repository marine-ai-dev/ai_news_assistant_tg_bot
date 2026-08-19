"""sources.capability — content-type and evidence enforcement, Step 3.

Uses the real shipped config/sources.yaml where it exercises real sources (section 24's
own examples name real sources), and small synthetic SourceDefinition objects for
edge cases the real registry doesn't happen to cover.
"""

from __future__ import annotations

import pytest

from ai_news_editor.domain.enums import (
    ContentCapability,
    EditorialCategory,
    EditorialEvidence,
    TrustTier,
)
from ai_news_editor.sources.capability import (
    CapabilityError,
    allowed_evidence,
    allows_category,
    is_evidence_allowed,
    validate_classification,
)
from ai_news_editor.sources.config import DEFAULT_CONFIG_PATH, SourceDefinition, load_sources_config


@pytest.fixture(scope="module")
def registry():  # type: ignore[no-untyped-def]
    return load_sources_config(DEFAULT_CONFIG_PATH)


def _source(**overrides: object) -> SourceDefinition:
    data: dict[str, object] = {
        "id": "test_source",
        "name": "Test Source",
        "adapter": "rss",
        "url": "https://example.invalid/feed.xml",
        "trust_tier": TrustTier.OFFICIAL,
        "editorial_role": "A source for capability tests.",
        "priority": "PRIMARY_NORMAL",
        "content_types": (ContentCapability.NEWS,),
        "publisher_region": "UNITED_STATES",
    }
    data.update(overrides)
    return SourceDefinition.model_validate(data)


class TestRealRegistryExamples:
    """The exact examples from Step 3 section 24, against the real shipped registry."""

    def test_reuters_news_is_allowed(self, registry) -> None:  # type: ignore[no-untyped-def]
        reuters = registry.get("reuters_technology")
        assert allows_category(reuters, EditorialCategory.NEWS)
        assert is_evidence_allowed(reuters, EditorialEvidence.REPUTABLE_SECONDARY)

    def test_hacker_news_ai_lifehack_is_allowed(self, registry) -> None:  # type: ignore[no-untyped-def]
        hn = registry.get("hackernews")
        assert allows_category(hn, EditorialCategory.AI_LIFEHACK)
        assert is_evidence_allowed(hn, EditorialEvidence.USER_REPORTED)
        assert is_evidence_allowed(hn, EditorialEvidence.COMMUNITY_DISCUSSION)

    def test_hacker_news_cannot_be_primary_factual_news(self, registry) -> None:  # type: ignore[no-untyped-def]
        """The rule this module exists for: a Tier C source cannot masquerade as a
        primary factual NEWS source, at either the capability or the evidence layer."""
        hn = registry.get("hackernews")
        assert not allows_category(hn, EditorialCategory.NEWS)
        assert not is_evidence_allowed(hn, EditorialEvidence.PRIMARY_SOURCE)
        with pytest.raises(CapabilityError):
            validate_classification(
                hn, category=EditorialCategory.NEWS, evidence=EditorialEvidence.PRIMARY_SOURCE
            )

    def test_google_research_research_is_allowed(self, registry) -> None:  # type: ignore[no-untyped-def]
        google_research = registry.get("google_research")
        assert allows_category(google_research, EditorialCategory.RESEARCH)
        assert is_evidence_allowed(google_research, EditorialEvidence.PRIMARY_SOURCE)
        assert is_evidence_allowed(google_research, EditorialEvidence.RESEARCH_PAPER)

    def test_product_hunt_ai_tool_is_allowed_with_community_evidence_only(
        self, registry  # type: ignore[no-untyped-def]
    ) -> None:
        product_hunt = registry.get("producthunt_ai")
        assert allows_category(product_hunt, EditorialCategory.AI_TOOL)
        assert is_evidence_allowed(product_hunt, EditorialEvidence.COMMUNITY_DISCUSSION)
        assert is_evidence_allowed(product_hunt, EditorialEvidence.USER_REPORTED)
        assert not is_evidence_allowed(product_hunt, EditorialEvidence.PRIMARY_SOURCE)


class TestEvidenceByTier:
    def test_official_tier_may_use_primary_source(self) -> None:
        source = _source(trust_tier=TrustTier.OFFICIAL)
        assert EditorialEvidence.PRIMARY_SOURCE in allowed_evidence(source)

    def test_official_tier_may_use_research_paper(self) -> None:
        source = _source(trust_tier=TrustTier.OFFICIAL)
        assert EditorialEvidence.RESEARCH_PAPER in allowed_evidence(source)

    def test_reputable_secondary_tier_is_limited_to_its_own_evidence(self) -> None:
        source = _source(trust_tier=TrustTier.REPUTABLE_SECONDARY, priority="DISCOVERY")
        assert allowed_evidence(source) == (EditorialEvidence.REPUTABLE_SECONDARY,)

    def test_community_tier_never_allows_primary_source(self) -> None:
        source = _source(
            trust_tier=TrustTier.COMMUNITY_SIGNAL, priority="COMMUNITY", signal_only=True
        )
        assert EditorialEvidence.PRIMARY_SOURCE not in allowed_evidence(source)
        assert EditorialEvidence.RESEARCH_PAPER not in allowed_evidence(source)

    def test_community_tier_allows_user_reported_and_community_discussion(self) -> None:
        source = _source(
            trust_tier=TrustTier.COMMUNITY_SIGNAL, priority="COMMUNITY", signal_only=True
        )
        assert set(allowed_evidence(source)) == {
            EditorialEvidence.USER_REPORTED,
            EditorialEvidence.COMMUNITY_DISCUSSION,
        }


class TestCategoryCapability:
    def test_a_source_without_the_capability_is_rejected(self) -> None:
        source = _source(content_types=(ContentCapability.RESEARCH,))
        assert not allows_category(source, EditorialCategory.NEWS)

    def test_a_source_with_the_capability_is_allowed(self) -> None:
        source = _source(content_types=(ContentCapability.NEWS, ContentCapability.AI_TOOL))
        assert allows_category(source, EditorialCategory.AI_TOOL)

    def test_weekly_digest_maps_to_the_input_capability(self) -> None:
        source = _source(content_types=(ContentCapability.WEEKLY_DIGEST_INPUT,))
        assert allows_category(source, EditorialCategory.WEEKLY_DIGEST)


class TestValidateClassification:
    def test_a_fully_valid_pairing_raises_nothing(self) -> None:
        source = _source(content_types=(ContentCapability.NEWS,), trust_tier=TrustTier.OFFICIAL)
        validate_classification(
            source, category=EditorialCategory.NEWS, evidence=EditorialEvidence.PRIMARY_SOURCE
        )

    def test_wrong_category_capability_is_rejected(self) -> None:
        source = _source(content_types=(ContentCapability.RESEARCH,), trust_tier=TrustTier.OFFICIAL)
        with pytest.raises(CapabilityError, match="does not declare"):
            validate_classification(
                source, category=EditorialCategory.NEWS,
                evidence=EditorialEvidence.PRIMARY_SOURCE,
            )

    def test_wrong_evidence_for_tier_is_rejected(self) -> None:
        source = _source(
            content_types=(ContentCapability.NEWS,),
            trust_tier=TrustTier.REPUTABLE_SECONDARY,
            priority="DISCOVERY",
        )
        with pytest.raises(CapabilityError, match="cannot supply"):
            validate_classification(
                source, category=EditorialCategory.NEWS,
                evidence=EditorialEvidence.PRIMARY_SOURCE,
            )
