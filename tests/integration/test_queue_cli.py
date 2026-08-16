"""The queue and scheduler commands, driven through Typer's runner.

The CLI is where a busy person types something quickly, so the questions here are about
what a hurried keystroke can and cannot do: can `queue add` schedule something nobody
approved, can `scheduler once` publish something nobody queued, and does a mistyped date
get guessed at rather than refused.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from ai_news_editor.cli.main import app
from ai_news_editor.domain.enums import DraftStatus, QueueStatus
from ai_news_editor.publishing.gate import approve_draft
from ai_news_editor.settings import get_settings
from ai_news_editor.storage import db
from ai_news_editor.storage.repositories import DraftRepository
from ai_news_editor.storage.repositories.publication_queue import PublicationQueueRepository
from tests.conftest import DRAFT_CONTENT

runner = CliRunner()

CHANNEL = "@test_channel"
#: Assembled rather than written out, so no secret scanner has a literal to match.
TOKEN = "123456789:" + "A" * 35

sent: list[dict[str, Any]] = []


def output_of(result: object) -> str:
    import contextlib

    parts = [getattr(result, "output", "") or ""]
    with contextlib.suppress(AttributeError, ValueError):
        parts.append(result.stderr or "")  # type: ignore[attr-defined]
    return " ".join(" ".join(parts).split())


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A temporary database and a Telegram that cannot exist."""
    monkeypatch.setenv("AI_NEWS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AI_NEWS_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("AI_NEWS_TELEGRAM_CHANNEL", CHANNEL)
    monkeypatch.setenv("AI_NEWS_MEDIA_DIR", str(tmp_path / "media"))
    (tmp_path / "media").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    sent.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"url": str(request.url),
                     **(json.loads(request.content) if request.content else {})})
        if request.url.path.endswith("getChat"):
            return httpx.Response(
                200, json={"ok": True, "result": {"id": -100777, "type": "channel"}}
            )
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 555, "chat": {"id": -100777}}}
        )

    from ai_news_editor.publishing import telegram as telegram_module

    original = telegram_module.TelegramClient.__init__

    def patched(self, token, *, transport=None, **kwargs):  # type: ignore[no-untyped-def]
        original(self, token, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(telegram_module.TelegramClient, "__init__", patched)
    yield
    get_settings.cache_clear()


def connect(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "ai_news.sqlite3")


def seed(tmp_path: Path, *, approved: bool = True) -> str:
    """One draft, optionally carrying a real recorded human approval.

    Built through the repositories rather than raw SQL, so the fixture cannot drift
    away from the schema the application actually uses.
    """
    runner.invoke(app, ["db", "init"])
    connection = connect(tmp_path)
    try:
        from ai_news_editor.storage.repositories import (
            ArticleRepository,
            RawItemRepository,
            SourceRepository,
        )
        from tests.conftest import make_article, make_raw_item, make_source

        source = SourceRepository(connection).upsert(make_source())
        item = RawItemRepository(connection).add(make_raw_item(source.id))
        article = ArticleRepository(connection).add(make_article(item.id, source.id))

        drafts = DraftRepository(connection)
        draft, version = drafts.create(article_id=article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        if approved:
            approve_draft(connection, draft.id, actor="owner:test",
                          expected_version_id=version.id)
        connection.commit()
        return str(draft.id)
    finally:
        connection.close()


def when(hours: int = 30) -> str:
    """A local time far enough ahead to be unambiguously in the future."""
    from ai_news_editor.scheduling.clock import to_local

    return f"{to_local(datetime.now(UTC) + timedelta(hours=hours)):%d.%m %H:%M}"


class TestQueueAdd:
    def test_an_approved_draft_can_be_scheduled(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        result = runner.invoke(app, ["queue", "add", draft_id, "--at", when()])

        assert result.exit_code == 0, output_of(result)
        assert "Scheduled" in output_of(result)
        assert "Europe/Kyiv" in output_of(result)

        connection = connect(tmp_path)
        items = PublicationQueueRepository(connection).list_all()
        assert len(items) == 1
        assert items[0].status is QueueStatus.SCHEDULED
        connection.close()

    def test_an_unapproved_draft_cannot_be_scheduled(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path, approved=False)
        result = runner.invoke(app, ["queue", "add", draft_id, "--at", when()])

        assert result.exit_code == 1
        assert "Not scheduled" in output_of(result)
        connection = connect(tmp_path)
        assert PublicationQueueRepository(connection).list_all() == []
        connection.close()

    def test_a_time_in_the_past_is_refused(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        result = runner.invoke(app, ["queue", "add", draft_id, "--at", "2020-01-01 10:00"])
        assert result.exit_code == 1
        assert "past" in output_of(result)

    def test_an_unreadable_time_is_refused_rather_than_guessed(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        result = runner.invoke(app, ["queue", "add", draft_id, "--at", "sometime thursday"])
        assert result.exit_code == 1
        assert "could not read" in output_of(result)

    def test_a_nonexistent_local_time_is_refused(self, tmp_path: Path) -> None:
        """Spring forward: 03:30 does not happen that night in Kyiv."""
        draft_id = seed(tmp_path)
        result = runner.invoke(app, ["queue", "add", draft_id, "--at", "2027-03-28 03:30"])
        assert result.exit_code == 1
        assert "does not exist" in output_of(result)

    def test_a_malformed_draft_id_is_refused(self, tmp_path: Path) -> None:
        seed(tmp_path)
        result = runner.invoke(app, ["queue", "add", "not-a-uuid", "--at", when()])
        assert result.exit_code == 1
        assert "Not a draft id" in output_of(result)


class TestQueueViewing:
    def test_an_empty_queue_says_so_and_suggests_the_next_step(self, tmp_path: Path) -> None:
        seed(tmp_path)
        result = runner.invoke(app, ["queue", "list"])
        assert result.exit_code == 0
        assert "Nothing scheduled" in output_of(result)

    def test_list_shows_a_scheduled_post_in_channel_time(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["queue", "add", draft_id, "--at", when()])
        result = runner.invoke(app, ["queue", "list"])
        assert "Europe/Kyiv" in output_of(result)
        assert "SCHEDULED" in output_of(result)

    def test_show_explains_one_item_with_its_history(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["queue", "add", draft_id, "--at", when()])
        connection = connect(tmp_path)
        item = PublicationQueueRepository(connection).list_all()[0]
        connection.close()

        result = runner.invoke(app, ["queue", "show", str(item.id)[:8]])
        assert result.exit_code == 0
        assert "QUEUED" in output_of(result)
        assert "History" in output_of(result)

    def test_an_unknown_queue_id_fails_clearly(self, tmp_path: Path) -> None:
        seed(tmp_path)
        result = runner.invoke(app, ["queue", "show", "deadbeef"])
        assert result.exit_code == 1
        assert "No queue item matches" in output_of(result)

    def test_the_policy_table_is_honest_about_what_it_is(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["queue", "policy"])
        assert result.exit_code == 0
        text = output_of(result)
        assert "NEWS" in text and "EXPLAINER" in text
        # The numbers are editorial defaults, and the command says so rather than
        # implying they were measured.
        assert "not measured optima" in text


class TestRescheduleAndCancel:
    def test_rescheduling_honours_only_the_new_time(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["queue", "add", draft_id, "--at", when(30)])
        connection = connect(tmp_path)
        item = PublicationQueueRepository(connection).list_all()[0]
        original = item.scheduled_for
        connection.close()

        result = runner.invoke(app, ["queue", "reschedule", str(item.id)[:8], "--at", when(50)])
        assert result.exit_code == 0, output_of(result)
        assert "Moved" in output_of(result)

        connection = connect(tmp_path)
        items = PublicationQueueRepository(connection).list_all()
        assert len(items) == 1  # moved, not duplicated
        assert items[0].scheduled_for != original
        connection.close()

    def test_cancelling_leaves_the_draft_approved(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["queue", "add", draft_id, "--at", when()])
        connection = connect(tmp_path)
        item = PublicationQueueRepository(connection).list_all()[0]
        connection.close()

        result = runner.invoke(app, ["queue", "cancel", str(item.id)[:8]])
        assert result.exit_code == 0
        assert "still approved" in output_of(result)

        connection = connect(tmp_path)
        from uuid import UUID

        assert PublicationQueueRepository(connection).get(item.id).status is QueueStatus.CANCELLED
        assert DraftRepository(connection).get(UUID(draft_id)).status is DraftStatus.APPROVED
        connection.close()

    def test_a_cancelled_item_cannot_be_rescheduled(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["queue", "add", draft_id, "--at", when()])
        connection = connect(tmp_path)
        item = PublicationQueueRepository(connection).list_all()[0]
        connection.close()
        runner.invoke(app, ["queue", "cancel", str(item.id)[:8]])

        result = runner.invoke(app, ["queue", "reschedule", str(item.id)[:8], "--at", when(50)])
        assert result.exit_code == 1
        assert "Not rescheduled" in output_of(result)


class TestScheduler:
    def test_a_dry_run_sends_nothing_and_says_so(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["queue", "add", draft_id, "--at", when()])

        result = runner.invoke(app, ["scheduler", "once", "--dry-run"])
        assert result.exit_code == 0, output_of(result)
        assert "ZERO Telegram sends" in output_of(result)
        assert [s for s in sent if "sendMessage" in s["url"]] == []

    def test_a_pass_with_nothing_due_publishes_nothing(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["queue", "add", draft_id, "--at", when()])

        result = runner.invoke(app, ["scheduler", "once"])
        assert result.exit_code == 0, output_of(result)
        assert "published: 0" in output_of(result)
        assert [s for s in sent if "sendMessage" in s["url"]] == []

    def test_an_empty_queue_is_not_an_error(self, tmp_path: Path) -> None:
        seed(tmp_path)
        result = runner.invoke(app, ["scheduler", "once", "--dry-run"])
        assert result.exit_code == 0
        assert "Nothing is due" in output_of(result)


class TestNoDangerousFlags:
    """Scheduling must not acquire the flags publishing was never allowed."""

    @pytest.mark.parametrize("command", [["queue", "add"], ["queue", "cancel"],
                                         ["scheduler", "once"], ["scheduler", "run"]])
    def test_no_auto_or_bulk_flag_exists(self, command: list[str]) -> None:
        from typer.main import get_command

        forbidden = {"--yes", "-y", "--all", "--approve-all", "--auto-approve",
                     "--auto-publish", "--publish-all", "--force"}
        node: Any = get_command(app)
        for part in command:
            node = node.commands[part]
        options = {opt for param in node.params for opt in param.opts}
        assert options & forbidden == set()

    def test_the_scheduler_cannot_be_told_to_approve_anything(self) -> None:
        from typer.main import get_command

        scheduler = get_command(app).commands["scheduler"]
        for name, sub in scheduler.commands.items():
            joined = " ".join(opt for param in sub.params for opt in param.opts)
            assert "approve" not in joined.lower(), name


def _week_flag() -> list[str]:
    """Which week `when()` actually lands in.

    A time 30 hours out is next week when today is a Sunday, and the calendar would
    then correctly show an empty current week. Ask for the week the post is really in
    rather than assuming it is this one.
    """
    from ai_news_editor.planning.calendar import week_bounds

    now = datetime.now(UTC)
    target = now + timedelta(hours=30)
    return [] if week_bounds(now) == week_bounds(target) else ["--next"]


class TestCalendarCommands:
    """The calendar reads and reports. It must never schedule."""

    def test_an_empty_week_is_not_an_error(self, tmp_path: Path) -> None:
        seed(tmp_path)
        result = runner.invoke(app, ["calendar", "week"])
        assert result.exit_code == 0, output_of(result)
        assert "Europe/Kyiv" in output_of(result)
        assert "Nothing scheduled" in output_of(result)

    def test_the_week_shows_a_scheduled_post_with_its_metadata(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["queue", "add", draft_id, "--at", when()])

        result = runner.invoke(app, ["calendar", "week", *_week_flag()])
        assert result.exit_code == 0, output_of(result)
        text = output_of(result)
        assert "NEWS" in text
        assert "NEWCOMER" in text or "BEGINNER" in text or "GENERAL" in text
        assert "SCHEDULED" in text

    def test_next_week_is_a_different_week(self, tmp_path: Path) -> None:
        seed(tmp_path)
        this_week = output_of(runner.invoke(app, ["calendar", "week"]))
        next_week = output_of(runner.invoke(app, ["calendar", "week", "--next"]))
        assert this_week != next_week

    def test_balance_reports_the_mix_and_says_targets_are_not_quotas(
        self, tmp_path: Path
    ) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["queue", "add", draft_id, "--at", when()])

        result = runner.invoke(app, ["calendar", "balance", *_week_flag()])
        assert result.exit_code == 0, output_of(result)
        text = output_of(result)
        assert "target" in text.lower()
        assert "not quotas" in text
        assert "beginner-accessible" in text

    def test_gaps_lists_approved_work_without_scheduling_any_of_it(
        self, tmp_path: Path
    ) -> None:
        draft_id = seed(tmp_path)
        result = runner.invoke(app, ["calendar", "gaps"])

        assert result.exit_code == 0, output_of(result)
        assert draft_id[:8] in output_of(result)
        assert "Approved, not scheduled" in output_of(result)

        connection = connect(tmp_path)
        assert PublicationQueueRepository(connection).list_all() == []
        connection.close()

    def test_gaps_counts_pending_review_separately(self, tmp_path: Path) -> None:
        """Pending work is not publishable, so it never joins the calendar."""
        seed(tmp_path, approved=False)
        result = runner.invoke(app, ["calendar", "gaps"])
        assert "Awaiting review (not publishable)" in output_of(result)

    def test_suggest_proposes_a_time_and_schedules_nothing(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        result = runner.invoke(app, ["calendar", "suggest", draft_id])

        assert result.exit_code == 0, output_of(result)
        text = output_of(result)
        assert "Suggested slots" in text
        assert "nothing has been scheduled" in text
        # It hands the owner the command rather than running it.
        assert "ai-news queue add" in text

        connection = connect(tmp_path)
        assert PublicationQueueRepository(connection).list_all() == []
        connection.close()

    def test_suggest_shows_its_reasoning(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        text = output_of(runner.invoke(app, ["calendar", "suggest", draft_id]))
        assert "slot, currently free" in text
        assert "score" in text

    def test_suggest_never_claims_a_best_posting_time(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        text = output_of(runner.invoke(app, ["calendar", "suggest", draft_id])).lower()
        for claim in ("best time", "optimal", "peak", "highest engagement"):
            assert claim not in text, claim

    def test_suggest_refuses_a_malformed_draft_id(self, tmp_path: Path) -> None:
        seed(tmp_path)
        result = runner.invoke(app, ["calendar", "suggest", "not-a-uuid"])
        assert result.exit_code == 1
        assert "Not a draft id" in output_of(result)

    def test_no_calendar_command_can_schedule_or_approve(self) -> None:
        """The whole group is read-only, and that is asserted rather than assumed."""
        from typer.main import get_command

        calendar = get_command(app).commands["calendar"]
        forbidden = {"--yes", "-y", "--all", "--approve", "--approve-all", "--schedule",
                     "--auto", "--apply", "--confirm"}
        for name, sub in calendar.commands.items():
            options = {opt for param in sub.params for opt in param.opts}
            assert options & forbidden == set(), name
