"""The scheduler actually sending something. Never skip these.

Every other test about the queue asks whether the scheduler *decides* correctly. These
ask what happens when it decides yes and starts making Telegram calls with nobody at the
keyboard — which is the only moment in this project where a machine puts words in front
of readers.

Two properties matter more than the rest, and both are about the same failure:

**The main post is never sent twice.** Not on a retry, not after a crash, not after a
comment fails, not when two workers wake up at the same second.

**An unknown outcome stops everything.** If the scheduler cannot tell whether a message
landed, it records that and waits for a person. A missing comment is an annoyance a
human can fix; a duplicate post is something readers see and nobody can undo.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_news_editor.domain.enums import DraftStatus, PublicationStatus, QueueStatus
from ai_news_editor.publishing.gate import approve_draft
from ai_news_editor.publishing.plan import Component
from ai_news_editor.publishing.rich import ComponentRepository
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.scheduling import queue as queue_service
from ai_news_editor.scheduling.worker import Verdict, assess, process_once, run
from ai_news_editor.storage.repositories import DraftRepository, PublicationRepository
from ai_news_editor.storage.repositories.publication_queue import PublicationQueueRepository
from tests.conftest import DRAFT_CONTENT

pytestmark = pytest.mark.safety

CHANNEL = "@test_channel"
#: Assembled, never written out: a scanner cannot tell a placeholder from a credential.
TOKEN = "123456789:" + "A" * 35
SOON = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0)
DUE = SOON + timedelta(minutes=1)


class Recorder(httpx.MockTransport):
    """Answers every Bot API call and counts what was actually sent."""

    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.calls: list[str] = []
        self.payloads: list[dict[str, Any]] = []
        failures = failures or {}

        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            self.calls.append(method)
            if request.headers.get("content-type", "").startswith("application/json"):
                self.payloads.append(json.loads(request.content))

            mode = failures.get(method)
            if mode == "fail":
                return httpx.Response(
                    400, json={"ok": False, "error_code": 400, "description": "nope"}
                )
            if mode == "timeout":
                raise httpx.ReadTimeout("lost", request=request)
            if method == "getChat":
                # No linked discussion group, exactly like the real test channel.
                return httpx.Response(
                    200, json={"ok": True, "result": {"id": -100777, "type": "channel"}}
                )
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 4242, "chat": {"id": -100777}}}
            )

        super().__init__(handler)

    def count(self, method: str) -> int:
        return self.calls.count(method)


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    return root


def factory_for(transport: Recorder):  # type: ignore[no-untyped-def]
    """Build clients on demand, all sharing one recording transport."""

    def build() -> TelegramClient:
        return TelegramClient(TOKEN, transport=transport)

    return build


def queued(connection: sqlite3.Connection, drafts: DraftRepository, article, media_root: Path):  # type: ignore[no-untyped-def]
    """An approved draft, deliberately scheduled — the only way a queue item exists."""
    draft, version = drafts.create(article_id=article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)
    item, _warnings = queue_service.schedule(
        connection, draft.id, SOON, channel=CHANNEL, media_root=media_root, actor="owner:test"
    )
    return item


class TestTheHappyPath:
    def test_a_due_item_publishes_once_and_is_recorded(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        item = queued(connection, drafts, seeded_article, media_root)
        transport = Recorder()

        report = process_once(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            client_factory=factory_for(transport), now=DUE,
        )

        assert report.published == [item.id]
        assert transport.count("sendMessage") == 1

        after = PublicationQueueRepository(connection).get(item.id)
        assert after.status is QueueStatus.PUBLISHED
        assert after.publication_id is not None
        assert after.claimed_by is None

        publication = PublicationRepository(connection).get(after.publication_id)
        assert publication.status is PublicationStatus.SUCCEEDED
        assert publication.message_id == 4242
        assert drafts.get(item.draft_id).status is DraftStatus.PUBLISHED

        # And the main message is recorded as a component, which is what stops a
        # future run from ever sending it again.
        assert Component.MAIN in ComponentRepository(connection).succeeded(item.draft_version_id)

    def test_a_second_pass_sends_nothing_more(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        """The most important assertion in this file."""
        queued(connection, drafts, seeded_article, media_root)
        transport = Recorder()
        factory = factory_for(transport)

        process_once(connection, worker="w1", channel=CHANNEL, media_root=media_root,
                     client_factory=factory, now=DUE)
        sent_after_first = transport.count("sendMessage")

        process_once(connection, worker="w1", channel=CHANNEL, media_root=media_root,
                     client_factory=factory, now=DUE + timedelta(minutes=1))

        assert transport.count("sendMessage") == sent_after_first == 1


class TestNothingIsSentWithoutIntent:
    def test_an_approved_draft_nobody_queued_is_never_touched(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)
        transport = Recorder()

        report = process_once(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            client_factory=factory_for(transport), now=DUE + timedelta(days=1),
        )

        assert report.published == []
        # getChat is the read-only discussion-group lookup a pass always does. What
        # matters is that nothing was *sent*.
        assert [c for c in transport.calls if c.startswith("send")] == []
        assert drafts.get(draft.id).status is DraftStatus.APPROVED

    def test_a_dry_run_makes_no_send_request_at_all(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        item = queued(connection, drafts, seeded_article, media_root)
        transport = Recorder()

        report = process_once(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            client_factory=factory_for(transport), now=DUE, dry_run=True,
        )

        assert transport.count("sendMessage") == 0
        assert report.sends_made == 0
        assert PublicationQueueRepository(connection).get(item.id).status is QueueStatus.SCHEDULED


class TestFailureIsNeverGuessedAt:
    def test_a_refused_send_leaves_the_draft_republishable(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        """Telegram said no, so nothing is on the channel and nothing is assumed."""
        item = queued(connection, drafts, seeded_article, media_root)
        transport = Recorder(failures={"sendMessage": "fail"})

        report = process_once(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            client_factory=factory_for(transport), now=DUE,
        )

        assert report.published == []
        after = PublicationQueueRepository(connection).get(item.id)
        assert after.status is QueueStatus.FAILED
        assert after.hold_reason
        # The draft goes back to APPROVED: a retry must not need a second human decision.
        assert drafts.get(item.draft_id).status is DraftStatus.APPROVED

    def test_a_lost_response_stops_everything_and_never_retries(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        """The dangerous case: the post may or may not exist. A machine must not guess."""
        item = queued(connection, drafts, seeded_article, media_root)
        transport = Recorder(failures={"sendMessage": "timeout"})
        factory = factory_for(transport)

        process_once(connection, worker="w1", channel=CHANNEL, media_root=media_root,
                     client_factory=factory, now=DUE)

        after = PublicationQueueRepository(connection).get(item.id)
        assert after.status is QueueStatus.UNCERTAIN
        assert "by hand" in (after.hold_reason or "")
        # The draft stays in PUBLISHING, which is not a publishable state.
        assert drafts.get(item.draft_id).status is DraftStatus.PUBLISHING

        # A later pass must not try again, whatever the clock says.
        attempts = transport.count("sendMessage")
        process_once(connection, worker="w1", channel=CHANNEL, media_root=media_root,
                     client_factory=factory, now=DUE + timedelta(hours=1))
        assert transport.count("sendMessage") == attempts

    def test_an_unresolved_attempt_blocks_a_fresh_schedule_too(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        item = queued(connection, drafts, seeded_article, media_root)
        transport = Recorder(failures={"sendMessage": "timeout"})
        process_once(connection, worker="w1", channel=CHANNEL, media_root=media_root,
                     client_factory=factory_for(transport), now=DUE)

        # Two independent guards refuse this, and the first one wins: an unresolved
        # attempt leaves the draft in PUBLISHING, which has no valid approval. Either
        # message is correct; what must never happen is a second schedule.
        with pytest.raises(queue_service.QueueError):
            queue_service.schedule(
                connection, item.draft_id, SOON + timedelta(days=1), channel=CHANNEL,
                media_root=media_root, actor="owner:test",
            )
        assert PublicationQueueRepository(connection).active_for_draft(item.draft_id) == []


class TestTwoWorkers:
    def test_only_one_of_two_concurrent_passes_sends_anything(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        """Two schedulers on one Mac is a Tuesday, not a hypothetical."""
        queued(connection, drafts, seeded_article, media_root)
        transport = Recorder()
        factory = factory_for(transport)

        first = process_once(connection, worker="worker-a", channel=CHANNEL,
                             media_root=media_root, client_factory=factory, now=DUE)
        second = process_once(connection, worker="worker-b", channel=CHANNEL,
                              media_root=media_root, client_factory=factory, now=DUE)

        assert len(first.published) == 1
        assert second.published == []
        assert transport.count("sendMessage") == 1


class TestCrashRecovery:
    def test_a_worker_that_died_before_sending_can_be_recovered_and_published(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        """Nothing was sent, so recovery is safe — and the post still goes out."""
        item = queued(connection, drafts, seeded_article, media_root)
        repo = PublicationQueueRepository(connection)
        repo.claim(item.id, worker="dead", now=DUE, lease=timedelta(minutes=1))

        transport = Recorder()
        report = process_once(
            connection, worker="fresh", channel=CHANNEL, media_root=media_root,
            client_factory=factory_for(transport), now=DUE + timedelta(minutes=2),
        )

        assert item.id in report.recovered
        assert report.published == [item.id]
        assert transport.count("sendMessage") == 1

    def test_a_worker_that_died_mid_send_is_held_rather_than_retried(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        """Nobody knows what was sent, so nobody sends anything."""
        item = queued(connection, drafts, seeded_article, media_root)
        repo = PublicationQueueRepository(connection)
        repo.claim(item.id, worker="dead", now=DUE, lease=timedelta(minutes=1))
        # The publication service marks the draft PUBLISHING and commits before it
        # sends. A crash after that point leaves exactly this state.
        drafts.set_status(item.draft_id, DraftStatus.PUBLISHING)

        transport = Recorder()
        report = process_once(
            connection, worker="fresh", channel=CHANNEL, media_root=media_root,
            client_factory=factory_for(transport), now=DUE + timedelta(minutes=2),
        )

        assert report.published == []
        assert transport.count("sendMessage") == 0
        assert PublicationQueueRepository(connection).get(item.id).status is (
            QueueStatus.HOLD_FOR_REVIEW
        )

    def test_recovery_never_deadlocks(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        item = queued(connection, drafts, seeded_article, media_root)
        repo = PublicationQueueRepository(connection)
        repo.claim(item.id, worker="dead", now=DUE, lease=timedelta(seconds=1))
        assert [i.id for i in repo.stale_claims(now=DUE + timedelta(minutes=5))] == [item.id]

        repo.release(item.id, worker="fresh", reason="lease expired")
        reclaimed = repo.claim(item.id, worker="fresh", now=DUE + timedelta(minutes=5))
        assert reclaimed is not None


class TestPartialPublication:
    def test_a_recorded_main_message_is_never_sent_again(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        item = queued(connection, drafts, seeded_article, media_root)
        transport = Recorder()
        factory = factory_for(transport)
        process_once(connection, worker="w1", channel=CHANNEL, media_root=media_root,
                     client_factory=factory, now=DUE)

        # Force a second look at the same version, as a resume would.
        result = assess(
            connection,
            PublicationQueueRepository(connection).get(item.id),
            now=DUE + timedelta(minutes=5),
            media_root=media_root,
        )
        assert result.verdict is not Verdict.PUBLISH

    def test_a_comment_with_no_discussion_group_is_deferred_not_dropped(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        """The real channel has no linked group. That must be recorded, never inlined."""
        from ai_news_editor.domain.enums import PromptPlacement

        draft, _v = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        _d, version = drafts.append_version(
            draft.id,
            **{**DRAFT_CONTENT, "prompt_placement": PromptPlacement.COMMENT,  # type: ignore[arg-type]
               "comment_text": "Повний промпт: опиши задачу і дай контекст."},
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        approve_draft(connection, draft.id, actor="owner:test", expected_version_id=version.id)
        item, _w = queue_service.schedule(
            connection, draft.id, SOON, channel=CHANNEL, media_root=media_root,
            actor="owner:test",
        )

        transport = Recorder()
        process_once(connection, worker="w1", channel=CHANNEL, media_root=media_root,
                     client_factory=factory_for(transport), now=DUE)

        rows = connection.execute(
            "SELECT component, status FROM publication_components WHERE draft_version_id = ?",
            (str(item.draft_version_id),),
        ).fetchall()
        recorded = {r["component"]: r["status"] for r in rows}
        assert recorded["MAIN"] == "SUCCEEDED"
        assert recorded["COMMENT"] == "DEFERRED"
        # The prompt was not quietly folded into the post to make up for it.
        posted = next(p for p in transport.payloads if "text" in p)
        assert "Повний промпт" not in posted["text"]


class TestTheLoop:
    def test_the_loop_stops_and_reports_each_pass(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        queued(connection, drafts, seeded_article, media_root)
        transport = Recorder()
        seen: list[int] = []

        passes = run(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            client_factory=factory_for(transport),
            poll_interval=timedelta(seconds=1),
            max_passes=2,
            on_pass=lambda report: seen.append(len(report.published)),
            sleep=lambda _seconds: None,
        )

        assert passes == 2
        assert len(seen) == 2
        # Nothing is due yet in real time, so the loop is quiet rather than eager.
        assert transport.count("sendMessage") == 0

    def test_the_loop_never_busy_waits(
        self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
        media_root: Path,
    ) -> None:
        """A queue with nothing due for hours must not wake up hundreds of times."""
        queued(connection, drafts, seeded_article, media_root)
        slept: list[float] = []

        run(
            connection, worker="w1", channel=CHANNEL, media_root=media_root,
            poll_interval=timedelta(seconds=60), max_passes=3,
            sleep=slept.append,
        )

        assert slept  # it did sleep
        assert all(delay >= 1.0 for delay in slept)
