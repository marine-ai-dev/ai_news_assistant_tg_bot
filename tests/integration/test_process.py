"""The processing pipeline end to end against a temporary database."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from ai_news_editor.domain.enums import (
    ArticleStatus,
    DuplicateReason,
    PrefilterReason,
    SourceKind,
    TrustTier,
)
from ai_news_editor.pipeline.process import pipeline_stats, process
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    CommunitySignalRepository,
    RawItemRepository,
    SourceRepository,
)
from tests.conftest import make_raw_item, make_source

LONG = (
    "OpenAI launches a new ChatGPT feature that lets ordinary users build custom agents "
    "without writing any code at all"
)
OTHER = (
    "Midjourney releases version eight with dramatically better photorealistic human "
    "faces and correctly rendered hands in every scene"
)
WHEN = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def seeded(sources: SourceRepository) -> None:
    sources.upsert(make_source("official", trust_tier=TrustTier.OFFICIAL))
    sources.upsert(make_source("media", trust_tier=TrustTier.REPUTABLE_SECONDARY))
    sources.upsert(
        make_source(
            "hackernews",
            kind=SourceKind.HN_SIGNAL,
            trust_tier=TrustTier.COMMUNITY_SIGNAL,
            signal_only=True,
        )
    )


def add_item(raw_items: RawItemRepository, source_id: str = "official", **kw: object) -> None:
    defaults: dict[str, object] = {
        "title_original": "A headline about an AI product update",
        "summary_raw": LONG,
        "published_at": WHEN,
    }
    defaults.update(kw)
    raw_items.add(make_raw_item(source_id, **defaults))


class TestNormalizationStage:
    def test_creates_articles_from_raw_items(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/a")
        add_item(
            raw_items,
            url_original="https://official.invalid/b",
            title_original="Another headline entirely",
            summary_raw=OTHER,
        )

        report = process(connection)
        assert report.normalized == 2
        assert report.ready == 2

    def test_raw_items_are_left_untouched(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        """RawItem is provenance and stays exactly as ingested."""
        add_item(raw_items, url_original="https://official.invalid/a", title_original="  Padded  ")
        before = raw_items.list_by_source("official")[0]
        process(connection)
        after = raw_items.list_by_source("official")[0]
        assert after.title_original == before.title_original == "  Padded  "

    def test_articles_link_back_to_their_raw_item(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/a")
        process(connection)
        article = ArticleRepository(connection).list_by_status(ArticleStatus.NORMALIZED)[0]
        assert raw_items.get(article.raw_item_id).source_id == "official"

    def test_unnormalizable_items_are_reported_not_dropped_silently(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        raw_items.add(
            make_raw_item("official", title_original=None, url_original="https://x.invalid/a")
        )
        report = process(connection)
        assert report.rejected == 1
        assert report.rejections


class TestIdempotency:
    def test_a_second_run_creates_nothing(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/a")
        add_item(
            raw_items,
            url_original="https://official.invalid/b",
            title_original="Second headline here",
            summary_raw=OTHER,
        )

        first = process(connection)
        second = process(connection)

        assert first.normalized == 2
        assert second.considered == 0
        assert second.normalized == 0
        assert ArticleRepository(connection).count_by_status()[ArticleStatus.NORMALIZED.value] == 2

    def test_repeated_runs_stay_stable(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/a")
        for _ in range(4):
            process(connection)
        assert sum(ArticleRepository(connection).count_by_status().values()) == 1

    def test_new_items_are_picked_up_later(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/a")
        process(connection)
        add_item(
            raw_items,
            url_original="https://official.invalid/b",
            title_original="Later headline arrives",
            summary_raw=OTHER,
        )
        report = process(connection)
        assert report.normalized == 1

    def test_limit_is_honoured_and_the_rest_resume(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        for i in range(5):
            add_item(
                raw_items,
                url_original=f"https://official.invalid/{i}",
                title_original=f"Headline number {i} about AI",
                summary_raw=f"Distinct body number {i}: {OTHER if i % 2 else LONG} variant {i}",
            )
        assert process(connection, limit=2).normalized == 2
        assert process(connection).normalized == 3


class TestDuplicateDetection:
    def test_same_canonical_url_is_marked_duplicate(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/a")
        add_item(raw_items, url_original="https://official.invalid/a?utm_source=rss")

        report = process(connection)
        assert report.exact_duplicates == 1

        repo = ArticleRepository(connection)
        duplicate = repo.list_by_status(ArticleStatus.DUPLICATE)[0]
        assert duplicate.duplicate_reason is DuplicateReason.SAME_CANONICAL_URL
        assert duplicate.duplicate_of_id is not None

    def test_the_duplicate_relationship_is_auditable(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        """'Why is B a duplicate of A?' must always have an answer."""
        add_item(raw_items, url_original="https://official.invalid/a")
        add_item(raw_items, url_original="https://official.invalid/a#frag")
        process(connection)

        repo = ArticleRepository(connection)
        duplicate = repo.list_by_status(ArticleStatus.DUPLICATE)[0]
        original = repo.get(duplicate.duplicate_of_id)  # type: ignore[arg-type]
        assert duplicate.duplicate_reason is not None
        assert duplicate.filtered_by is PrefilterReason.DUPLICATE
        assert original.status is ArticleStatus.NORMALIZED

    def test_duplicates_are_marked_not_deleted(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/a")
        add_item(raw_items, url_original="https://official.invalid/a/")
        process(connection)
        assert sum(ArticleRepository(connection).count_by_status().values()) == 2

    def test_unrelated_articles_are_not_merged(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/a")
        add_item(
            raw_items,
            url_original="https://official.invalid/b",
            title_original="A completely different story about something else",
            summary_raw="Researchers describe a novel CUDA kernel optimisation for sparse "
            "matrix multiplication on modern GPUs",
        )
        report = process(connection)
        assert report.duplicates == 0
        assert report.ready == 2

    def test_cross_source_resemblance_is_recorded_not_screened(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        """Secondary reporting is kept: it may corroborate a sensitive story later."""
        add_item(raw_items, "official", url_original="https://official.invalid/a")
        add_item(raw_items, "media", url_original="https://official.invalid/a")

        report = process(connection)
        assert report.possible_cross_source == 1
        assert report.exact_duplicates == 0

        repo = ArticleRepository(connection)
        flagged = [
            a for a in repo.list_by_status(ArticleStatus.NORMALIZED) if a.possible_duplicate_of_id
        ]
        assert len(flagged) == 1
        assert flagged[0].status is ArticleStatus.NORMALIZED


class TestPrefilterStage:
    def test_junk_is_screened_with_a_reason(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(
            raw_items,
            url_original="https://official.invalid/jobs",
            title_original="Careers at ExampleCorp",
            summary_raw=None,
        )
        report = process(connection)
        assert report.screened_out == 1

        screened = ArticleRepository(connection).list_by_status(ArticleStatus.SCREENED_OUT)[0]
        assert screened.filtered_by is PrefilterReason.JOB_LISTING

    def test_real_stories_survive(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(
            raw_items,
            url_original="https://official.invalid/feature",
            title_original="ChatGPT can now edit your photos directly in the app",
        )
        report = process(connection)
        assert report.ready == 1
        assert report.screened_out == 0

    def test_screening_reasons_are_counted(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(
            raw_items,
            url_original="https://official.invalid/j",
            title_original="We're hiring engineers",
            summary_raw=None,
        )
        report = process(connection)
        assert report.screening_reasons.get(PrefilterReason.JOB_LISTING.value) == 1


class TestCommunitySignals:
    def _hn_item(self, url: str, object_id: str = "1", points: int = 100) -> dict[str, object]:
        return {
            "external_id": object_id,
            "title_original": "Discussed on Hacker News",
            "url_original": url,
            "payload_raw": json.dumps(
                {"objectID": object_id, "points": points, "num_comments": 42}
            ),
        }

    def test_signal_sources_never_become_articles(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        """Community chatter must not be representable as an editorial candidate."""
        raw_items.add(make_raw_item("hackernews", **self._hn_item("https://official.invalid/a")))
        report = process(connection)
        assert report.normalized == 0
        assert sum(ArticleRepository(connection).count_by_status().values()) == 0

    def test_signals_are_recorded_separately(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        raw_items.add(make_raw_item("hackernews", **self._hn_item("https://official.invalid/a")))
        report = process(connection)
        assert report.signals_recorded == 1
        assert CommunitySignalRepository(connection).count() == 1

    def test_a_signal_attaches_to_a_matching_article(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/story")
        raw_items.add(
            make_raw_item(
                "hackernews", **self._hn_item("https://official.invalid/story?utm_source=hn")
            )
        )
        process(connection)

        repo = ArticleRepository(connection)
        article = repo.list_by_status(ArticleStatus.NORMALIZED)[0]
        signals = CommunitySignalRepository(connection).list_for_article(article.id)
        assert len(signals) == 1
        assert signals[0].points == 100
        assert signals[0].num_comments == 42

    def test_a_signal_seen_before_its_article_attaches_on_a_later_run(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        """Attention often precedes our copy of the story."""
        raw_items.add(
            make_raw_item("hackernews", **self._hn_item("https://official.invalid/later"))
        )
        process(connection)
        assert CommunitySignalRepository(connection).count_attached() == 0

        add_item(raw_items, url_original="https://official.invalid/later")
        process(connection)
        assert CommunitySignalRepository(connection).count_attached() == 1

    def test_signals_are_idempotent(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        raw_items.add(make_raw_item("hackernews", **self._hn_item("https://official.invalid/a")))
        process(connection)
        process(connection)
        assert CommunitySignalRepository(connection).count() == 1

    def test_signal_text_never_becomes_article_content(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/story")
        raw_items.add(
            make_raw_item("hackernews", **self._hn_item("https://official.invalid/story"))
        )
        process(connection)

        article = ArticleRepository(connection).list_by_status(ArticleStatus.NORMALIZED)[0]
        assert "Hacker News" not in (article.clean_text or "")
        assert article.source_id == "official"


class TestSourceSelection:
    def test_processing_can_be_scoped_to_one_source(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, "official", url_original="https://official.invalid/a")
        add_item(
            raw_items,
            "media",
            url_original="https://media.invalid/b",
            title_original="Media headline here",
            summary_raw=OTHER,
        )

        report = process(connection, source_ids=["media"])
        assert report.normalized == 1
        assert (
            ArticleRepository(connection).list_by_status(ArticleStatus.NORMALIZED)[0].source_id
            == "media"
        )


class TestStats:
    def test_funnel_counts(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/a")
        add_item(raw_items, url_original="https://official.invalid/a?utm_source=x")
        add_item(
            raw_items,
            url_original="https://official.invalid/j",
            title_original="Careers at ExampleCorp",
            summary_raw=None,
        )
        process(connection)

        stats = pipeline_stats(connection)
        assert stats["raw_items"] == 3
        assert stats["articles"] == 3
        assert stats["duplicates"] == 1
        assert stats["screened_out"] == 1
        assert stats["awaiting_evaluation"] == 1
        assert stats["unprocessed"] == 0

    def test_unprocessed_is_reported_before_processing(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/a")
        assert pipeline_stats(connection)["unprocessed"] == 1


class TestNoAiInvolved:
    def test_processing_reaches_no_network_and_no_model(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        """The whole stage is deterministic; the network guard would fail it otherwise."""
        add_item(raw_items, url_original="https://official.invalid/a")
        first = process(connection)
        assert first.normalized == 1

    def test_results_are_reproducible(
        self, seeded: None, raw_items: RawItemRepository, connection: sqlite3.Connection
    ) -> None:
        add_item(raw_items, url_original="https://official.invalid/a")
        process(connection)
        article = ArticleRepository(connection).list_by_status(ArticleStatus.NORMALIZED)[0]

        from ai_news_editor.pipeline.normalize import normalize

        again = normalize(raw_items.get(article.raw_item_id))
        assert again.content_hash == article.content_hash  # type: ignore[union-attr]
        assert again.simhash == article.simhash  # type: ignore[union-attr]
