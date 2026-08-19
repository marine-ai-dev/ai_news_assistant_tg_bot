"""editorial.primary_source — Step 3 section 6."""

from __future__ import annotations

from uuid import uuid4

from ai_news_editor.domain.enums import TrustTier
from ai_news_editor.domain.models import Article
from ai_news_editor.editorial.primary_source import (
    pick_primary,
    prefer_primary_sources,
    story_groups,
)

_TIERS = {
    "openai_blog": TrustTier.OFFICIAL,
    "techcrunch": TrustTier.REPUTABLE_SECONDARY,
    "hackernews": TrustTier.COMMUNITY_SIGNAL,
}


def _tier_of(article: Article) -> TrustTier:
    return _TIERS[article.source_id]


def _article(source_id: str, **overrides: object) -> Article:
    data: dict[str, object] = {
        "raw_item_id": uuid4(),
        "source_id": source_id,
        "title": f"{source_id} reports the story",
        "canonical_url": f"https://example.invalid/{source_id}",
    }
    data.update(overrides)
    return Article.model_validate(data)


class TestStoryGroups:
    def test_unlinked_articles_are_each_their_own_group(self) -> None:
        a, b = _article("openai_blog"), _article("techcrunch")
        groups = story_groups([a, b])
        assert sorted(len(g) for g in groups) == [1, 1]

    def test_a_possible_duplicate_link_groups_two_articles_together(self) -> None:
        primary = _article("openai_blog")
        secondary = _article("techcrunch", possible_duplicate_of_id=primary.id)
        groups = story_groups([primary, secondary])
        assert len(groups) == 1
        assert {a.id for a in groups[0]} == {primary.id, secondary.id}

    def test_two_articles_linked_to_a_common_third_share_one_group(self) -> None:
        primary = _article("openai_blog")
        secondary = _article("techcrunch", possible_duplicate_of_id=primary.id)
        tertiary = _article("hackernews", possible_duplicate_of_id=primary.id)
        groups = story_groups([primary, secondary, tertiary])
        assert len(groups) == 1
        assert len(groups[0]) == 3

    def test_a_dangling_link_to_an_article_not_in_the_pool_is_ignored(self) -> None:
        lone = _article("openai_blog", possible_duplicate_of_id=uuid4())
        groups = story_groups([lone])
        assert groups == [[lone]]


class TestPickPrimary:
    def test_the_official_tier_article_is_preferred_over_reputable_secondary(self) -> None:
        official = _article("openai_blog")
        secondary = _article("techcrunch")
        assert pick_primary([secondary, official], _tier_of) is official

    def test_a_tie_keeps_the_first_seen_article(self) -> None:
        first = _article("openai_blog")
        second = _article("openai_blog")
        assert pick_primary([first, second], _tier_of) is first

    def test_a_singleton_group_returns_its_only_article(self) -> None:
        only = _article("hackernews")
        assert pick_primary([only], _tier_of) is only


class TestPreferPrimarySources:
    def test_every_article_in_a_group_maps_to_the_same_preferred_article(self) -> None:
        official = _article("openai_blog")
        secondary = _article("techcrunch", possible_duplicate_of_id=official.id)
        community = _article("hackernews", possible_duplicate_of_id=official.id)

        preferred = prefer_primary_sources([official, secondary, community], _tier_of)

        assert preferred[official.id] is official
        assert preferred[secondary.id] is official
        assert preferred[community.id] is official

    def test_an_unlinked_lower_tier_article_still_prefers_itself(self) -> None:
        """No same-story Tier A candidate exists in the pool, so the Tier C article
        is its own preferred pick — this module never invents a better source."""
        lone = _article("hackernews")
        preferred = prefer_primary_sources([lone], _tier_of)
        assert preferred[lone.id] is lone

    def test_every_input_article_id_is_present_as_a_key(self) -> None:
        a, b, c = _article("openai_blog"), _article("techcrunch"), _article("hackernews")
        preferred = prefer_primary_sources([a, b, c], _tier_of)
        assert {a.id, b.id, c.id} <= preferred.keys()
