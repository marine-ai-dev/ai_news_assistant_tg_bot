"""sources.geography — Step 6B: the source-origin allowlist.

Small synthetic SourceDefinition objects per region — the same style as
test_source_capability.py's own edge-case fixtures — plus a check against the real
shipped registry, since every enabled source there must already be eligible.
"""

from __future__ import annotations

import pytest

from ai_news_editor.domain.enums import ContentCapability, PublisherRegion, TrustTier
from ai_news_editor.sources.config import DEFAULT_CONFIG_PATH, SourceDefinition, load_sources_config
from ai_news_editor.sources.geography import (
    ALLOWED_REGIONS,
    EXPLICITLY_FORBIDDEN_REGIONS,
    GeographyError,
    is_source_eligible,
    validate_source_geography,
)


def _source(region: PublisherRegion) -> SourceDefinition:
    return SourceDefinition.model_validate(
        {
            "id": "test_source",
            "name": "Test Source",
            "adapter": "rss",
            "url": "https://example.invalid/feed.xml",
            "trust_tier": TrustTier.OFFICIAL,
            "editorial_role": "A source for geography tests.",
            "priority": "PRIMARY_NORMAL",
            "content_types": (ContentCapability.NEWS,),
            "publisher_region": region,
        }
    )


class TestAllowlistMembership:
    @pytest.mark.parametrize(
        "region",
        [
            PublisherRegion.UKRAINE,
            PublisherRegion.EUROPE,
            PublisherRegion.UNITED_KINGDOM,
            PublisherRegion.UNITED_STATES,
        ],
    )
    def test_the_four_eligible_regions_pass(self, region: PublisherRegion) -> None:
        assert is_source_eligible(_source(region)) is True
        validate_source_geography(_source(region))  # raises nothing

    @pytest.mark.parametrize(
        "region", [PublisherRegion.RUSSIA, PublisherRegion.BELARUS, PublisherRegion.IRAN]
    )
    def test_the_three_explicitly_forbidden_regions_fail(self, region: PublisherRegion) -> None:
        assert is_source_eligible(_source(region)) is False
        with pytest.raises(GeographyError, match=region.value):
            validate_source_geography(_source(region))

    def test_a_reviewed_but_out_of_scope_origin_fails(self) -> None:
        """OTHER is a real, known origin (e.g. Canada) — still ineligible, just not
        one of the three explicitly named forbidden ones."""
        assert is_source_eligible(_source(PublisherRegion.OTHER)) is False
        with pytest.raises(GeographyError, match="not on the channel's geography allowlist"):
            validate_source_geography(_source(PublisherRegion.OTHER))

    def test_an_unreviewed_origin_fails_closed(self) -> None:
        """The allowlist has no "assume yes" path — UNKNOWN is ineligible until a
        human explicitly classifies the source."""
        assert is_source_eligible(_source(PublisherRegion.UNKNOWN)) is False
        with pytest.raises(GeographyError, match="unknown origin fails closed"):
            validate_source_geography(_source(PublisherRegion.UNKNOWN))

    def test_the_allowlist_is_exactly_the_four_specified_regions(self) -> None:
        assert {
            PublisherRegion.UKRAINE,
            PublisherRegion.EUROPE,
            PublisherRegion.UNITED_KINGDOM,
            PublisherRegion.UNITED_STATES,
        } == ALLOWED_REGIONS

    def test_the_forbidden_set_is_exactly_the_three_named_countries(self) -> None:
        assert {
            PublisherRegion.RUSSIA,
            PublisherRegion.BELARUS,
            PublisherRegion.IRAN,
        } == EXPLICITLY_FORBIDDEN_REGIONS

    def test_allowed_and_forbidden_never_overlap(self) -> None:
        assert ALLOWED_REGIONS.isdisjoint(EXPLICITLY_FORBIDDEN_REGIONS)


class TestSourceOriginNotArticleSubject:
    """The rule is about where the source is published, never what an article is
    about — a US/EU/UK/UA outlet reporting on Russia is still fine."""

    def test_an_eligible_source_is_not_affected_by_forbidden_countries_appearing_in_its_name(
        self,
    ) -> None:
        source = SourceDefinition.model_validate(
            {
                "id": "us_outlet_covering_russia",
                "name": "US Outlet — Russia Sanctions Desk",
                "adapter": "rss",
                "url": "https://example.invalid/russia-desk.xml",
                "trust_tier": TrustTier.OFFICIAL,
                "editorial_role": "US-based reporting on Russia/Belarus/Iran affairs.",
                "priority": "PRIMARY_NORMAL",
                "content_types": (ContentCapability.NEWS,),
                "publisher_region": PublisherRegion.UNITED_STATES,
            }
        )
        assert is_source_eligible(source) is True


class TestRealRegistryIsCompliant:
    def test_every_enabled_source_in_the_real_registry_is_geography_eligible(self) -> None:
        config = load_sources_config(DEFAULT_CONFIG_PATH)
        ineligible = [d.id for d in config.enabled() if not is_source_eligible(d)]
        assert ineligible == []

    def test_every_source_in_the_real_registry_has_a_reviewed_region(self) -> None:
        """Not UNKNOWN for any source, enabled or disabled — every one has actually
        been reviewed, even the ones sitting disabled."""
        config = load_sources_config(DEFAULT_CONFIG_PATH)
        unreviewed = [d.id for d in config.sources if d.publisher_region is PublisherRegion.UNKNOWN]
        assert unreviewed == []
