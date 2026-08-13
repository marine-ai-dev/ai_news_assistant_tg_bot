"""The ``ai-news publish``, ``ai-news publication`` and ``ai-news telegram`` commands.

Publication is the second of two separate human decisions. The first — *is this good
enough to run?* — happened in ``ai-news review`` and is recorded as an approval. This
one is *send it now, to this channel*, and it is asked separately because it is the
decision that becomes visible to strangers.

So: one draft id per command, the destination on screen before anything is asked, and
the literal word PUBLISH. No ``--all``, no ``--yes``, no threshold.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.publishing.message import telegram_length
from ai_news_editor.publishing.plan import describe
from ai_news_editor.publishing.service import (
    PublicationPlan,
    approved_drafts,
    prepare_publication,
    publication_history,
    publish_draft,
    recent_publications,
)
from ai_news_editor.publishing.telegram import TelegramClient, TelegramPublisher
from ai_news_editor.settings import Settings, get_settings

console = Console()
err_console = Console(stderr=True)

#: The exact word that sends a post to a real audience. Not "y". Not Enter.
PUBLISH_WORD = "PUBLISH"

publication_app = typer.Typer(name="publication", help="Read the publication log.")
telegram_app = typer.Typer(name="telegram", help="Telegram diagnostics. Read-only.")


def _settings() -> Settings:
    try:
        return get_settings()
    except AiNewsError as exc:
        err_console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


def _require_telegram(settings: Settings) -> tuple[str, str]:
    """Token and channel, or a clear explanation of what is missing."""
    missing = []
    if settings.telegram_bot_token is None:
        missing.append("AI_NEWS_TELEGRAM_BOT_TOKEN")
    if not settings.telegram_channel:
        missing.append("AI_NEWS_TELEGRAM_CHANNEL")
    if missing:
        err_console.print(
            f"[bold red]Not configured:[/bold red] {', '.join(missing)} is not set.\n"
            "Create a bot with @BotFather, add it to your channel as an administrator "
            "with 'Post Messages', then put both values in .env. See the README."
        )
        raise typer.Exit(code=2)
    assert settings.telegram_bot_token is not None and settings.telegram_channel is not None
    return settings.telegram_bot_token.get_secret_value(), settings.telegram_channel


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        err_console.print(f"[bold red]Not a draft id:[/bold red] {value}")
        raise typer.Exit(code=2) from exc


# ---------------------------------------------------------------------------
# telegram doctor
# ---------------------------------------------------------------------------


@telegram_app.command("doctor")
def telegram_doctor() -> None:
    """Check the Telegram setup without sending anything.

    Read-only by construction: it calls getMe, getChat and getChatMember and nothing
    else. It will not post a test message — a diagnostic that publishes is not a
    diagnostic.

    Where the API cannot answer a question, it says so rather than reporting success.
    """
    settings = _settings()
    token, channel = _require_telegram(settings)

    table = Table(title="Telegram check")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")

    ok = True
    with TelegramClient(token) as client:
        table.add_row("Token configured", "[green]OK[/green]", "read from the environment")

        try:
            identity = client.get_me()
        except AiNewsError as exc:
            table.add_row("getMe", "[red]FAIL[/red]", str(exc))
            console.print(table)
            raise typer.Exit(code=1) from exc
        handle = f"@{identity.username}" if identity.username else identity.first_name
        table.add_row("getMe", "[green]OK[/green]", f"{handle} (id {identity.id})")

        try:
            chat = client.get_chat(channel)
        except AiNewsError as exc:
            table.add_row("Destination resolves", "[red]FAIL[/red]", str(exc))
            console.print(table)
            err_console.print(
                "\nA bot can only resolve a chat it has been added to. Add "
                f"{handle} to {channel} first."
            )
            raise typer.Exit(code=1) from exc

        label = chat.title or (f"@{chat.username}" if chat.username else str(chat.id))
        table.add_row("Destination resolves", "[green]OK[/green]", f"{label} (id {chat.id})")
        if chat.postable:
            table.add_row("Destination type", "[green]OK[/green]", f"{chat.type}, postable")
        else:
            ok = False
            table.add_row("Destination type", "[red]FAIL[/red]", f"{chat.type} is not postable")

        rights = client.get_posting_rights(channel, identity.id)
        if rights.can_post is True:
            table.add_row("Posting rights", "[green]OK[/green]", rights.detail)
        elif rights.can_post is False:
            ok = False
            table.add_row("Posting rights", "[red]FAIL[/red]", rights.detail)
        else:
            table.add_row("Posting rights", "[yellow]UNKNOWN[/yellow]", rights.detail)

    console.print(table)
    console.print(
        f"\nDestination: [bold]{channel}[/bold]. Nothing was sent — this command only reads."
    )
    if not ok:
        raise typer.Exit(code=1)


@telegram_app.command("whoami")
def telegram_whoami() -> None:
    """Print the Telegram user id of whoever messages the bot next.

    The review bot must know which single account may use it, and that id is not
    discoverable from the token. This waits for one message, prints the sender's id, and
    exits — no editorial data is read and nothing is stored.
    """
    from ai_news_editor.bot.api import BotApi, parse_update
    from ai_news_editor.bot.review_bot import discard_backlog

    settings = _settings()
    if settings.telegram_bot_token is None:
        err_console.print(
            "[bold red]Not configured:[/bold red] AI_NEWS_TELEGRAM_BOT_TOKEN is not set."
        )
        raise typer.Exit(code=2)

    console.print(
        "Open Telegram, find your bot, and send it any message.\n"
        "[dim]Waiting… Ctrl+C to stop.[/dim]"
    )
    with TelegramClient(settings.telegram_bot_token.get_secret_value()) as client:
        api = BotApi(client)
        offset = discard_backlog(api)
        try:
            while True:
                for update in api.get_updates(offset):
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    parsed = parse_update(update)
                    if parsed is None:
                        continue
                    console.print(
                        f"\nYour Telegram user id: [bold]{parsed.user_id}[/bold]\n\n"
                        "Put it in [bold].env[/bold]:\n"
                        f"  AI_NEWS_TELEGRAM_OWNER_USER_ID={parsed.user_id}"
                    )
                    return
        except KeyboardInterrupt:
            console.print("\nStopped. Nothing was changed.")


@telegram_app.command("review-bot")
def telegram_review_bot() -> None:
    """Run the private review bot until interrupted.

    Long polling, so no public URL, no webhook and no hosting: it works for as long as
    this process is running on this machine.

    The bot can approve, reject, request a rewrite and edit — all through the same
    review service the terminal uses. It cannot publish.
    """
    from ai_news_editor.bot.api import BotApi
    from ai_news_editor.bot.review_bot import (
        ReviewBot,
        discard_backlog,
        pending_summary,
        poll,
    )
    from ai_news_editor.bot.session import Session
    from ai_news_editor.cli.main import open_migrated_database

    settings = _settings()
    if settings.telegram_bot_token is None:
        err_console.print(
            "[bold red]Not configured:[/bold red] AI_NEWS_TELEGRAM_BOT_TOKEN is not set."
        )
        raise typer.Exit(code=2)
    if settings.telegram_owner_user_id is None:
        err_console.print(
            "[bold red]Not configured:[/bold red] AI_NEWS_TELEGRAM_OWNER_USER_ID is not "
            "set.\nFind your id with [bold]ai-news telegram whoami[/bold], then put it "
            "in .env."
        )
        raise typer.Exit(code=2)

    connection = open_migrated_database()
    try:
        counts = pending_summary(connection)
        with TelegramClient(settings.telegram_bot_token.get_secret_value()) as client:
            api = BotApi(client)
            identity = client.get_me()
            offset = discard_backlog(api)

            console.print(
                f"Review bot running as [bold]@{identity.username or identity.first_name}"
                f"[/bold].\n"
                f"Awaiting review: [bold]{counts['pending']}[/bold]  ·  "
                f"approved {counts['approved']}  ·  published {counts['published']}\n"
                "Send [bold]/review[/bold] to the bot in Telegram. Ctrl+C to stop.\n"
                "[dim]Approving here does not publish anything.[/dim]"
            )

            bot = ReviewBot(
                api=api,
                connection=connection,
                owner_id=settings.telegram_owner_user_id,
                session=Session(),
                channel=settings.telegram_channel,
                media_root=settings.resolved_media_dir,
            )
            try:
                for _update_id in poll(bot, offset=offset):
                    pass
            except KeyboardInterrupt:
                console.print("\nStopped. Every decision already made is saved.")
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def publish(
    draft_id: Annotated[
        str, typer.Argument(help="The approved draft to publish. Exactly one.")
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate and show the exact payload. Sends nothing."),
    ] = False,
) -> None:
    """Publish one approved draft to the configured Telegram channel.

    Requires a draft a human approved in ``ai-news review``, and then a second explicit
    confirmation here. One draft per invocation, named explicitly — this command never
    goes looking for something to publish.
    """
    from ai_news_editor.cli.main import open_migrated_database

    settings = _settings()
    token, channel = _require_telegram(settings)
    identifier = _parse_uuid(draft_id)

    connection = open_migrated_database()
    try:
        # Discussion group discovery is read-only and happens before anything is sent:
        # a comment has nowhere to go without one, and that must be visible in the plan
        # rather than discovered halfway through a publication.
        discussion_chat_id: int | None = None
        with TelegramClient(token) as probe:
            try:
                discussion_chat_id = probe.linked_discussion_chat(channel)
            except AiNewsError as exc:
                err_console.print(f"[yellow]Could not check for a discussion group:[/yellow] {exc}")

        try:
            plan = prepare_publication(
                connection,
                identifier,
                channel=channel,
                media_root=settings.resolved_media_dir,
                discussion_chat_id=discussion_chat_id,
            )
        except AiNewsError as exc:
            err_console.print(f"[bold red]Not publishable:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc
        except ValueError as exc:  # message too long
            err_console.print(f"[bold red]Not publishable:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc

        _show_plan(plan)

        if plan.already_published is not None:
            console.print(
                f"\n[yellow]Already published[/yellow] to {channel} as message "
                f"{plan.already_published.message_id} on "
                f"{_when(plan.already_published.published_at)}. Nothing to do."
            )
            return
        if plan.unresolved is not None:
            err_console.print(
                f"\n[bold red]An earlier attempt has an unknown outcome[/bold red] "
                f"(publication {plan.unresolved.id}). It may already be on the channel.\n"
                "Check the channel yourself before trying again — this will not resend."
            )
            raise typer.Exit(code=1)

        if dry_run:
            console.print(
                "\n[bold]Dry run.[/bold] The approval was verified and the plan above is "
                "exactly what a real run would do. No Telegram send request was made."
            )
            return

        if not _confirm(channel):
            console.print("[yellow]Not published.[/yellow] Nothing was sent.")
            return

        with TelegramClient(token) as client:
            publisher = TelegramPublisher(client, channel)
            try:
                publication = publish_draft(connection, plan, publisher)
            except AiNewsError as exc:
                err_console.print(f"\n[bold red]Publication failed:[/bold red] {exc}")
                raise typer.Exit(code=1) from exc

        console.print(
            f"\n[bold green]Published.[/bold green] Message {publication.message_id} in "
            f"{publication.chat_id or channel} at {_when(publication.published_at)}."
        )
    finally:
        connection.close()


@publication_app.command("approved")
def publication_approved() -> None:
    """Show drafts a human has approved, which are the only publishable ones."""
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        drafts = approved_drafts(connection)
        if not drafts:
            console.print(
                "No approved drafts. Approve one first with [bold]ai-news review[/bold]."
            )
            return
        table = Table(title="Approved and publishable")
        table.add_column("Draft id")
        table.add_column("Approved at")
        for draft in drafts:
            table.add_row(str(draft.id), draft.updated_at.strftime("%Y-%m-%d %H:%M"))
        console.print(table)
        console.print("\nPublish one with [bold]ai-news publish <draft-id>[/bold].")
    finally:
        connection.close()


def _confirm(channel: str) -> bool:
    """Require the literal word. A bare Enter, or 'y', must not publish."""
    console.print(
        f"\nThis sends the post above to [bold]{channel}[/bold], where other people "
        "will see it.\nIt cannot be unsent by this application."
    )
    typed = Prompt.ask(f"Type {PUBLISH_WORD} to publish", default="", show_default=False)
    return typed.strip() == PUBLISH_WORD


def _show_plan(plan: PublicationPlan) -> None:
    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim")
    meta.add_column()
    meta.add_row("Draft", str(plan.draft.id))
    meta.add_row("Version", f"{plan.version.version_no} ({plan.version.content_hash[:12]}…)")
    meta.add_row("Approved by", plan.authorization.approved_by)
    meta.add_row("Approved at", _when(plan.authorization.approved_at))
    meta.add_row("Channel", f"[bold]{plan.channel}[/bold]")
    meta.add_row("Parse mode", plan.message.parse_mode or "plain text")
    meta.add_row("Length", f"{telegram_length(plan.message.payload_text)} / 4096")
    if plan.version.media:
        meta.add_row("Media", f"{len(plan.version.media)} asset(s)")
    if plan.version.comment_text:
        meta.add_row("Comment", f"{len(plan.version.comment_text)} chars")
    meta.add_row(
        "Discussion group",
        str(plan.discussion_chat_id) if plan.discussion_chat_id else "none linked",
    )

    console.rule("FINAL PREVIEW")
    console.print(meta)
    console.print(Panel(plan.message.approved_text, title="EXACTLY WHAT WILL BE SENT"))

    if plan.version.comment_text:
        console.print(
            Panel(plan.version.comment_text, title="COMMENT TO PUBLISH WITH THE POST")
        )

    if plan.bundle_plan is not None:
        console.print("\n[bold]PUBLICATION PLAN[/bold]")
        for line in describe(plan.bundle_plan):
            console.print(f"  {line}")
        for warning in plan.bundle_plan.warnings:
            console.print(f"  [yellow]note:[/yellow] {warning}")
        if plan.bundle_plan.deferred:
            console.print(
                "  [yellow]Some parts cannot be sent yet.[/yellow] The rest will publish; "
                "the deferred parts are recorded, not dropped."
            )


def _when(value: object) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else "-"


# ---------------------------------------------------------------------------
# publication list
# ---------------------------------------------------------------------------


@publication_app.command("list")
def publication_list(
    draft_id: Annotated[
        str | None, typer.Option("--draft", "-d", help="Only this draft's attempts.")
    ] = None,
) -> None:
    """Show publication attempts — the successes, the failures and the unresolved ones."""
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        records = (
            publication_history(connection, _parse_uuid(draft_id))
            if draft_id
            else recent_publications(connection)
        )
        if not records:
            console.print("Nothing has been published yet.")
            return

        table = Table(title="Publications")
        table.add_column("Draft")
        table.add_column("Ver", justify="right")
        table.add_column("Channel")
        table.add_column("Status")
        table.add_column("Message id", justify="right")
        table.add_column("When")
        for record in records:
            table.add_row(
                str(record.draft_id)[:8],
                str(_version_no(connection, record.draft_version_id)),
                record.channel,
                _status_markup(record.status.value),
                str(record.message_id or "-"),
                _when(record.published_at or record.created_at),
            )
        console.print(table)

        unresolved = [r for r in records if r.status.value == "UNCERTAIN"]
        if unresolved:
            console.print(
                f"\n[yellow]{len(unresolved)} attempt(s) with an unknown outcome.[/yellow] "
                "Check the channel — the post may or may not be there."
            )
    finally:
        connection.close()


def _status_markup(status: str) -> str:
    colour = {"SUCCEEDED": "green", "FAILED": "red", "UNCERTAIN": "yellow"}[status]
    return f"[{colour}]{status}[/{colour}]"


def _version_no(connection: sqlite3.Connection, version_id: UUID) -> int | str:
    row = connection.execute(
        "SELECT version_no FROM draft_versions WHERE id = ?", (str(version_id),)
    ).fetchone()
    return int(row["version_no"]) if row else "?"
