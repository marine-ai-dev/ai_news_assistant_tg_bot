"""Rule-based prefilter.

Two halves matter equally: obvious junk is screened out with a named reason, and
anything that could plausibly be a story survives. The second half is the one that
protects the product — the prefilter must never pre-empt editorial judgement.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import PrefilterReason
from ai_news_editor.domain.models import Article
from ai_news_editor.editorial.prefilter import RULES, STALE_AFTER, rule_by_id, screen


def article(
    title: str, text: str | None = "Some body text about the announcement.", **kw: object
) -> Article:
    data: dict[str, object] = {
        "raw_item_id": uuid4(),
        "source_id": "alpha",
        "title": title,
        "canonical_url": "https://alpha.invalid/a",
        "clean_text": text,
    }
    data.update(kw)
    return Article.model_validate(data)


class TestRejections:
    def test_empty_title_and_body(self) -> None:
        verdict = screen(article("hi", text=None))
        assert verdict.screened_out
        assert verdict.reason is PrefilterReason.EMPTY_CONTENT

    @pytest.mark.parametrize(
        "title",
        [
            "Archives",
            "Page 2",
            "Privacy Policy",
            "Terms of Service",
            "Newsletter",
            "Older posts",
            "Untitled",
            "Sitemap",
        ],
    )
    def test_navigation_and_boilerplate(self, title: str) -> None:
        verdict = screen(article(title, text=None))
        assert verdict.screened_out
        assert verdict.reason in (PrefilterReason.BOILERPLATE, PrefilterReason.EMPTY_CONTENT)

    @pytest.mark.parametrize(
        "title",
        [
            "We're hiring a research scientist",
            "Join our team in London",
            "Careers at ExampleCorp",
            "Open roles on the AI team",
        ],
    )
    def test_job_listings(self, title: str) -> None:
        verdict = screen(article(title))
        assert verdict.screened_out
        assert verdict.reason is PrefilterReason.JOB_LISTING

    @pytest.mark.parametrize(
        "title",
        [
            "ExampleCorp Announces Second-Quarter Results",
            "Q3 2026 earnings call scheduled",
            "Board declares quarterly cash dividend",
            "Annual report now available",
            "Form 10-K filed with the SEC",
        ],
    )
    def test_investor_and_legal_notices(self, title: str) -> None:
        verdict = screen(article(title))
        assert verdict.screened_out
        assert verdict.reason is PrefilterReason.LEGAL_OR_INVESTOR_NOTICE

    def test_very_old_items(self) -> None:
        old = now_utc() - STALE_AFTER - timedelta(days=10)
        verdict = screen(article("A genuinely old announcement", published_at=old))
        assert verdict.screened_out
        assert verdict.reason is PrefilterReason.STALE_ITEM

    def test_every_rejection_names_a_rule(self) -> None:
        verdict = screen(article("Careers at ExampleCorp"))
        assert verdict.rule_id is not None
        assert rule_by_id(verdict.rule_id).reason is verdict.reason


class TestSurvivors:
    """Anything that could plausibly be a story must reach Phase 4."""

    @pytest.mark.parametrize(
        "title",
        [
            "ChatGPT can now edit your photos directly in the app",
            "Canva adds AI video generation for free users",
            "Gemini gets a new voice mode for everyday questions",
            "Notion AI can now summarise your meeting notes",
            "Microsoft Copilot arrives in Excel for all users",
            "Someone made a deepfake of a bank CEO and stole millions",
            "An AI chatbot invented a refund policy and the airline had to honour it",
            "Suno's new model writes songs that sound uncannily human",
        ],
    )
    def test_consumer_ai_stories_are_kept(self, title: str) -> None:
        assert screen(article(title)).keep

    @pytest.mark.parametrize(
        "title",
        [
            "Introducing a new sparse attention kernel for faster inference",
            "We trained a 400B parameter mixture-of-experts model",
            "A new quantisation method cuts memory use by 60 percent",
            "Announcing CUDA 14 support in our inference runtime",
            "Scaling laws for multimodal pretraining revisited",
        ],
    )
    def test_technical_stories_are_never_screened_out_here(self, title: str) -> None:
        """A deeply technical release can matter enormously to ordinary users.

        Whether it is *interesting* is the LLM editor's call in Phase 4. Filtering on
        technicality here would throw the story away before anything could judge it.
        """
        assert screen(article(title)).keep

    def test_a_title_only_changelog_entry_is_kept(self) -> None:
        """Changelog entries often have a meaningful title and no body at all."""
        assert screen(
            article("Share context with Custom Agents from the Share menu", text=None)
        ).keep

    def test_missing_publication_date_is_not_treated_as_stale(self) -> None:
        assert screen(article("A story with no date", published_at=None)).keep

    def test_recent_items_are_kept(self) -> None:
        assert screen(article("Recent news", published_at=now_utc() - timedelta(days=3))).keep

    def test_a_word_like_career_inside_a_real_story_is_kept(self) -> None:
        assert screen(article("How AI is changing career advice for students")).keep

    def test_ukrainian_titles_are_kept(self) -> None:
        assert screen(article("Новий інструмент ШІ для створення відео")).keep


class TestDeterminism:
    def test_same_article_always_gets_the_same_verdict(self) -> None:
        subject = article("Careers at ExampleCorp")
        assert [screen(subject).reason for _ in range(5)] == [PrefilterReason.JOB_LISTING] * 5

    def test_rule_ids_are_unique(self) -> None:
        ids = [rule.id for rule in RULES]
        assert len(ids) == len(set(ids))

    def test_every_rule_is_documented(self) -> None:
        assert all(rule.description.strip() for rule in RULES)

    def test_unknown_rule_lookup_raises(self) -> None:
        with pytest.raises(KeyError):
            rule_by_id("rule.does_not_exist")

    def test_rules_are_conservative_in_number(self) -> None:
        """A sprawling blocklist would quietly become the real editor."""
        assert len(RULES) <= 8


class TestOrdering:
    def test_the_most_specific_reason_is_recorded(self) -> None:
        """An empty job posting reports EMPTY_CONTENT, the first rule that applies."""
        verdict = screen(article("hi", text=None, published_at=datetime(2000, 1, 1, tzinfo=UTC)))
        assert verdict.reason is PrefilterReason.EMPTY_CONTENT
