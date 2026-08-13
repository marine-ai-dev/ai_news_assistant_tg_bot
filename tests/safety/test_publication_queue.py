"""Scheduling puts time between a human's yes and a post appearing. These guard that gap.

Everything that can go wrong in this project gets easier once a post can be scheduled
for Thursday. The draft can be edited on Wednesday. The approval can be withdrawn. The
image can be moved. The news can stop being news. And unlike the publish command, there
is nobody at the keyboard when the moment arrives.

So the rule these tests exist to hold is a single sentence: **a queue item is not
permission, it is a request to ask again.** Every check that guarded the publish command
runs again immediately before the send, and any one of them failing stops the post and
calls a human rather than resolving itself.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ai_news_editor.domain.enums import (
    ContentType,
    DraftStatus,
    PublicationStatus,
    QueueStatus,
    ReviewAction,
)
from ai_news_editor.domain.models import Publication, QueueItem
from ai_news_editor.publishing.gate import approve_draft
from ai_news_editor.scheduling import queue as queue_service
from ai_news_editor.scheduling.clock import TimeError, daypart, describe, parse_local, to_utc
from ai_news_editor.scheduling.freshness import check_freshness, check_overdue
from ai_news_editor.scheduling.worker import Verdict, assess, process_once
from ai_news_editor.storage.repositories import DraftRepository, PublicationRepository
from ai_news_editor.storage.repositories.publication_queue import PublicationQueueRepository
from tests.conftest import DRAFT_CONTENT

pytestmark = pytest.mark.safety

CHANNEL = "@test_channel"
#: Six hours out: a real future instant (so scheduling accepts it) that is still well
#: inside every freshness window, so a test about versions is not quietly a test about
#: staleness.
LATER = (datetime.now(UTC) + timedelta(hours=6)).replace(microsecond=0)


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    return root


def approved(connection: sqlite3.Connection, drafts: DraftRepository, article):
    """A draft a human approved, the only kind that can be scheduled."""
    draft, version = drafts.create(article_id=article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    approve_draft(
        connection,
        draft.id,
        actor="owner:test",
        expected_version_id=version.id,
    )
    return drafts.get(draft.id), drafts.current_version(draft.id)


def queue_one(
    connection: sqlite3.Connection,
    drafts: DraftRepository,
    article,
    *,
    media_root: Path,
    when: datetime = LATER,
) -> QueueItem:
    draft, _version = approved(connection, drafts, article)
    item, _warnings = queue_service.schedule(
        connection, draft.id, when, channel=CHANNEL, media_root=media_root, actor="owner:test"
    )
    return item


class TestOnlyApprovedContentCanBeQueued:
    """No approval, no queue. Scheduling is not a second way to approve something."""

    @pytest.mark.parametrize(
        "status",
        [DraftStatus.PENDING_REVIEW, DraftStatus.REJECTED, DraftStatus.NEEDS_REWRITE],
    )
    def test_unapproved_content_is_refused(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
        status: DraftStatus,
    ) -> None:
        draft, _version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        if status is not DraftStatus.PENDING_REVIEW:
            drafts.set_status(draft.id, status)

        with pytest.raises(queue_service.QueueError, match="no valid approval"):
            queue_service.schedule(
                connection, draft.id, LATER, channel=CHANNEL,
                media_root=media_root, actor="owner:test",
            )
        assert PublicationQueueRepository(connection).list_all() == []

    def test_already_published_content_is_refused(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        """Scheduling a published post would put a second copy on the channel."""
        draft, _version = approved(connection, drafts, seeded_article)
        drafts.set_status(draft.id, DraftStatus.PUBLISHING)
        drafts.set_status(draft.id, DraftStatus.PUBLISHED)

        with pytest.raises(queue_service.QueueError, match="already published"):
            queue_service.schedule(
                connection, draft.id, LATER, channel=CHANNEL,
                media_root=media_root, actor="owner:test",
            )

    def test_a_version_already_sent_cannot_be_queued_again(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        draft, version = approved(connection, drafts, seeded_article)
        decision = _approval_id(connection, draft.id, version.id)
        PublicationRepository(connection).add(
            Publication(
                draft_id=draft.id, draft_version_id=version.id, review_decision_id=decision,
                content_hash=version.content_hash, channel=CHANNEL,
                status=PublicationStatus.SUCCEEDED, message_id=7,
                published_at=datetime(2026, 8, 1, tzinfo=UTC),
            )
        )
        with pytest.raises(queue_service.QueueError, match="already published"):
            queue_service.schedule(
                connection, draft.id, LATER, channel=CHANNEL,
                media_root=media_root, actor="owner:test",
            )

    def test_the_same_version_cannot_be_scheduled_twice(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        with pytest.raises(queue_service.QueueError, match="already scheduled"):
            queue_service.schedule(
                connection, item.draft_id, LATER + timedelta(hours=3), channel=CHANNEL,
                media_root=media_root, actor="owner:test",
            )

    def test_a_past_time_is_refused(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        draft, _version = approved(connection, drafts, seeded_article)
        with pytest.raises(queue_service.QueueError, match="in the past"):
            queue_service.schedule(
                connection, draft.id, datetime(2020, 1, 1, tzinfo=UTC), channel=CHANNEL,
                media_root=media_root, actor="owner:test",
            )

    def test_a_naive_time_is_refused(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        """A time without a timezone is a time nobody agreed on."""
        draft, _version = approved(connection, drafts, seeded_article)
        with pytest.raises(queue_service.QueueError, match="timezone"):
            queue_service.schedule(
                connection, draft.id, datetime(2030, 1, 1, 10, 0), channel=CHANNEL,
                media_root=media_root, actor="owner:test",
            )

    def test_a_missing_required_file_is_refused_at_queue_time(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        """Told at the keyboard, not at three in the morning."""
        from ai_news_editor.domain.enums import MediaOrigin, MediaRole
        from ai_news_editor.domain.models import MediaAsset

        draft, _version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        _draft, version = drafts.append_version(
            draft.id,
            **{k: v for k, v in DRAFT_CONTENT.items() if k != "hashtags"},  # type: ignore[arg-type]
            media=(
                MediaAsset(
                    role=MediaRole.RESULT_IMAGE, origin=MediaOrigin.OWNER_GENERATED,
                    reference="gone.png", description="результат", tool_used="Gemini",
                ),
            ),
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        approve_draft(
            connection, draft.id, actor="owner:test", expected_version_id=version.id
        )
        with pytest.raises(queue_service.QueueError, match="not there"):
            queue_service.schedule(
                connection, draft.id, LATER, channel=CHANNEL,
                media_root=media_root, actor="owner:test",
            )


class TestEditingInvalidatesTheSchedule:
    """The single most important invariant in this phase."""

    def test_a_new_version_kills_the_queue_item(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        """V1 queued, then edited. V1 can never publish, and V2 was never scheduled."""
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)

        drafts.append_version(
            item.draft_id,
            **{**DRAFT_CONTENT, "title": "🆕 Зовсім інший заголовок"},  # type: ignore[arg-type]
        )

        repo = PublicationQueueRepository(connection)
        after = repo.get(item.id)
        assert after.status is QueueStatus.INVALIDATED
        assert "edited" in (after.hold_reason or "")

        # And nothing was quietly retargeted at the new version.
        assert repo.active_for_draft(item.draft_id) == []
        assert drafts.get(item.draft_id).status is DraftStatus.PENDING_REVIEW

    def test_an_invalidated_item_is_never_published(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        drafts.append_version(
            item.draft_id,
            **{**DRAFT_CONTENT, "title": "🆕 Інший заголовок"},  # type: ignore[arg-type]
        )
        report = process_once(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            now=LATER + timedelta(minutes=1),
        )
        # It is not even due any more — it left SCHEDULED the moment the draft changed.
        assert report.published == []

    def test_the_scheduler_refuses_a_stale_version_binding(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        """Belt and braces: even a hand-forged item pointing at an old version stops."""
        draft, version = approved(connection, drafts, seeded_article)
        item = QueueItem(
            draft_id=draft.id,
            draft_version_id=uuid4(),  # never current
            review_decision_id=_approval_id(connection, draft.id, version.id),
            content_hash=version.content_hash,
            channel=CHANNEL,
            scheduled_for=LATER,
        )
        verdict = assess(
            connection, item, now=LATER + timedelta(minutes=1), media_root=media_root
        )
        assert verdict.verdict is Verdict.INVALIDATE


class TestApprovalBinding:
    def test_a_changed_content_hash_stops_it(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        draft, version = approved(connection, drafts, seeded_article)
        item = QueueItem(
            draft_id=draft.id,
            draft_version_id=version.id,
            review_decision_id=_approval_id(connection, draft.id, version.id),
            content_hash="0" * 64,
            channel=CHANNEL,
            scheduled_for=LATER,
        )
        result = assess(connection, item, now=LATER, media_root=media_root)
        assert result.verdict is Verdict.INVALIDATE
        assert "hashes" in (result.reason or "")

    def test_an_unknown_approval_stops_it(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        draft, version = approved(connection, drafts, seeded_article)
        item = QueueItem(
            draft_id=draft.id,
            draft_version_id=version.id,
            review_decision_id=uuid4(),
            content_hash=version.content_hash,
            channel=CHANNEL,
            scheduled_for=LATER,
        )
        result = assess(connection, item, now=LATER, media_root=media_root)
        assert result.verdict is Verdict.INVALIDATE
        assert "approval" in (result.reason or "")


class TestFreshness:
    """Different content ages at completely different rates, so one window is wrong."""

    def test_news_expires_in_hours_and_an_explainer_does_not(self) -> None:
        approved_at = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
        three_days = approved_at + timedelta(days=3)

        stale = check_freshness(
            content_type=ContentType.NEWS, approved_at=approved_at, now=three_days
        )
        assert not stale
        assert "another look" in (stale.reason or "")

        assert check_freshness(
            content_type=ContentType.EXPLAINER, approved_at=approved_at, now=three_days
        )

    @pytest.mark.parametrize(
        "content_type",
        [ContentType.PROMPT, ContentType.TESTED_USE_CASE],
    )
    def test_source_backed_content_expires_in_weeks(self, content_type: ContentType) -> None:
        approved_at = datetime(2026, 1, 1, tzinfo=UTC)
        assert check_freshness(
            content_type=content_type, approved_at=approved_at,
            now=approved_at + timedelta(days=20),
        )
        assert not check_freshness(
            content_type=content_type, approved_at=approved_at,
            now=approved_at + timedelta(days=60),
        )

    def test_stale_news_is_held_rather_than_published(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        report = process_once(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            now=LATER + timedelta(days=30),
        )
        assert report.published == []
        after = PublicationQueueRepository(connection).get(item.id)
        assert after.status is QueueStatus.STALE_REVIEW_REQUIRED
        assert after.hold_reason


class TestOverduePolicy:
    """The Mac sleeps. The queue backs up. Nothing blasts out on wake."""

    def test_a_short_delay_is_fine(self) -> None:
        due = datetime(2026, 8, 13, 7, 0, tzinfo=UTC)
        assert check_overdue(
            content_type=ContentType.NEWS, scheduled_for=due, now=due + timedelta(minutes=20)
        )

    def test_a_long_delay_holds_news_sooner_than_an_explainer(self) -> None:
        due = datetime(2026, 8, 13, 7, 0, tzinfo=UTC)
        late = due + timedelta(hours=8)
        assert not check_overdue(content_type=ContentType.NEWS, scheduled_for=due, now=late)
        assert check_overdue(content_type=ContentType.EXPLAINER, scheduled_for=due, now=late)

    def test_a_heavily_overdue_item_is_held(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        report = process_once(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            now=LATER + timedelta(hours=12),
        )
        assert report.published == []
        assert PublicationQueueRepository(connection).get(item.id).status is (
            QueueStatus.STALE_REVIEW_REQUIRED
        )


class TestTimezone:
    """Kyiv time is the editorial clock. The Mac's timezone is never consulted."""

    def test_a_local_time_becomes_the_right_utc_instant(self) -> None:
        summer = to_utc(datetime(2026, 8, 13, 10, 0))
        assert summer == datetime(2026, 8, 13, 7, 0, tzinfo=UTC)  # UTC+3 in summer

        winter = to_utc(datetime(2026, 12, 13, 10, 0))
        assert winter == datetime(2026, 12, 13, 8, 0, tzinfo=UTC)  # UTC+2 in winter

    def test_it_displays_back_in_the_channel_timezone(self) -> None:
        assert describe(datetime(2026, 8, 13, 7, 0, tzinfo=UTC)).startswith("13 Aug 2026 · 10:00")

    def test_a_nonexistent_local_time_is_refused(self) -> None:
        """Spring forward: 03:30 does not happen that night, so it is not guessed at."""
        with pytest.raises(TimeError, match="does not exist"):
            to_utc(datetime(2026, 3, 29, 3, 30))

    def test_an_ambiguous_local_time_is_refused(self) -> None:
        """Autumn back: 03:30 happens twice, and picking one would publish an hour off."""
        with pytest.raises(TimeError, match="happens twice"):
            to_utc(datetime(2026, 10, 25, 3, 30))

    def test_presets_produce_valid_instants(self) -> None:
        now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)  # the day before the clocks move
        for name in ("morning", "afternoon", "evening"):
            when = daypart(name, now=now)
            assert when.tzinfo is not None
            assert when > now

    def test_typed_input_is_parsed_or_rejected_but_never_guessed(self) -> None:
        now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
        assert parse_local("13.08 14:30", now=now) == datetime(2026, 8, 13, 11, 30, tzinfo=UTC)
        assert parse_local("2026-08-13 14:30", now=now) == datetime(
            2026, 8, 13, 11, 30, tzinfo=UTC
        )
        with pytest.raises(TimeError, match="could not read"):
            parse_local("next tuesday-ish", now=now)

    def test_a_bare_date_that_has_passed_means_next_year(self) -> None:
        now = datetime(2026, 12, 20, 12, 0, tzinfo=UTC)
        assert parse_local("05.01 10:00", now=now).year == 2027


class TestWorkerCoordination:
    def test_only_one_of_two_workers_claims_an_item(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        """Two schedulers on one Mac is a Tuesday, not a hypothetical."""
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        repo = PublicationQueueRepository(connection)
        now = LATER + timedelta(minutes=1)

        first = repo.claim(item.id, worker="worker-a", now=now)
        second = repo.claim(item.id, worker="worker-b", now=now)

        assert first is not None
        assert second is None
        assert repo.get(item.id).claimed_by == "worker-a"

    def test_the_loser_does_not_process_the_item(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        now = LATER + timedelta(minutes=1)
        PublicationQueueRepository(connection).claim(item.id, worker="worker-a", now=now)

        report = process_once(
            connection, worker="worker-b", channel=CHANNEL, media_root=media_root, now=now
        )
        assert report.skipped_not_claimed == []  # it is no longer SCHEDULED, so not due
        assert report.published == []

    def test_a_dead_workers_claim_is_recovered_without_republishing(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        """A crashed worker must not lock a post out forever — or duplicate it."""
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        repo = PublicationQueueRepository(connection)
        claimed_at = LATER + timedelta(minutes=1)
        repo.claim(item.id, worker="dead-worker", now=claimed_at, lease=timedelta(minutes=1))

        much_later = claimed_at + timedelta(minutes=5)
        assert [i.id for i in repo.stale_claims(now=much_later)] == [item.id]

        report = process_once(
            connection, worker="fresh-worker", channel=CHANNEL,
            media_root=media_root, now=much_later,
        )
        assert item.id in report.recovered
        # Released and reassessed from scratch. Recovery grants nothing; the item is
        # judged again, and here it is simply too late to publish unattended.
        assert report.published == []
        assert repo.get(item.id).claimed_by is None

    def test_recovery_holds_an_item_whose_draft_is_mid_publish(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        """The dangerous crash: a worker died after starting to send.

        The draft is left in PUBLISHING, which has no valid approval, so the reassessment
        stops rather than sending something that may already be on the channel.
        """
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        drafts.set_status(item.draft_id, DraftStatus.PUBLISHING)

        result = assess(
            connection,
            PublicationQueueRepository(connection).get(item.id),
            now=LATER + timedelta(minutes=1),
            media_root=media_root,
        )
        assert result.verdict is not Verdict.PUBLISH


class TestCancelAndReschedule:
    def test_cancelling_leaves_the_approval_alone(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        """"Not then" is not "no". The draft stays approved and can be scheduled again."""
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        queue_service.cancel(connection, item.id, actor="owner:test")

        assert PublicationQueueRepository(connection).get(item.id).status is (
            QueueStatus.CANCELLED
        )
        assert drafts.get(item.draft_id).status is DraftStatus.APPROVED

        # And it can be scheduled again, without a second approval.
        again, _warnings = queue_service.schedule(
            connection, item.draft_id, LATER + timedelta(days=1), channel=CHANNEL,
            media_root=media_root, actor="owner:test",
        )
        assert again.status is QueueStatus.SCHEDULED

    def test_a_cancelled_item_never_publishes(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        queue_service.cancel(connection, item.id, actor="owner:test")
        report = process_once(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            now=LATER + timedelta(minutes=1),
        )
        assert report.published == []
        assert report.assessed == []

    def test_rescheduling_honours_only_the_new_time(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        moved_to = LATER + timedelta(days=2)
        moved, _warnings = queue_service.reschedule(
            connection, item.id, moved_to, actor="owner:test"
        )

        assert moved.scheduled_for == moved_to
        repo = PublicationQueueRepository(connection)
        # One row, not two: the old time cannot also fire.
        assert len(repo.list_upcoming()) == 1
        # And the history still knows where it started.
        assert repo.first_scheduled_for(item.id) == LATER

    def test_a_held_item_cannot_be_rescheduled_behind_the_scenes(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        PublicationQueueRepository(connection).set_status(
            item.id, QueueStatus.HOLD_FOR_REVIEW, actor="w1", reason="something to resolve"
        )
        with pytest.raises(queue_service.QueueError, match="Only a waiting item"):
            queue_service.reschedule(
                connection, item.id, LATER + timedelta(days=1), actor="owner:test"
            )


class TestSpacing:
    def test_a_nearby_post_produces_a_warning_not_a_refusal(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        queue_one(connection, drafts, seeded_article, media_root=media_root)
        warnings = queue_service.warnings_for(connection, LATER + timedelta(minutes=20))
        assert [w.kind for w in warnings] == ["crowding"]

    def test_an_identical_timestamp_is_refused_unless_deliberate(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
        sources,
        raw_items,
        articles,
    ) -> None:
        """Two posts in the same second reads as a glitch, so it takes a second decision."""
        queue_one(connection, drafts, seeded_article, media_root=media_root)
        other = _another_article(sources, raw_items, articles)
        second, _version = approved(connection, drafts, other)

        with pytest.raises(queue_service.QueueError, match="exactly"):
            queue_service.schedule(
                connection, second.id, LATER, channel=CHANNEL,
                media_root=media_root, actor="owner:test",
            )

        item, warnings = queue_service.schedule(
            connection, second.id, LATER, channel=CHANNEL, media_root=media_root,
            actor="owner:test", allow_collision=True,
        )
        assert item.status is QueueStatus.SCHEDULED
        assert any(w.kind == "collision" for w in warnings)


class TestSchedulerRestraint:
    """What the scheduler refuses to do is most of what makes it safe."""

    def test_it_never_publishes_something_nobody_queued(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        """An approved draft sitting there is not an instruction to publish it."""
        approved(connection, drafts, seeded_article)
        report = process_once(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            now=LATER + timedelta(days=1),
        )
        assert report.assessed == []
        assert report.published == []

    def test_a_dry_run_claims_nothing_and_sends_nothing(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        report = process_once(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            now=LATER + timedelta(minutes=1), dry_run=True,
        )
        assert report.sends_made == 0
        assert len(report.assessed) == 1
        assert PublicationQueueRepository(connection).get(item.id).status is (
            QueueStatus.SCHEDULED
        )
        assert PublicationQueueRepository(connection).get(item.id).claimed_by is None

    def test_a_dry_run_explains_its_decision(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        from ai_news_editor.scheduling.worker import format_assessment

        queue_one(connection, drafts, seeded_article, media_root=media_root)
        report = process_once(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            now=LATER + timedelta(minutes=1), dry_run=True,
        )
        lines = "\n".join(format_assessment(report.assessed[0]))
        for expected in ("version still current", "approval still valid", "freshness", "Verdict"):
            assert expected in lines

    def test_an_uncertain_component_stops_the_item_without_a_retry(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        """Avoid a duplicate over completeness, always."""
        from ai_news_editor.publishing.plan import Component
        from ai_news_editor.publishing.rich import ComponentOutcome, ComponentRepository

        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        version = drafts.current_version(item.draft_id)
        publication = PublicationRepository(connection).add(
            Publication(
                draft_id=item.draft_id, draft_version_id=version.id,
                review_decision_id=item.review_decision_id,
                content_hash=version.content_hash, channel=CHANNEL,
                status=PublicationStatus.FAILED, failure_reason="earlier attempt",
            )
        )
        ComponentRepository(connection).add(
            publication_id=publication.id, draft_id=item.draft_id,
            draft_version_id=version.id,
            outcome=ComponentOutcome(
                component=Component.MAIN, method="sendMessage",
                status=PublicationStatus.UNCERTAIN, failure_reason="lost",
            ),
        )

        result = assess(
            connection,
            PublicationQueueRepository(connection).get(item.id),
            now=LATER + timedelta(minutes=1),
            media_root=media_root,
        )
        assert result.verdict is Verdict.HOLD
        assert "unknown state" in (result.reason or "")

    def test_a_main_message_already_sent_is_never_sent_again(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        """Partial publication: the post exists, so the item is done, not repeated."""
        from ai_news_editor.publishing.plan import Component
        from ai_news_editor.publishing.rich import ComponentOutcome, ComponentRepository

        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        version = drafts.current_version(item.draft_id)
        publication = PublicationRepository(connection).add(
            Publication(
                draft_id=item.draft_id, draft_version_id=version.id,
                review_decision_id=item.review_decision_id,
                content_hash=version.content_hash, channel=CHANNEL,
                status=PublicationStatus.SUCCEEDED, message_id=99,
                published_at=LATER,
            )
        )
        ComponentRepository(connection).add(
            publication_id=publication.id, draft_id=item.draft_id,
            draft_version_id=version.id,
            outcome=ComponentOutcome(
                component=Component.MAIN, method="sendMessage",
                status=PublicationStatus.SUCCEEDED, message_id=99, chat_id="-100777",
            ),
        )

        result = assess(
            connection,
            PublicationQueueRepository(connection).get(item.id),
            now=LATER + timedelta(minutes=1),
            media_root=media_root,
        )
        assert result.verdict is Verdict.DONE
        assert "already on" in (result.reason or "")


class TestAuditTrail:
    def test_every_move_is_recorded_and_cannot_be_rewritten(
        self,
        connection: sqlite3.Connection,
        drafts: DraftRepository,
        seeded_article,
        media_root: Path,
    ) -> None:
        item = queue_one(connection, drafts, seeded_article, media_root=media_root)
        repo = PublicationQueueRepository(connection)
        queue_service.reschedule(connection, item.id, LATER + timedelta(days=1), actor="owner:test")
        queue_service.cancel(connection, item.id, actor="owner:test")

        events = [row["event"] for row in repo.history(item.id)]
        assert events == ["QUEUED", "RESCHEDULED", "CANCELLED"]

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE publication_queue_events SET event = 'x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM publication_queue_events")

    def test_a_held_item_always_says_why(self) -> None:
        """A hold nobody can understand is a hold nobody resolves."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="must record why"):
            QueueItem(
                draft_id=uuid4(), draft_version_id=uuid4(), review_decision_id=uuid4(),
                content_hash="a" * 64, channel=CHANNEL, scheduled_for=LATER,
                status=QueueStatus.HOLD_FOR_REVIEW,
            )


def _approval_id(connection: sqlite3.Connection, draft_id, version_id):
    from ai_news_editor.storage.repositories import ReviewDecisionRepository

    decision = ReviewDecisionRepository(connection).latest_approval(draft_id, version_id)
    assert decision is not None
    assert decision.action is ReviewAction.APPROVE
    return decision.id


def _another_article(sources, raw_items, articles):
    """A second article, so a second draft can exist alongside the first."""
    from tests.conftest import make_article, make_raw_item, make_source

    source = sources.upsert(make_source())
    item = raw_items.add(make_raw_item(source.id))
    return articles.add(
        make_article(item.id, source.id, canonical_url="https://example.invalid/second-story")
    )
