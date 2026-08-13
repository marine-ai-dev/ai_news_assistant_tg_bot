"""``ai-news queue`` and ``ai-news scheduler``.

Two commands sets that look similar and are not. ``queue`` is how the owner expresses
intent; ``scheduler`` is the machine acting on intent already expressed. Nothing in
``scheduler`` can create a queue item, and nothing in ``queue`` sends anything.

Neither can approve. There is no flag here that turns a pending draft into a scheduled
one — approval happens in ``ai-news review`` or the review bot, and scheduling can only
ever follow it.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import QueueStatus
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.domain.models import QueueItem
from ai_news_editor.scheduling import queue as queue_service
from ai_news_editor.scheduling.clock import (
    CHANNEL_TIMEZONE,
    TimeError,
    describe,
    parse_local,
    to_local,
)
from ai_news_editor.scheduling.freshness import DEFAULT_FRESHNESS, DEFAULT_OVERDUE_TOLERANCE
from ai_news_editor.scheduling.worker import (
    DEFAULT_POLL_INTERVAL,
    format_assessment,
    process_once,
    run,
)
from ai_news_editor.settings import Settings, get_settings
from ai_news_editor.storage.repositories import DraftRepository
from ai_news_editor.storage.repositories.publication_queue import PublicationQueueRepository

console = Console()
err_console = Console(stderr=True)

queue_app = typer.Typer(name="queue", help="Schedule approved posts, and see what is scheduled.")
scheduler_app = typer.Typer(name="scheduler", help="Run the local publication scheduler.")

_STATUS_COLOUR = {
    QueueStatus.SCHEDULED: "cyan",
    QueueStatus.PROCESSING: "blue",
    QueueStatus.PUBLISHED: "green",
    QueueStatus.CANCELLED: "dim",
    QueueStatus.INVALIDATED: "yellow",
    QueueStatus.STALE_REVIEW_REQUIRED: "yellow",
    QueueStatus.HOLD_FOR_REVIEW: "yellow",
    QueueStatus.FAILED: "red",
    QueueStatus.UNCERTAIN: "bold red",
}


def _settings() -> Settings:
    try:
        return get_settings()
    except AiNewsError as exc:
        err_console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


def _channel(settings: Settings) -> str:
    if not settings.telegram_channel:
        err_console.print(
            "[bold red]No channel configured.[/bold red] Set AI_NEWS_TELEGRAM_CHANNEL."
        )
        raise typer.Exit(code=1)
    return settings.telegram_channel


def _resolve(connection: sqlite3.Connection, reference: str) -> QueueItem:
    """Accept a full queue id or an unambiguous prefix of one."""
    item = PublicationQueueRepository(connection).find(reference.strip())
    if item is None:
        err_console.print(
            f"[bold red]No queue item matches[/bold red] {reference!r}. "
            "Run 'ai-news queue list' to see the ids."
        )
        raise typer.Exit(code=1)
    return item


def _title(connection: sqlite3.Connection, item: QueueItem) -> str:
    try:
        return DraftRepository(connection).get_version(item.draft_version_id).title
    except AiNewsError:  # pragma: no cover - a queued version always exists
        return "(unavailable)"


def _parse_when(value: str, timezone_name: str) -> object:
    try:
        return parse_local(value, now=now_utc(), tz_name=timezone_name)
    except TimeError as exc:
        err_console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1) from exc


@queue_app.command("list")
def queue_list(
    history: Annotated[
        bool,
        typer.Option(
            "--history",
            # Not "--all". That flag is forbidden across this CLI, because it reads as
            # "act on everything" — and nothing here ever acts on everything.
            help="Include cancelled, published and held items, not just what is upcoming.",
        ),
    ] = False,
) -> None:
    """What is scheduled, in the channel's timezone."""
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        repo = PublicationQueueRepository(connection)
        items = repo.list_all(limit=100) if history else repo.list_upcoming(limit=100)
        if not items:
            console.print(
                "[dim]Nothing scheduled.[/dim] Approve a draft, then "
                "'ai-news queue add <draft-id> --at \"13.08 10:00\"'."
            )
            return

        table = Table(title="Publication queue", show_lines=False)
        table.add_column("id", style="dim")
        table.add_column("when")
        table.add_column("status")
        table.add_column("post")

        for item in items:
            local = to_local(item.scheduled_for, item.display_timezone)
            colour = _STATUS_COLOUR.get(item.status, "white")
            table.add_row(
                str(item.id)[:8],
                f"{local:%d %b · %H:%M}",
                f"[{colour}]{item.status.value}[/{colour}]",
                _title(connection, item)[:52],
            )
        console.print(table)
        console.print(f"[dim]Times shown in {items[0].display_timezone}.[/dim]")

        attention = repo.list_needing_attention(limit=10)
        if attention and not history:
            console.print(
                f"\n[yellow]{len(attention)} item(s) need a decision[/yellow] — "
                "'ai-news queue list --history' to see them."
            )
    finally:
        connection.close()


@queue_app.command("show")
def queue_show(
    queue_id: Annotated[str, typer.Argument(help="Queue id, or an unambiguous prefix.")],
) -> None:
    """One queue item in full, with its history."""
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        item = _resolve(connection, queue_id)
        repo = PublicationQueueRepository(connection)
        colour = _STATUS_COLOUR.get(item.status, "white")

        console.print(f"\n[bold]{_title(connection, item)}[/bold]")
        console.print(f"queue id     {item.id}")
        console.print(f"draft        {item.draft_id}")
        console.print(f"version      {item.draft_version_id}")
        console.print(f"scheduled    {describe(item.scheduled_for, item.display_timezone)}")
        console.print(f"status       [{colour}]{item.status.value}[/{colour}]")
        if item.hold_reason:
            console.print(f"reason       {item.hold_reason}")
        if item.claimed_by:
            console.print(f"claimed by   {item.claimed_by}")
        if item.publication_id:
            console.print(f"publication  {item.publication_id}")

        original = repo.first_scheduled_for(item.id)
        if original is not None and original != item.scheduled_for:
            console.print(
                f"[dim]originally scheduled for "
                f"{describe(original, item.display_timezone)}[/dim]"
            )

        console.print("\n[bold]History[/bold]")
        for row in repo.history(item.id):
            detail = f" — {row['detail']}" if row["detail"] else ""
            console.print(f"  {row['created_at'][:16]}  {row['event']:<14} {row['actor']}{detail}")
    finally:
        connection.close()


@queue_app.command("add")
def queue_add(
    draft_id: Annotated[str, typer.Argument(help="An approved draft. Exactly one.")],
    at: Annotated[
        str,
        typer.Option("--at", help='When, in channel time: "13.08 10:00" or "2026-08-13 10:00".'),
    ],
    timezone_name: Annotated[
        str, typer.Option("--timezone", help="Timezone the time is written in.")
    ] = CHANNEL_TIMEZONE,
    anyway: Annotated[
        bool,
        typer.Option(
            "--anyway",
            help="Schedule even though another post holds that exact moment. Deliberate.",
        ),
    ] = False,
) -> None:
    """Schedule one approved draft for a future time.

    Only an approved draft can be scheduled, and this command cannot approve anything.
    It also never picks a draft for you: the id is required and there is no 'all'.
    """
    from ai_news_editor.cli.main import open_migrated_database

    settings = _settings()
    channel = _channel(settings)
    try:
        identifier = UUID(draft_id.strip())
    except ValueError as exc:
        err_console.print(f"[bold red]Not a draft id:[/bold red] {draft_id!r}")
        raise typer.Exit(code=1) from exc

    when = _parse_when(at, timezone_name)
    connection = open_migrated_database()
    try:
        try:
            item, warnings = queue_service.schedule(
                connection,
                identifier,
                when,  # type: ignore[arg-type]
                channel=channel,
                media_root=settings.resolved_media_dir,
                actor="owner:cli",
                timezone_name=timezone_name,
                allow_collision=anyway,
            )
        except (queue_service.QueueError, AiNewsError) as exc:
            err_console.print(f"[bold red]Not scheduled:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc

        console.print(
            f"[bold green]Scheduled[/bold green] for "
            f"{describe(item.scheduled_for, item.display_timezone)} "
            f"(queue item {str(item.id)[:8]})."
        )
        for warning in warnings:
            console.print(f"[yellow]Note:[/yellow] {warning.message}")
        console.print(
            "[dim]The scheduler re-checks approval, freshness and files immediately "
            "before sending. Nothing is guaranteed to publish by being queued.[/dim]"
        )
    finally:
        connection.close()


@queue_app.command("reschedule")
def queue_reschedule(
    queue_id: Annotated[str, typer.Argument(help="Queue id, or an unambiguous prefix.")],
    at: Annotated[str, typer.Option("--at", help='The new time, e.g. "13.08 18:00".')],
    timezone_name: Annotated[
        str, typer.Option("--timezone", help="Timezone the time is written in.")
    ] = CHANNEL_TIMEZONE,
    anyway: Annotated[
        bool, typer.Option("--anyway", help="Accept an exact collision with another post.")
    ] = False,
) -> None:
    """Move a waiting item to a different time."""
    from ai_news_editor.cli.main import open_migrated_database

    when = _parse_when(at, timezone_name)
    connection = open_migrated_database()
    try:
        item = _resolve(connection, queue_id)
        try:
            moved, warnings = queue_service.reschedule(
                connection,
                item.id,
                when,  # type: ignore[arg-type]
                actor="owner:cli",
                timezone_name=timezone_name,
                allow_collision=anyway,
            )
        except (queue_service.QueueError, AiNewsError) as exc:
            err_console.print(f"[bold red]Not rescheduled:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc

        console.print(
            f"[bold green]Moved[/bold green] to "
            f"{describe(moved.scheduled_for, moved.display_timezone)}."
        )
        for warning in warnings:
            console.print(f"[yellow]Note:[/yellow] {warning.message}")
    finally:
        connection.close()


@queue_app.command("cancel")
def queue_cancel(
    queue_id: Annotated[str, typer.Argument(help="Queue id, or an unambiguous prefix.")],
) -> None:
    """Withdraw a schedule. The draft stays approved and can be scheduled again."""
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        item = _resolve(connection, queue_id)
        try:
            queue_service.cancel(connection, item.id, actor="owner:cli")
        except (queue_service.QueueError, AiNewsError) as exc:
            err_console.print(f"[bold red]Not cancelled:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(
            "[bold]Cancelled.[/bold] The draft is still approved — nothing about the "
            "review was undone."
        )
    finally:
        connection.close()


@queue_app.command("policy")
def queue_policy() -> None:
    """The freshness and overdue windows currently in force."""
    table = Table(title="Freshness policy (editorial defaults, not measured optima)")
    table.add_column("content type")
    table.add_column("publishable within of approval")
    table.add_column("may publish this late")
    for content_type, window in DEFAULT_FRESHNESS.items():
        table.add_row(
            content_type.value,
            _duration(window),
            _duration(DEFAULT_OVERDUE_TOLERANCE[content_type]),
        )
    console.print(table)
    console.print(
        "[dim]Past either window the scheduler holds the post for a human rather than "
        "publishing it. Configured in scheduling/freshness.py.[/dim]"
    )


def _duration(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    return f"{hours}h" if hours < 48 else f"{hours // 24}d"


@scheduler_app.command("once")
def scheduler_once(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Assess everything due and send nothing at all."),
    ] = False,
) -> None:
    """One pass over the queue: assess what is due, and act on it.

    ``--dry-run`` runs the identical assessment and makes zero send requests, so what it
    prints is what a real pass would do rather than a description of it.
    """
    from ai_news_editor.cli.main import open_migrated_database

    settings = _settings()
    channel = _channel(settings)
    connection = open_migrated_database()
    try:
        factory = _client_factory(settings)
        moment = now_utc()
        console.print(f"[bold]Now:[/bold] {describe(moment)}\n")

        report = process_once(
            connection,
            worker=_worker_name(),
            channel=channel,
            media_root=settings.resolved_media_dir,
            client_factory=factory,
            now=moment,
            dry_run=dry_run,
        )

        if report.recovered:
            console.print(
                f"[yellow]Recovered {len(report.recovered)} expired claim(s)[/yellow] "
                "from a worker that stopped without finishing."
            )
        if not report.assessed:
            console.print("[dim]Nothing is due.[/dim]")

        for assessment in report.assessed:
            console.print("─" * 72)
            for line in format_assessment(assessment):
                console.print(line)
            console.print()

        if dry_run:
            console.print(
                "[bold]DRY RUN — ZERO Telegram sends.[/bold] Nothing was claimed and "
                "nothing was published."
            )
        else:
            console.print(
                f"[bold]Pass complete.[/bold] published: {len(report.published)}, "
                f"held for review: {len(report.held)}, "
                f"claimed elsewhere: {len(report.skipped_not_claimed)}."
            )
    finally:
        connection.close()


@scheduler_app.command("run")
def scheduler_run(
    interval: Annotated[
        int, typer.Option("--interval", help="Seconds between passes.", min=5)
    ] = int(DEFAULT_POLL_INTERVAL.total_seconds()),
) -> None:
    """Run the scheduler until stopped with Ctrl-C.

    A long-running local process, and nothing more than that: no server, no webhook, no
    hosting. It publishes only items the owner queued, and only when every check still
    passes. Ctrl-C stops it between passes rather than mid-send.
    """
    from ai_news_editor.cli.main import open_migrated_database

    settings = _settings()
    channel = _channel(settings)
    connection = open_migrated_database()
    try:
        console.print(
            f"[bold]Scheduler running.[/bold] Channel {channel}, "
            f"checking every {interval}s. Ctrl-C to stop.\n"
            "[dim]This machine must stay awake for scheduled posts to go out on time. "
            "If it sleeps, overdue items are held for review rather than published "
            "late.[/dim]\n"
        )

        def report_pass(report: object) -> None:
            published = len(getattr(report, "published", []))
            held = len(getattr(report, "held", []))
            if published or held:
                console.print(
                    f"[dim]{describe(now_utc())}[/dim] published {published}, held {held}"
                )

        passes = run(
            connection,
            worker=_worker_name(),
            channel=channel,
            media_root=settings.resolved_media_dir,
            client_factory=_client_factory(settings),
            poll_interval=timedelta(seconds=interval),
            on_pass=report_pass,
        )
        console.print(f"\n[bold]Stopped[/bold] after {passes} pass(es).")
    finally:
        connection.close()


def _worker_name() -> str:
    """Identifies the claiming process in the queue's history. Never a secret."""
    import os
    import socket

    return f"{socket.gethostname().split('.')[0]}:{os.getpid()}"


def _client_factory(settings: Settings):
    """Builds Telegram clients on demand, so a dry run opens no connection.

    The token is read here and handed straight to the client. It is never logged, never
    stored, and never part of a queue record.
    """
    from ai_news_editor.publishing.telegram import TelegramClient

    if settings.telegram_bot_token is None:
        return None
    token = settings.telegram_bot_token.get_secret_value()

    def build() -> TelegramClient:
        return TelegramClient(token)

    return build
