"""The corners of the scheduling code: parsing, presets, holds and the queue service.

Small tests for the branches that only fire on a bad day — a preset that lands on a
missing hour, a queue item whose approval evaporated, a reschedule of something a worker
already holds. Each is cheap to write and expensive to discover in production at 09:00
on a Tuesday.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ai_news_editor.domain.enums import ContentType, DraftStatus, QueueStatus
from ai_news_editor.domain.models import QueueItem
from ai_news_editor.publishing.gate import approve_draft
from ai_news_editor.scheduling import queue as queue_service
from ai_news_editor.scheduling.clock import (
    CHANNEL_TIMEZONE,
    DAYPARTS,
    TimeError,
    daypart,
    describe,
    parse_local,
    to_local,
    to_utc,
    zone,
)
from ai_news_editor.scheduling.freshness import (
    DEFAULT_FRESHNESS,
    DEFAULT_OVERDUE_TOLERANCE,
    check_freshness,
    check_overdue,
    freshness_window,
    overdue_tolerance,
)
from ai_news_editor.scheduling.worker import Verdict, assess, format_assessment
from ai_news_editor.storage.repositories import DraftRepository
from ai_news_editor.storage.repositories.publication_queue import PublicationQueueRepository
from tests.conftest import DRAFT_CONTENT

CHANNEL = "@test_channel"
SOON = (datetime.now(UTC) + timedelta(hours=3)).replace(microsecond=0)


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    return root


def approved_draft(connection: sqlite3.Connection, drafts: DraftRepository, article):  # type: ignore[no-untyped-def]
    draft, version = drafts.create(article_id=article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)
    return drafts.get(draft.id), drafts.current_version(draft.id)


class TestParsingTimes:
    @pytest.mark.parametrize(
        "text",
        ["13.08 14:30", "13.08.2027 14:30", "2027-08-13 14:30", "13/08 14:30", "13-08 14:30"],
    )
    def test_every_documented_and_forgiving_format_is_accepted(self, text: str) -> None:
        parsed = parse_local(text, now=datetime(2027, 1, 1, tzinfo=UTC))
        assert parsed.tzinfo is not None
        assert to_local(parsed).hour == 14

    @pytest.mark.parametrize("text", ["", "   ", "tomorrow", "13.08", "14:30", "32.13 99:99"])
    def test_anything_unreadable_is_refused_rather_than_guessed(self, text: str) -> None:
        with pytest.raises(TimeError):
            parse_local(text, now=datetime(2027, 1, 1, tzinfo=UTC))

    def test_a_bare_date_still_ahead_this_year_stays_this_year(self) -> None:
        now = datetime(2027, 1, 10, 12, 0, tzinfo=UTC)
        assert parse_local("20.01 10:00", now=now).year == 2027

    def test_an_aware_datetime_is_refused_where_a_local_one_is_expected(self) -> None:
        """Passing something already zoned would silently double-convert."""
        with pytest.raises(TimeError, match="without a timezone"):
            to_utc(datetime(2027, 8, 13, 10, 0, tzinfo=UTC))

    def test_the_zone_helper_returns_the_channel_zone_by_default(self) -> None:
        assert str(zone()) == CHANNEL_TIMEZONE

    def test_describe_reads_the_way_a_person_writes_a_date(self) -> None:
        text = describe(datetime(2027, 8, 13, 7, 0, tzinfo=UTC))
        assert "13 Aug 2027" in text
        assert "10:00" in text
        assert CHANNEL_TIMEZONE in text


class TestPresets:
    @pytest.mark.parametrize("name", list(DAYPARTS))
    def test_each_preset_lands_on_its_configured_local_hour(self, name: str) -> None:
        now = datetime(2027, 6, 1, 12, 0, tzinfo=UTC)
        local = to_local(daypart(name, now=now))
        assert (local.hour, local.minute) == DAYPARTS[name]
        assert local.date() > to_local(now).date()

    def test_an_unknown_preset_is_refused(self) -> None:
        with pytest.raises(TimeError, match="unknown daypart"):
            daypart("midnight-ish", now=datetime(2027, 6, 1, tzinfo=UTC))

    def test_presets_are_named_after_the_day_not_after_engagement(self) -> None:
        """A convenience button must not smuggle in an editorial claim."""
        assert set(DAYPARTS) == {"morning", "afternoon", "evening"}
        for name in DAYPARTS:
            assert "best" not in name and "peak" not in name


class TestFreshnessPolicy:
    def test_every_content_type_has_both_windows(self) -> None:
        """A type with no policy would silently fall back to a guess."""
        for content_type in ContentType:
            assert content_type in DEFAULT_FRESHNESS, content_type
            assert content_type in DEFAULT_OVERDUE_TOLERANCE, content_type

    def test_news_is_the_strictest_and_evergreen_the_most_forgiving(self) -> None:
        assert DEFAULT_FRESHNESS[ContentType.NEWS] < DEFAULT_FRESHNESS[ContentType.PROMPT]
        assert DEFAULT_FRESHNESS[ContentType.PROMPT] < DEFAULT_FRESHNESS[ContentType.EXPLAINER]
        assert (
            DEFAULT_OVERDUE_TOLERANCE[ContentType.NEWS]
            < DEFAULT_OVERDUE_TOLERANCE[ContentType.RESOURCE]
        )

    def test_a_type_with_no_entry_still_gets_a_conservative_window(self) -> None:
        class Fake:
            value = "SOMETHING_NEW"

        assert freshness_window(Fake()) == timedelta(days=30)  # type: ignore[arg-type]
        assert overdue_tolerance(Fake()) == timedelta(days=1)  # type: ignore[arg-type]

    def test_a_verdict_is_truthy_only_when_fresh(self) -> None:
        approved_at = datetime(2027, 1, 1, tzinfo=UTC)
        fresh = check_freshness(
            content_type=ContentType.NEWS, approved_at=approved_at,
            now=approved_at + timedelta(hours=1),
        )
        stale = check_freshness(
            content_type=ContentType.NEWS, approved_at=approved_at,
            now=approved_at + timedelta(days=9),
        )
        assert bool(fresh) is True
        assert bool(stale) is False
        assert stale.reason and "another look" in stale.reason

    @pytest.mark.parametrize(
        ("delay", "expected_fragment"),
        [
            (timedelta(seconds=30), "s"),
            (timedelta(minutes=30), "min"),
            (timedelta(hours=5), "h"),
            (timedelta(days=5), "d"),
        ],
    )
    def test_durations_read_as_a_person_would_say_them(
        self, delay: timedelta, expected_fragment: str
    ) -> None:
        due = datetime(2027, 1, 1, tzinfo=UTC)
        verdict = check_overdue(
            content_type=ContentType.NEWS, scheduled_for=due, now=due + delay
        )
        text = verdict.reason or ""
        assert expected_fragment in text or verdict.fresh

    def test_something_not_yet_due_is_never_overdue(self) -> None:
        due = datetime(2027, 1, 1, tzinfo=UTC)
        assert check_overdue(
            content_type=ContentType.NEWS, scheduled_for=due, now=due - timedelta(hours=5)
        )


class TestQueueServiceEdges:
    def test_a_missing_draft_cannot_be_scheduled(
        self, connection: sqlite3.Connection, media_root: Path
    ) -> None:
        from ai_news_editor.domain.errors import EntityNotFoundError

        with pytest.raises((queue_service.QueueError, EntityNotFoundError)):
            queue_service.schedule(
                connection, uuid4(), SOON, channel=CHANNEL,
                media_root=media_root, actor="owner:test",
            )

    def test_rescheduling_to_the_past_is_refused(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        draft, _v = approved_draft(connection, drafts, seeded_article)
        item, _w = queue_service.schedule(
            connection, draft.id, SOON, channel=CHANNEL, media_root=media_root,
            actor="owner:test",
        )
        with pytest.raises(queue_service.QueueError, match="past"):
            queue_service.reschedule(
                connection, item.id, datetime(2020, 1, 1, tzinfo=UTC), actor="owner:test"
            )

    def test_rescheduling_with_a_naive_time_is_refused(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        draft, _v = approved_draft(connection, drafts, seeded_article)
        item, _w = queue_service.schedule(
            connection, draft.id, SOON, channel=CHANNEL, media_root=media_root,
            actor="owner:test",
        )
        with pytest.raises(queue_service.QueueError, match="timezone"):
            queue_service.reschedule(
                connection, item.id, datetime(2030, 1, 1, 10, 0), actor="owner:test"
            )

    def test_a_published_item_cannot_be_cancelled(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        draft, _v = approved_draft(connection, drafts, seeded_article)
        item, _w = queue_service.schedule(
            connection, draft.id, SOON, channel=CHANNEL, media_root=media_root,
            actor="owner:test",
        )
        repo = PublicationQueueRepository(connection)
        repo.set_status(item.id, QueueStatus.PUBLISHED, actor="w1")
        with pytest.raises(queue_service.QueueError, match="already published"):
            queue_service.cancel(connection, item.id, actor="owner:test")

    def test_an_item_a_worker_is_sending_cannot_be_cancelled_mid_flight(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        draft, _v = approved_draft(connection, drafts, seeded_article)
        item, _w = queue_service.schedule(
            connection, draft.id, SOON, channel=CHANNEL, media_root=media_root,
            actor="owner:test",
        )
        PublicationQueueRepository(connection).claim(item.id, worker="w1", now=SOON)
        with pytest.raises(queue_service.QueueError, match="right now"):
            queue_service.cancel(connection, item.id, actor="owner:test")

    def test_invalidating_leaves_an_in_flight_item_alone(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        """The worker re-verifies before it sends; pulling the row would only confuse it."""
        draft, _v = approved_draft(connection, drafts, seeded_article)
        item, _w = queue_service.schedule(
            connection, draft.id, SOON, channel=CHANNEL, media_root=media_root,
            actor="owner:test",
        )
        repo = PublicationQueueRepository(connection)
        repo.claim(item.id, worker="w1", now=SOON)

        stopped = queue_service.invalidate_for_draft(
            connection, draft.id, actor="test", reason="because"
        )
        assert stopped == []
        assert repo.get(item.id).status is QueueStatus.PROCESSING

    def test_upcoming_and_attention_lists_are_readable(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        draft, _v = approved_draft(connection, drafts, seeded_article)
        item, _w = queue_service.schedule(
            connection, draft.id, SOON, channel=CHANNEL, media_root=media_root,
            actor="owner:test",
        )
        assert [i.id for i in queue_service.upcoming(connection)] == [item.id]
        assert queue_service.needing_attention(connection) == []

        PublicationQueueRepository(connection).set_status(
            item.id, QueueStatus.HOLD_FOR_REVIEW, actor="w1", reason="a missing file"
        )
        assert [i.id for i in queue_service.needing_attention(connection)] == [item.id]


class TestQueueRepositoryEdges:
    def test_an_ambiguous_prefix_resolves_to_nothing(
        self, connection: sqlite3.Connection
    ) -> None:
        """Better to say "no match" than to act on the wrong post."""
        assert PublicationQueueRepository(connection).find("") is None

    def test_counting_by_status_reports_what_is_there(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        draft, _v = approved_draft(connection, drafts, seeded_article)
        queue_service.schedule(
            connection, draft.id, SOON, channel=CHANNEL, media_root=media_root,
            actor="owner:test",
        )
        assert PublicationQueueRepository(connection).count_by_status() == {"SCHEDULED": 1}

    def test_the_next_scheduled_item_is_the_soonest_one(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        draft, _v = approved_draft(connection, drafts, seeded_article)
        item, _w = queue_service.schedule(
            connection, draft.id, SOON, channel=CHANNEL, media_root=media_root,
            actor="owner:test",
        )
        repo = PublicationQueueRepository(connection)
        upcoming = repo.next_scheduled(now=SOON - timedelta(hours=1))
        assert upcoming is not None
        assert upcoming.id == item.id
        assert repo.next_scheduled(now=SOON + timedelta(hours=1)) is None


class TestAssessmentReporting:
    def test_a_stopped_item_is_reported_rather_than_processed(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        draft, version = approved_draft(connection, drafts, seeded_article)
        item = QueueItem(
            draft_id=draft.id,
            draft_version_id=version.id,
            review_decision_id=uuid4(),
            content_hash=version.content_hash,
            channel=CHANNEL,
            scheduled_for=SOON,
            status=QueueStatus.CANCELLED,
        )
        result = assess(connection, item, now=SOON, media_root=media_root)
        assert result.verdict is Verdict.HOLD
        assert "not waiting" in (result.reason or "")
        assert result.failed  # at least one named check is marked failed

    def test_the_dry_run_report_names_every_check_it_ran(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        draft, _v = approved_draft(connection, drafts, seeded_article)
        item, _w = queue_service.schedule(
            connection, draft.id, SOON, channel=CHANNEL, media_root=media_root,
            actor="owner:test",
        )
        result = assess(
            connection,
            PublicationQueueRepository(connection).get(item.id),
            now=SOON + timedelta(minutes=1),
            media_root=media_root,
        )
        lines = "\n".join(format_assessment(result))

        for expected in ("Queue item", "Scheduled", "Verdict", "version still current",
                         "approval still valid", "freshness", "assets present",
                         "publication gate", "Publication plan"):
            assert expected in lines, expected
        # And it never prints anything that could be a credential or a local path.
        assert "/Users/" not in lines
