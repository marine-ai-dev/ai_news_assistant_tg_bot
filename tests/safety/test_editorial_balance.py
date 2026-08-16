"""The calendar describes and suggests. It must never decide.

Phase 10 is the first layer that offers an opinion about *what to publish next*, which
makes it the first place where a helpful tool could quietly become an editor. These
tests draw that line and keep it drawn:

**It writes nothing.** No approval, no queue row, no status change. A suggestion is a
sentence.

**It never invents.** A tool it cannot read from metadata is absent, not guessed from
prose. A percentage it cannot compute is not reported.

**It never claims a best time.** There is no analytics data in this project, so a
"peak engagement hour" would be a fabrication the owner would then plan around.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    ContentType,
    DraftStatus,
)
from ai_news_editor.planning.buckets import (
    ACCESSIBLE_TARGET_MIN,
    DEFAULT_MIX,
    Bucket,
    bucket_for,
    is_accessible,
    is_practical,
)
from ai_news_editor.planning.calendar import (
    Freshness,
    approved_unscheduled,
    build_week,
    pending_summary,
    week_bounds,
)
from ai_news_editor.planning.suggest import suggest_slot
from ai_news_editor.publishing.gate import approve_draft
from ai_news_editor.scheduling import queue as queue_service
from ai_news_editor.storage.repositories import DraftRepository
from ai_news_editor.storage.repositories.publication_queue import PublicationQueueRepository
from tests.conftest import DRAFT_CONTENT

pytestmark = pytest.mark.safety

CHANNEL = "@test_channel"


def _next_monday() -> datetime:
    """The start of the next full week, in UTC.

    Pinned to a Monday rather than "tomorrow" so these tests do not depend on the day
    they are run: a week built from "tomorrow" on a Sunday lands in the *following*
    week, and the calendar under test then correctly reports an empty week while the
    test expects a full one. Found exactly that way.
    """
    now = datetime.now(UTC)
    ahead = 7 - now.weekday() or 7
    return (now + timedelta(days=ahead)).replace(
        hour=7, minute=0, second=0, microsecond=0
    )


#: Monday morning of the next full week. Every scheduled post in this file hangs off it,
#: and every calendar assertion examines the week that contains it.
BASE = _next_monday()


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    return root


def make_scheduled(
    connection: sqlite3.Connection,
    drafts: DraftRepository,
    articles,
    sources,
    raw_items,
    media_root: Path,
    *,
    when: datetime,
    audience: AudienceTier = AudienceTier.NEWCOMER,
    category: Category = Category.EVERYDAY_AI,
    url: str | None = None,
    **overrides: object,
):  # type: ignore[no-untyped-def]
    """One approved, scheduled post. Everything goes through the real services."""
    from tests.conftest import make_article, make_raw_item, make_source

    source = sources.upsert(make_source())
    item = raw_items.add(make_raw_item(source.id))
    article = articles.add(
        make_article(item.id, source.id, canonical_url=url or f"https://e.invalid/{uuid4()}")
    )

    content = {**DRAFT_CONTENT, "audience": audience, "category": category, **overrides}
    draft, version = drafts.create(article_id=article.id, **content)  # type: ignore[arg-type]
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)
    queued, _warnings = queue_service.schedule(
        connection, draft.id, when, channel=CHANNEL, media_root=media_root,
        actor="owner:test", allow_collision=True,
    )
    return queued


class TestBucketsReuseTheExistingTaxonomy:
    """Phase 10 added no stored taxonomy. Every bucket is derived."""

    def test_every_content_type_and_category_pair_resolves(self) -> None:
        for content_type in ContentType:
            for category in Category:
                assert isinstance(bucket_for(content_type, category), Bucket)

    def test_a_prompt_is_practical_whatever_it_is_about(self) -> None:
        for category in Category:
            assert bucket_for(ContentType.PROMPT, category) is Bucket.PRACTICAL
            assert bucket_for(ContentType.TESTED_USE_CASE, category) is Bucket.PRACTICAL

    @pytest.mark.parametrize(
        ("category", "expected"),
        [
            (Category.PRODUCT_UPDATE, Bucket.PRODUCT_UPDATE),
            (Category.USEFUL_TOOL, Bucket.PRODUCT_UPDATE),
            (Category.WOW, Bucket.WOW),
            (Category.AI_FAIL, Bucket.WOW),
            (Category.SCIENCE_LITE, Bucket.SCIENCE),
            (Category.TRENDING, Bucket.NEWS),
            (Category.EVERYDAY_AI, Bucket.NEWS),
        ],
    )
    def test_news_is_split_by_what_it_is_about(
        self, category: Category, expected: Bucket
    ) -> None:
        assert bucket_for(ContentType.NEWS, category) is expected

    def test_the_targets_sum_to_one(self) -> None:
        assert sum(DEFAULT_MIX.values()) == pytest.approx(1.0)

    def test_accessibility_means_newcomer_or_beginner(self) -> None:
        assert is_accessible(AudienceTier.NEWCOMER)
        assert is_accessible(AudienceTier.BEGINNER)
        assert not is_accessible(AudienceTier.GENERAL)
        assert not is_accessible(AudienceTier.TECH_CURIOUS)

    def test_practical_means_something_to_try(self) -> None:
        assert is_practical(ContentType.PROMPT)
        assert is_practical(ContentType.TESTED_USE_CASE)
        assert not is_practical(ContentType.NEWS)


class TestTheCalendarIsAView:
    def test_an_empty_week_says_so_rather_than_inventing_content(
        self, connection: sqlite3.Connection
    ) -> None:
        week = build_week(connection, now=BASE)
        assert week.entries == []
        assert any(w.kind == "empty" for w in week.warnings)

    def test_a_week_runs_monday_to_sunday_in_channel_time(self) -> None:
        start, end = week_bounds(datetime(2026, 8, 13, 12, 0, tzinfo=UTC))
        assert start.weekday() == 0
        assert end.weekday() == 6
        assert (end - start).days == 6

    def test_scheduled_posts_appear_grouped_by_day(
        self, connection: sqlite3.Connection, drafts, articles, sources, raw_items,
        media_root: Path,
    ) -> None:
        make_scheduled(connection, drafts, articles, sources, raw_items, media_root, when=BASE)
        make_scheduled(
            connection, drafts, articles, sources, raw_items, media_root,
            when=BASE + timedelta(days=1),
        )
        week = build_week(connection, now=BASE)
        assert len(week.entries) == 2
        assert len(week.by_day) == 2

    def test_cancelled_and_invalidated_items_are_not_on_the_calendar(
        self, connection: sqlite3.Connection, drafts, articles, sources, raw_items,
        media_root: Path,
    ) -> None:
        item = make_scheduled(
            connection, drafts, articles, sources, raw_items, media_root, when=BASE
        )
        queue_service.cancel(connection, item.id, actor="owner:test")
        week = build_week(connection, now=BASE)
        assert week.entries == []

    def test_the_calendar_writes_nothing(
        self, connection: sqlite3.Connection, drafts, articles, sources, raw_items,
        media_root: Path,
    ) -> None:
        """The strongest property in this file."""
        item = make_scheduled(
            connection, drafts, articles, sources, raw_items, media_root, when=BASE
        )
        before = {
            "queue": [
                (i.id, i.status, i.scheduled_for)
                for i in PublicationQueueRepository(connection).list_all()
            ],
            "drafts": [
                (d.id, d.status) for d in DraftRepository(connection).list_all(limit=100)
            ],
            "decisions": connection.execute(
                "SELECT COUNT(*) FROM review_decisions"
            ).fetchone()[0],
        }

        build_week(connection, now=datetime.now(UTC))
        approved_unscheduled(connection, now=datetime.now(UTC), channel=CHANNEL)
        pending_summary(connection)
        suggest_slot(connection, item.draft_id, now=datetime.now(UTC), channel=CHANNEL)

        after = {
            "queue": [
                (i.id, i.status, i.scheduled_for)
                for i in PublicationQueueRepository(connection).list_all()
            ],
            "drafts": [
                (d.id, d.status) for d in DraftRepository(connection).list_all(limit=100)
            ],
            "decisions": connection.execute(
                "SELECT COUNT(*) FROM review_decisions"
            ).fetchone()[0],
        }
        assert before == after


class TestDiagnostics:
    def _week_of(
        self, connection, drafts, articles, sources, raw_items, media_root, specs
    ):  # type: ignore[no-untyped-def]
        for offset, spec in enumerate(specs):
            make_scheduled(
                connection, drafts, articles, sources, raw_items, media_root,
                when=BASE + timedelta(hours=6 * offset), **spec,
            )
        return build_week(connection, now=BASE)

    def test_a_week_with_no_beginner_content_is_flagged(
        self, connection, drafts, articles, sources, raw_items, media_root: Path
    ) -> None:
        week = self._week_of(
            connection, drafts, articles, sources, raw_items, media_root,
            [{"audience": AudienceTier.TECH_CURIOUS}] * 4,
        )
        audience = [w for w in week.warnings if w.kind == "audience"]
        assert audience
        assert "never opened an AI chat" in audience[0].message

    def test_a_thin_beginner_share_is_reported_with_the_numbers(
        self, connection, drafts, articles, sources, raw_items, media_root: Path
    ) -> None:
        week = self._week_of(
            connection, drafts, articles, sources, raw_items, media_root,
            [{"audience": AudienceTier.NEWCOMER}] + [{"audience": AudienceTier.GENERAL}] * 5,
        )
        audience = next(w for w in week.warnings if w.kind == "audience")
        assert "1 of 6" in audience.message
        assert f"{ACCESSIBLE_TARGET_MIN:.0%}" in audience.message

    def test_a_healthy_share_is_reported_as_information_not_a_warning(
        self, connection, drafts, articles, sources, raw_items, media_root: Path
    ) -> None:
        week = self._week_of(
            connection, drafts, articles, sources, raw_items, media_root,
            [{"audience": AudienceTier.NEWCOMER}] * 3 + [{"audience": AudienceTier.GENERAL}] * 2,
        )
        audience = next(w for w in week.warnings if w.kind == "audience")
        assert audience.severity == "INFO"

    def test_a_run_of_the_same_bucket_is_flagged(
        self, connection, drafts, articles, sources, raw_items, media_root: Path
    ) -> None:
        week = self._week_of(
            connection, drafts, articles, sources, raw_items, media_root,
            [{"category": Category.EVERYDAY_AI}] * 5,
        )
        streaks = [w for w in week.warnings if w.kind == "streak"]
        assert streaks
        assert "in a row" in streaks[0].message

    def test_a_run_of_technical_posts_is_flagged_sooner(
        self, connection, drafts, articles, sources, raw_items, media_root: Path
    ) -> None:
        week = self._week_of(
            connection, drafts, articles, sources, raw_items, media_root,
            [{"audience": AudienceTier.TECH_CURIOUS}] * 3,
        )
        assert any(
            "assume prior AI experience" in w.message
            for w in week.warnings if w.kind == "streak"
        )

    def test_a_week_with_nothing_to_try_is_flagged(
        self, connection, drafts, articles, sources, raw_items, media_root: Path
    ) -> None:
        week = self._week_of(
            connection, drafts, articles, sources, raw_items, media_root,
            [{"category": Category.WOW}] * 3,
        )
        practical = [w for w in week.warnings if w.kind == "practical"]
        assert practical
        assert "what the channel promises" in practical[0].message

    def test_missing_buckets_are_named(
        self, connection, drafts, articles, sources, raw_items, media_root: Path
    ) -> None:
        week = self._week_of(
            connection, drafts, articles, sources, raw_items, media_root,
            [{"category": Category.EVERYDAY_AI}] * 5,
        )
        mix = [w for w in week.warnings if w.kind == "mix"]
        assert any("No" in w.message and "scheduled this week" in w.message for w in mix)

    def test_one_publisher_dominating_the_week_is_reported_not_rejected(
        self, connection, drafts, articles, sources, raw_items, media_root: Path
    ) -> None:
        """Google shipping three things is reporting, not bias. Say so; refuse nothing."""
        week = self._week_of(
            connection, drafts, articles, sources, raw_items, media_root,
            [{"category": Category.PRODUCT_UPDATE}] * 4,
        )
        source_warnings = [w for w in week.warnings if w.kind == "source"]
        assert source_warnings
        assert "not a reason to drop anything" in source_warnings[0].message
        # And every post is still on the calendar.
        assert len(week.entries) == 4


SERIES = "7 днів AI-креативів"


def make_series_post(
    connection: sqlite3.Connection, drafts: DraftRepository, media_root: Path,
    *, when: datetime, order: int,
):  # type: ignore[no-untyped-def]
    """A scheduled post belonging to an ordered series.

    A series is defined on the ContentItem, not the DraftVersion, so this goes through
    editorial-original content rather than a news article.
    """
    from ai_news_editor.domain.enums import ContentOrigin
    from ai_news_editor.domain.models import ContentItem, ExplainerBody
    from ai_news_editor.storage.repositories import ContentItemRepository

    item = ContentItemRepository(connection).add(
        ContentItem(
            content_type=ContentType.EXPLAINER,
            origin=ContentOrigin.EDITORIAL_ORIGINAL,
            audience=AudienceTier.NEWCOMER,
            title=f"Частина {order}",
            body=ExplainerBody(
                concept="Промпт",
                simple_explanation="Промпт — це те, що ви пишете ШІ звичайними словами.",
                real_life_example="Як записка колезі: що зробити і в якому вигляді.",
                why_it_matters="Від формулювання залежить, наскільки корисна відповідь.",
            ),
            series_name=SERIES,
            series_order=order,
            created_by="test",
        )
    )
    content = {**DRAFT_CONTENT, "content_type": ContentType.EXPLAINER,
               "audience": AudienceTier.NEWCOMER}
    draft, version = drafts.create(content_item_id=item.id, **content)  # type: ignore[arg-type]
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)
    queued, _w = queue_service.schedule(
        connection, draft.id, when, channel=CHANNEL, media_root=media_root,
        actor="owner:test", allow_collision=True,
    )
    return queued


class TestSeriesOrder:
    def test_a_series_scheduled_out_of_order_is_flagged_but_not_moved(
        self, connection: sqlite3.Connection, drafts, media_root: Path
    ) -> None:
        second = make_series_post(connection, drafts, media_root, when=BASE, order=2)
        make_series_post(
            connection, drafts, media_root, when=BASE + timedelta(days=1), order=1
        )

        week = build_week(connection, now=BASE)
        series_warnings = [w for w in week.warnings if w.kind == "series"]
        assert series_warnings
        assert "out of order" in series_warnings[0].message
        assert "Nothing was moved" in series_warnings[0].message

        # And the schedule is untouched.
        assert PublicationQueueRepository(connection).get(second.id).scheduled_for == BASE

    def test_a_series_in_order_produces_no_warning(
        self, connection: sqlite3.Connection, drafts, media_root: Path
    ) -> None:
        make_series_post(connection, drafts, media_root, when=BASE, order=1)
        make_series_post(
            connection, drafts, media_root, when=BASE + timedelta(days=1), order=2
        )
        week = build_week(connection, now=BASE)
        assert [w for w in week.warnings if w.kind == "series"] == []


class TestWaitingAndPending:
    def test_approved_but_unscheduled_drafts_are_listed_separately(
        self, connection: sqlite3.Connection, drafts, seeded_article, media_root: Path
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)

        waiting = approved_unscheduled(connection, now=datetime.now(UTC), channel=CHANNEL)
        assert [w.draft.id for w in waiting] == [draft.id]
        assert waiting[0].freshness is Freshness.FRESH
        # And listing them scheduled nothing.
        assert PublicationQueueRepository(connection).list_all() == []

    def test_a_scheduled_draft_is_not_listed_as_waiting(
        self, connection: sqlite3.Connection, drafts, articles, sources, raw_items,
        media_root: Path,
    ) -> None:
        make_scheduled(connection, drafts, articles, sources, raw_items, media_root, when=BASE)
        assert approved_unscheduled(connection, now=datetime.now(UTC), channel=CHANNEL) == []

    def test_pending_review_is_counted_but_never_mixed_into_the_calendar(
        self, connection: sqlite3.Connection, drafts, seeded_article
    ) -> None:
        draft, _v = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)

        assert pending_summary(connection) == {"NEWS": 1}
        week = build_week(connection, now=BASE)
        assert week.entries == []


class TestSuggestion:
    def test_it_proposes_times_and_schedules_nothing(
        self, connection: sqlite3.Connection, drafts, seeded_article, media_root: Path
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)

        suggestion = suggest_slot(
            connection, draft.id, now=datetime.now(UTC), channel=CHANNEL
        )
        assert suggestion.candidates
        assert suggestion.best is not None
        assert PublicationQueueRepository(connection).list_all() == []

    def test_every_candidate_explains_itself(
        self, connection: sqlite3.Connection, drafts, seeded_article, media_root: Path
    ) -> None:
        """A recommendation nobody can argue with is one they should ignore."""
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)

        suggestion = suggest_slot(
            connection, draft.id, now=datetime.now(UTC), channel=CHANNEL
        )
        for candidate in suggestion.candidates:
            assert candidate.reasons
            assert all(r.text for r in candidate.reasons)
            assert candidate.score == sum(r.points for r in candidate.reasons)

    def test_it_is_deterministic(
        self, connection: sqlite3.Connection, drafts, seeded_article, media_root: Path
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)

        now = datetime.now(UTC)
        first = suggest_slot(connection, draft.id, now=now, channel=CHANNEL)
        second = suggest_slot(connection, draft.id, now=now, channel=CHANNEL)
        assert [c.when for c in first.candidates] == [c.when for c in second.candidates]
        assert [c.score for c in first.candidates] == [c.score for c in second.candidates]

    def test_a_slot_that_another_post_already_holds_becomes_blocked(
        self, connection: sqlite3.Connection, drafts, articles, sources, raw_items,
        media_root: Path,
    ) -> None:
        """Take the slot the suggester likes best, then ask again."""
        first = seeded(sources, raw_items, articles)
        draft_a, version_a = drafts.create(article_id=first.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft_a.id, DraftStatus.PENDING_REVIEW)
        approve_draft(connection, draft_a.id, actor="owner:test",
                      expected_version_id=version_a.id)

        now = datetime.now(UTC)
        preferred = suggest_slot(connection, draft_a.id, now=now, channel=CHANNEL).best
        assert preferred is not None

        queue_service.schedule(
            connection, draft_a.id, preferred.when, channel=CHANNEL,
            media_root=media_root, actor="owner:test", allow_collision=True,
        )

        second = seeded(sources, raw_items, articles)
        draft_b, version_b = drafts.create(article_id=second.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft_b.id, DraftStatus.PENDING_REVIEW)
        approve_draft(connection, draft_b.id, actor="owner:test",
                      expected_version_id=version_b.id)

        suggestion = suggest_slot(connection, draft_b.id, now=now, channel=CHANNEL)
        clashing = next(c for c in suggestion.candidates if c.when == preferred.when)
        assert clashing.blocked
        assert any("exact moment" in r.text for r in clashing.reasons)
        # And it still offers somewhere else rather than giving up.
        assert suggestion.best is not None
        assert suggestion.best.when != preferred.when

    def test_it_never_claims_a_best_time_to_post(
        self, connection: sqlite3.Connection, drafts, seeded_article, media_root: Path
    ) -> None:
        """There is no analytics data here, so such a claim would be fabricated."""
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)

        suggestion = suggest_slot(
            connection, draft.id, now=datetime.now(UTC), channel=CHANNEL
        )
        text = " ".join(
            [*(n for n in suggestion.notes),
             *(r.text for c in suggestion.candidates for r in c.reasons)]
        ).lower()
        for claim in ("best time", "engagement", "optimal", "peak", "reach"):
            assert claim not in text, claim


def seeded(sources, raw_items, articles):
    """Another article, so a second draft can exist alongside the first."""
    from tests.conftest import make_article, make_raw_item, make_source

    source = sources.upsert(make_source())
    item = raw_items.add(make_raw_item(source.id))
    return articles.add(
        make_article(item.id, source.id, canonical_url=f"https://e.invalid/{uuid4()}")
    )


class TestSuggestionScoring:
    """Every point the suggester moves must trace to a reason a person can read."""

    def _approved(self, connection, drafts, sources, raw_items, articles, **overrides):
        article = seeded(sources, raw_items, articles)
        content = {**DRAFT_CONTENT, **overrides}
        draft, version = drafts.create(article_id=article.id, **content)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)
        return draft

    def test_news_is_pushed_towards_sooner_slots(
        self, connection: sqlite3.Connection, drafts, sources, raw_items, articles
    ) -> None:
        """News keeps badly, and the reasoning says exactly that."""
        draft = self._approved(
            connection, drafts, sources, raw_items, articles,
            content_type=ContentType.NEWS, category=Category.TRENDING,
        )
        suggestion = suggest_slot(
            connection, draft.id, now=datetime.now(UTC), channel=CHANNEL
        )
        soon = [c for c in suggestion.candidates if any("keeps badly" in r.text for r in c.reasons)]
        assert soon
        assert suggestion.best is not None
        assert any("keeps badly" in r.text for r in suggestion.best.reasons)

    def test_an_empty_day_is_worth_points(
        self, connection: sqlite3.Connection, drafts, sources, raw_items, articles
    ) -> None:
        draft = self._approved(connection, drafts, sources, raw_items, articles)
        suggestion = suggest_slot(
            connection, draft.id, now=datetime.now(UTC), channel=CHANNEL
        )
        assert any(
            any("nothing else scheduled that day" in r.text for r in c.reasons)
            for c in suggestion.candidates
        )

    def test_a_crowded_slot_is_penalised_with_the_gap_named(
        self, connection: sqlite3.Connection, drafts, sources, raw_items, articles,
        media_root: Path,
    ) -> None:
        first = self._approved(connection, drafts, sources, raw_items, articles)
        now = datetime.now(UTC)
        preferred = suggest_slot(connection, first.id, now=now, channel=CHANNEL).best
        assert preferred is not None
        # Put something 20 minutes away, inside the Phase-9 crowding window.
        queue_service.schedule(
            connection, first.id, preferred.when + timedelta(minutes=20), channel=CHANNEL,
            media_root=media_root, actor="owner:test", allow_collision=True,
        )

        second = self._approved(connection, drafts, sources, raw_items, articles)
        suggestion = suggest_slot(connection, second.id, now=now, channel=CHANNEL)
        crowded = next(c for c in suggestion.candidates if c.when == preferred.when)
        assert crowded.blocked
        assert any("min away" in r.text for r in crowded.reasons)

    def test_a_week_short_on_this_bucket_earns_points(
        self, connection: sqlite3.Connection, drafts, sources, raw_items, articles,
        media_root: Path,
    ) -> None:
        """Fill a week with one bucket, then ask where a different one should go."""
        for offset in range(3):
            make_scheduled(
                connection, drafts, articles, sources, raw_items, media_root,
                when=BASE + timedelta(hours=6 * offset), category=Category.WOW,
            )
        plain_news = self._approved(
            connection, drafts, sources, raw_items, articles, category=Category.TRENDING
        )
        suggestion = suggest_slot(
            connection, plain_news.id, now=datetime.now(UTC), channel=CHANNEL
        )
        assert any(
            any("short on NEWS" in r.text for r in c.reasons)
            for c in suggestion.candidates
        )

    def test_an_accessible_post_is_favoured_when_the_week_is_technical(
        self, connection: sqlite3.Connection, drafts, sources, raw_items, articles,
        media_root: Path,
    ) -> None:
        for offset in range(3):
            make_scheduled(
                connection, drafts, articles, sources, raw_items, media_root,
                when=BASE + timedelta(hours=6 * offset),
                audience=AudienceTier.TECH_CURIOUS,
            )
        friendly = self._approved(
            connection, drafts, sources, raw_items, articles, audience=AudienceTier.NEWCOMER
        )
        suggestion = suggest_slot(
            connection, friendly.id, now=datetime.now(UTC), channel=CHANNEL
        )
        assert any(
            any("needs more beginner-accessible" in r.text for r in c.reasons)
            for c in suggestion.candidates
        )

    def test_a_run_of_the_same_bucket_costs_points(
        self, connection: sqlite3.Connection, drafts, sources, raw_items, articles,
        media_root: Path,
    ) -> None:
        for offset in range(2):
            make_scheduled(
                connection, drafts, articles, sources, raw_items, media_root,
                when=BASE + timedelta(hours=4 * offset), category=Category.TRENDING,
            )
        more_news = self._approved(
            connection, drafts, sources, raw_items, articles, category=Category.TRENDING
        )
        suggestion = suggest_slot(
            connection, more_news.id, now=datetime.now(UTC), channel=CHANNEL
        )
        assert any(
            any("would make a run of" in r.text for r in c.reasons)
            for c in suggestion.candidates
        )

    def test_a_draft_with_no_approval_is_told_so_plainly(
        self, connection: sqlite3.Connection, drafts, seeded_article
    ) -> None:
        draft, _v = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)

        suggestion = suggest_slot(
            connection, draft.id, now=datetime.now(UTC), channel=CHANNEL
        )
        assert any("cannot be scheduled at all yet" in note for note in suggestion.notes)

    def test_the_notes_say_how_much_of_the_week_is_already_this_bucket(
        self, connection: sqlite3.Connection, drafts, sources, raw_items, articles,
        media_root: Path,
    ) -> None:
        make_scheduled(
            connection, drafts, articles, sources, raw_items, media_root, when=BASE
        )
        draft = self._approved(connection, drafts, sources, raw_items, articles)
        suggestion = suggest_slot(
            connection, draft.id, now=datetime.now(UTC), channel=CHANNEL
        )
        assert any("upcoming posts are already" in note for note in suggestion.notes)

    def test_a_series_part_follows_its_predecessor(
        self, connection: sqlite3.Connection, drafts, media_root: Path
    ) -> None:
        """Part 2 must not be offered a slot before part 1."""
        first = make_series_post(connection, drafts, media_root, when=BASE, order=1)

        from ai_news_editor.domain.enums import ContentOrigin
        from ai_news_editor.domain.models import ContentItem, ExplainerBody
        from ai_news_editor.storage.repositories import ContentItemRepository

        item = ContentItemRepository(connection).add(
            ContentItem(
                content_type=ContentType.EXPLAINER,
                origin=ContentOrigin.EDITORIAL_ORIGINAL,
                audience=AudienceTier.NEWCOMER,
                title="Частина 2",
                body=ExplainerBody(
                    concept="Контекст",
                    simple_explanation="Контекст — це те, що ШІ вже знає про вашу задачу.",
                    real_life_example="Як пояснити колезі, з чого все почалося.",
                    why_it_matters="Без контексту відповідь буде загальною.",
                ),
                series_name=SERIES,
                series_order=2,
                created_by="test",
            )
        )
        content = {**DRAFT_CONTENT, "content_type": ContentType.EXPLAINER}
        draft, version = drafts.create(content_item_id=item.id, **content)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)

        suggestion = suggest_slot(
            connection, draft.id, now=datetime.now(UTC), channel=CHANNEL
        )
        assert any("Part 2 of" in note for note in suggestion.notes)

        before_part_one = [
            c for c in suggestion.candidates if c.when < first.scheduled_for
        ]
        assert all(c.blocked for c in before_part_one), (
            "a later part must never be offered a slot before an earlier one"
        )
        assert any(
            any("follows the previous part" in r.text for r in c.reasons)
            for c in suggestion.candidates
        )
