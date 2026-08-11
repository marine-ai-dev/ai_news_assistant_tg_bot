"""The ``ai-news review`` command group.

An interactive loop over drafts awaiting a human. Every rule lives in
:mod:`review.service` and :mod:`publishing.gate`; this module reads keys and draws
panels.

Approval requires typing the word APPROVE. There is no ``--yes``, no ``--approve-all``
and no way to approve from a score — not because the flags were forgotten, but because
the whole product rests on approval being a thing a person did deliberately.
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

from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    ContentType,
    DraftStatus,
    ReviewAction,
)
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.publishing.gate import approve_draft, authorization_for_approved_draft
from ai_news_editor.review.editing import EditorError, edit_text
from ai_news_editor.review.service import (
    ReviewError,
    ReviewItem,
    apply_edit,
    length_note,
    reject_draft,
    request_rewrite,
    review_history,
    review_queue,
    status_counts,
    validate_edit,
)
from ai_news_editor.storage.repositories import DraftRepository

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="review",
    help="Read drafts awaiting review and approve, edit, reject or send them back.",
    invoke_without_command=True,
)

#: The exact word that approves a post. A bare Enter must never be enough.
APPROVE_WORD = "APPROVE"


@app.callback(invoke_without_command=True)
def review(
    ctx: typer.Context,
    draft: Annotated[
        str | None, typer.Option("--draft", "-d", help="Review only this draft id.")
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="Review only this category.")
    ] = None,
) -> None:
    """Walk through drafts awaiting review, one at a time."""
    if ctx.invoked_subcommand is not None:
        return

    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        _run_loop(connection, draft_id=_uuid(draft), category=_category(category))
    finally:
        connection.close()


def _run_loop(
    connection: sqlite3.Connection, *, draft_id: UUID | None, category: Category | None
) -> None:
    try:
        queue = review_queue(connection, draft_id=draft_id, category=category)
    except AiNewsError as exc:
        err_console.print(f"[bold red]Could not read the queue:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc

    if not queue:
        console.print("[green]Nothing awaiting review.[/green]")
        console.print("[dim]Write drafts with 'ai-news draft export' first.[/dim]")
        return

    position = 0
    total = len(queue)
    while position < total:
        item = queue[position]
        # Re-read: the draft may have changed since the queue was built.
        try:
            fresh = review_queue(connection, draft_id=item.draft.id)
        except AiNewsError:
            fresh = []
        if not fresh:
            console.print(
                f"[dim]Draft {str(item.draft.id)[:8]} is no longer awaiting review.[/dim]"
            )
            position += 1
            continue
        item = fresh[0]

        _render_item(item, position + 1, total, connection)
        choice = _ask_action()

        if choice == "q":
            console.print("[dim]Stopped. The remaining drafts stay as they are.[/dim]")
            break
        if choice in {"s", "n"}:
            # Navigation only: nothing is recorded and nothing changes.
            position += 1
            continue
        if choice == "a":
            if _do_approve(connection, item):
                position += 1
            continue
        if choice == "r":
            if _do_reject(connection, item):
                position += 1
            continue
        if choice == "w":
            if _do_rewrite(connection, item):
                position += 1
            continue
        if choice == "e":
            _do_edit(connection, item)
            continue

    _print_summary(connection)


# --- rendering --------------------------------------------------------------


def _render_item(
    item: ReviewItem, position: int, total: int, connection: sqlite3.Connection
) -> None:
    console.print()
    console.rule(f"[bold]DRAFT {position} / {total}[/bold]", style="cyan")

    meta = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    meta.add_column("", style="dim", no_wrap=True)
    meta.add_column("")
    meta.add_row("Status", f"[yellow]{item.draft.status.value}[/yellow]")
    meta.add_row("Type", _content_type_markup(item.draft.content_type))
    meta.add_row("Version", str(item.version.version_no))
    meta.add_row("Category", item.version.category.value)
    meta.add_row("Audience", _audience_markup(item.version.audience))
    if item.version.post_format:
        note = length_note(item)
        length = f"{len(item.rendered_post)} chars, {item.version.post_format.value}"
        meta.add_row("Format", length + (f"  [yellow]({note})[/yellow]" if note else ""))
    if item.score is not None:
        meta.add_row("Editorial score", f"{item.score:.1f}")
    if item.article is not None:
        meta.add_row("Source", f"{item.article.source_id}\n{item.article.canonical_url}")
    elif item.content_item is not None:
        # Editorial-original: say so plainly rather than leaving the row blank, which
        # would read as a missing source rather than an absent one.
        label = "Topic" if item.draft.content_type is ContentType.PROMPT else "Concept"
        meta.add_row(label, item.subject or "-")
        meta.add_row("Origin", "written by the channel (no external source)")
        if item.content_item.references:
            meta.add_row(
                "Checked against",
                "\n".join(f"{r.label} — {r.url}" for r in item.content_item.references),
            )
    console.print(meta)

    console.print(
        Panel(
            item.rendered_post,
            title="TELEGRAM PREVIEW",
            title_align="left",
            border_style="white",
        )
    )

    if item.version.writer_notes:
        console.print("[bold]Writer notes[/bold] [dim](internal, never published)[/dim]")
        for note in item.version.writer_notes:
            console.print(f"  • {note}")

    history = review_history(connection, item.draft.id)
    if history:
        console.print("[bold]History[/bold]")
        for decision in history:
            suffix = f" — {decision.note}" if decision.note else ""
            console.print(
                f"  [dim]{decision.created_at:%Y-%m-%d %H:%M}[/dim] "
                f"{decision.action.value} (v{_version_no(connection, decision.draft_version_id)})"
                f"{suffix}"
            )


def _ask_action() -> str:
    console.print(
        "\n[bold green]\\[A][/bold green] Approve   "
        "[bold]\\[E][/bold] Edit   "
        "[bold red]\\[R][/bold red] Reject   "
        "[bold yellow]\\[W][/bold yellow] Needs rewrite   "
        "[dim]\\[S] Skip   \\[N] Next   \\[Q] Quit[/dim]"
    )
    answer = Prompt.ask("Action", default="n", show_default=False).strip().lower()
    return answer[:1] if answer else "n"


# --- actions ----------------------------------------------------------------


def _do_approve(connection: sqlite3.Connection, item: ReviewItem) -> bool:
    """Approve after an explicit typed confirmation. Returns whether it happened."""
    console.print(
        f"\n[bold]Approving draft {str(item.draft.id)[:8]} "
        f"version {item.version.version_no}.[/bold]"
    )
    console.print(
        f"[dim]Type {APPROVE_WORD} to confirm. Anything else — including Enter — cancels.[/dim]"
    )
    typed = Prompt.ask("Confirm", default="", show_default=False)

    if typed.strip() != APPROVE_WORD:
        console.print("[yellow]Not approved.[/yellow]")
        return False

    note = Prompt.ask("Note (optional)", default="", show_default=False).strip() or None

    try:
        authorization = approve_draft(
            connection,
            item.draft.id,
            note=note,
            expected_version_id=item.version.id,
        )
    except AiNewsError as exc:
        err_console.print(f"[bold red]Approval refused:[/bold red] {exc}")
        return False

    console.print(
        f"[green]Approved[/green] version {authorization.version_no}. "
        "[dim]Approved is not published — there is no publisher yet.[/dim]"
    )
    return True


def _do_reject(connection: sqlite3.Connection, item: ReviewItem) -> bool:
    reason = Prompt.ask("Reason (optional)", default="", show_default=False).strip() or None
    try:
        reject_draft(
            connection, item.draft.id, note=reason, expected_version_id=item.version.id
        )
    except AiNewsError as exc:
        err_console.print(f"[bold red]Could not reject:[/bold red] {exc}")
        return False
    console.print("[red]Rejected.[/red] [dim]Kept in the database with its history.[/dim]")
    return True


def _do_rewrite(connection: sqlite3.Connection, item: ReviewItem) -> bool:
    console.print("[dim]What should change? This is what a rewrite will work from.[/dim]")
    reason = Prompt.ask("Reason", default="", show_default=False).strip() or None
    try:
        request_rewrite(
            connection, item.draft.id, note=reason, expected_version_id=item.version.id
        )
    except AiNewsError as exc:
        err_console.print(f"[bold red]Could not mark for rewrite:[/bold red] {exc}")
        return False
    console.print("[yellow]Marked as needing a rewrite.[/yellow]")
    return True


def _do_edit(connection: sqlite3.Connection, item: ReviewItem) -> None:
    try:
        result = edit_text(item.version.title, item.version.body)
    except EditorError as exc:
        err_console.print(f"[yellow]Edit cancelled:[/yellow] {exc}")
        return

    if not result.changed:
        console.print("[dim]No changes — the draft is untouched.[/dim]")
        return

    problem = validate_edit(
        headline=result.headline,
        body=result.body,
        source_label="check",
        source_url=item.version.source_url or item.article.canonical_url,
    )
    if problem:
        err_console.print(f"[bold red]Edit rejected:[/bold red] {problem}")
        err_console.print("[dim]Nothing was saved. Choose E again to retry.[/dim]")
        return

    try:
        _draft, version = apply_edit(
            connection,
            item.draft.id,
            headline=result.headline,
            body=result.body,
            expected_version_id=item.version.id,
        )
    except (ReviewError, AiNewsError) as exc:
        err_console.print(f"[bold red]Edit rejected:[/bold red] {exc}")
        return

    console.print(
        f"[green]Saved as version {version.version_no}.[/green] "
        "[dim]Version 1 is kept. Any earlier approval no longer applies — "
        "this version needs its own.[/dim]"
    )


# --- read-only views --------------------------------------------------------


@app.command("status")
def review_status() -> None:
    """Show how many drafts sit in each state."""
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        counts = status_counts(connection)
        approved = DraftRepository(connection).list_by_status(DraftStatus.APPROVED, limit=50)
        approved_rows = [
            (draft, DraftRepository(connection).current_version(draft.id)) for draft in approved
        ]
    finally:
        connection.close()

    table = Table(title="Drafts")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for status in DraftStatus:
        count = counts.get(status.value, 0)
        if count:
            table.add_row(status.value, str(count))
    if not counts:
        table.add_row("[dim]none[/dim]", "0")
    console.print(table)

    for draft, version in approved_rows:
        console.print(
            f"[green]approved[/green] {str(draft.id)[:8]} v{version.version_no} — {version.title}"
        )

    console.print(
        "[dim]APPROVED means a human approved that exact version. It does not mean "
        "published: there is no publisher yet.[/dim]"
    )


@app.command("history")
def show_history(
    draft_id: Annotated[str, typer.Argument(help="Draft id, or its first characters.")],
) -> None:
    """Show every version of a draft and every human decision on it."""
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        repo = DraftRepository(connection)
        match = next(
            (d for d in repo.list_all(limit=500) if str(d.id).startswith(draft_id.lower())), None
        )
        if match is None:
            err_console.print(f"No draft matching [bold]{draft_id}[/bold].")
            raise typer.Exit(code=1)

        versions = repo.list_versions(match.id)
        decisions = review_history(connection, match.id)
        authorization = authorization_for_approved_draft(connection, match.id)
    finally:
        connection.close()

    console.print(
        f"\n[bold]Draft {str(match.id)[:8]}[/bold]   status [yellow]{match.status.value}[/yellow]"
    )

    by_version: dict[str, list] = {}
    for decision in decisions:
        by_version.setdefault(str(decision.draft_version_id), []).append(decision)

    for version in versions:
        current = " [cyan](current)[/cyan]" if version.id == match.current_version_id else ""
        console.print(
            f"\n  [bold]Version {version.version_no}[/bold]{current}   "
            f"[dim]{version.created_at:%Y-%m-%d %H:%M} by {version.created_by}[/dim]"
        )
        console.print(f"    {version.title}")
        for decision in by_version.get(str(version.id), []):
            suffix = f" — {decision.note}" if decision.note else ""
            colour = {
                ReviewAction.APPROVE: "green",
                ReviewAction.REJECT: "red",
                ReviewAction.REQUEST_REWRITE: "yellow",
            }.get(decision.action, "white")
            console.print(
                f"    [{colour}]{decision.action.value}[/{colour}] "
                f"[dim]{decision.created_at:%Y-%m-%d %H:%M} by {decision.actor}[/dim]{suffix}"
            )
        if version.id != match.current_version_id:
            console.print("    [dim]superseded by a later version[/dim]")

    if authorization:
        console.print(
            f"\n  [green]Publication authorization is valid[/green] for version "
            f"{authorization.version_no}, approved by {authorization.approved_by}."
        )
    else:
        console.print("\n  [dim]No valid publication authorization.[/dim]")


def _print_summary(connection: sqlite3.Connection) -> None:
    counts = status_counts(connection)
    parts = [
        f"{counts.get(status.value, 0)} {status.value.lower().replace('_', ' ')}"
        for status in (
            DraftStatus.PENDING_REVIEW,
            DraftStatus.APPROVED,
            DraftStatus.NEEDS_REWRITE,
            DraftStatus.REJECTED,
        )
        if counts.get(status.value, 0)
    ]
    console.print(f"\n[dim]{' · '.join(parts) or 'no drafts'}[/dim]")


def _version_no(connection: sqlite3.Connection, version_id: UUID) -> int | str:
    try:
        return DraftRepository(connection).get_version(version_id).version_no
    except AiNewsError:  # pragma: no cover - a decision always names a real version
        return "?"


def _uuid(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError as exc:
        err_console.print(f"[bold red]Not a draft id:[/bold red] {value}")
        raise typer.Exit(code=2) from exc


def _category(value: str | None) -> Category | None:
    if value is None:
        return None
    try:
        return Category(value.upper())
    except ValueError as exc:
        err_console.print(f"[bold red]Unknown category:[/bold red] {value}")
        raise typer.Exit(code=2) from exc


def _content_type_markup(content_type: ContentType) -> str:
    """Colour by format, so the kind of post registers before the text is read."""
    colour = {
        ContentType.NEWS: "cyan",
        ContentType.PROMPT: "magenta",
        ContentType.EXPLAINER: "green",
    }[content_type]
    return f"[{colour}]{content_type.value}[/{colour}]"


def _audience_markup(audience: AudienceTier) -> str:
    """NEWCOMER is highlighted: it is the level with the strictest jargon rules."""
    if audience is AudienceTier.NEWCOMER:
        return f"[bold]{audience.value}[/bold] [dim](assume no AI knowledge)[/dim]"
    return audience.value
