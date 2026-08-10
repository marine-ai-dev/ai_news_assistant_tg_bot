"""The ``ai-news draft`` command group.

Export writing assignments, validate and import finished drafts, and read them back.
There is deliberately no approve, reject or publish command: a draft here is text
waiting for a human, and the review interface is a later phase.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ai_news_editor.domain.enums import Category, DraftStatus
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    DraftRepository,
    EvaluationRepository,
)
from ai_news_editor.writing.export import build_writing_batch
from ai_news_editor.writing.format import FORMAT_TARGETS, check_length, render_post
from ai_news_editor.writing.import_results import (
    DraftImportError,
    import_drafts,
    load_drafts,
    validate_against_database,
)

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="draft",
    help="Export writing assignments, import finished drafts, preview them.",
    no_args_is_help=True,
)

DEFAULT_WORK_DIR = Path("writing_work")


@app.command("export")
def export_assignments(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Maximum assignments.")] = 5,
    article: Annotated[
        list[str] | None,
        typer.Option("--article", "-a", help="Write only this article id. Repeatable."),
    ] = None,
    category: Annotated[
        list[str] | None, typer.Option("--category", "-c", help="Restrict to this category.")
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Where to write the assignment JSON.")
    ] = None,
) -> None:
    """Write a batch of writing assignments.

    Only shortlisted stories with a current evaluation and no existing draft are
    eligible. Rejected and held stories produce nothing — that is enforced in the
    domain, not by this command.
    """
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        batch, skipped = build_writing_batch(
            connection,
            limit=limit,
            article_ids=[UUID(value) for value in article] if article else None,
            categories=[Category(value.upper()) for value in category] if category else None,
        )
    except (AiNewsError, ValueError) as exc:
        err_console.print(f"[bold red]Export failed:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    finally:
        connection.close()

    for _, reason in skipped[:10]:
        console.print(f"[dim]skipped: {reason}[/dim]")

    if not batch.assignments:
        console.print(
            "[yellow]Nothing eligible to write.[/yellow] Shortlist stories first, or every "
            "shortlisted story already has a draft."
        )
        raise typer.Exit(code=0)

    path = output or (DEFAULT_WORK_DIR / f"{batch.batch_id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(batch.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    console.print(f"\n[bold]Writing batch[/bold] [cyan]{batch.batch_id}[/cyan]")
    console.print(f"Wrote [bold]{len(batch.assignments)}[/bold] assignments to [bold]{path}[/bold]")

    table = Table()
    table.add_column("Story", overflow="fold")
    table.add_column("Category", no_wrap=True)
    table.add_column("Format", no_wrap=True)
    table.add_column("Source check", no_wrap=True)
    for assignment in batch.assignments:
        table.add_row(
            assignment.original_title[:60],
            assignment.evaluation.category.value,
            assignment.suggested_format.value,
            "needed" if assignment.needs_source_check else "-",
        )
    console.print(table)
    console.print(
        "[dim]Next: write them following docs/telegram_style_guide.md, then "
        "'ai-news draft validate' and 'ai-news draft import'.[/dim]"
    )


@app.command("validate")
def validate_drafts(
    path: Annotated[Path, typer.Argument(help="Draft batch JSON to check.")],
) -> None:
    """Check a draft batch without writing anything."""
    from ai_news_editor.cli.main import open_migrated_database

    try:
        batch = load_drafts(path)
    except DraftImportError as exc:
        _render_problems(exc.problems)
        raise typer.Exit(code=1) from exc

    connection = open_migrated_database()
    try:
        problems, warnings = validate_against_database(connection, batch)
    finally:
        connection.close()

    if problems:
        _render_problems(problems)
        raise typer.Exit(code=1)

    console.print(f"[green]Valid.[/green] {len(batch.drafts)} draft(s) in {batch.batch_id}")
    for draft in batch.drafts:
        length = check_length(draft.rendered_text, draft.post_format)
        low, high = FORMAT_TARGETS[draft.post_format]
        mark = "[green]✓[/green]" if length.within_target else "[yellow]![/yellow]"
        console.print(
            f"  {mark} {draft.post_format.value:<10} {length.chars:>5} chars "
            f"[dim](target {low}-{high})[/dim]  {draft.headline[:50]}"
        )
    for warning in warnings:
        console.print(f"[yellow]note:[/yellow] {warning}")


@app.command("import")
def import_batch(
    path: Annotated[Path, typer.Argument(help="Draft batch JSON to import.")],
) -> None:
    """Validate a draft batch and store Draft + DraftVersion records.

    All-or-nothing. Every draft lands in PENDING_REVIEW; nothing is approved.
    """
    from ai_news_editor.cli.main import open_migrated_database

    try:
        batch = load_drafts(path)
    except DraftImportError as exc:
        _render_problems(exc.problems)
        raise typer.Exit(code=1) from exc

    connection = open_migrated_database()
    try:
        report = import_drafts(connection, batch)
    except DraftImportError as exc:
        _render_problems(exc.problems)
        raise typer.Exit(code=1) from exc
    finally:
        connection.close()

    console.print(f"\n[bold]Imported batch[/bold] [cyan]{report.batch_id}[/cyan]")
    console.print(f"Drafts created: [bold]{report.created}[/bold]")
    if report.already_present:
        console.print(f"Already had a draft: {report.already_present}")
    for warning in report.warnings:
        console.print(f"[yellow]note:[/yellow] {warning}")
    console.print(
        "[dim]All drafts are PENDING_REVIEW. Nothing is approved and nothing is "
        "published — a human still has to read and approve every post.[/dim]"
    )


@app.command("list")
def list_drafts(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many to show.")] = 20,
) -> None:
    """List stored drafts and their status."""
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        repo = DraftRepository(connection)
        rows = []
        for draft in repo.list_all(limit=limit):
            version = repo.current_version(draft.id)
            rows.append((draft, version))
    finally:
        connection.close()

    if not rows:
        console.print("[yellow]No drafts yet.[/yellow] Run 'ai-news draft export' to start.")
        return

    table = Table(title="Drafts")
    table.add_column("ID", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("v", justify="right")
    table.add_column("Format", no_wrap=True)
    table.add_column("Headline", overflow="fold")
    for draft, version in rows:
        colour = "yellow" if draft.status is DraftStatus.PENDING_REVIEW else "white"
        table.add_row(
            str(draft.id)[:8],
            f"[{colour}]{draft.status.value}[/{colour}]",
            str(version.version_no),
            version.post_format.value if version.post_format else "-",
            version.title,
        )
    console.print(table)
    console.print("[dim]Read-only. No approval or publishing exists yet.[/dim]")


@app.command("show")
def show_draft(
    draft_id: Annotated[str, typer.Argument(help="Draft id, or its first characters.")],
) -> None:
    """Preview one draft exactly as it would read."""
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

        version = repo.current_version(match.id)
        article = ArticleRepository(connection).get(match.article_id)
        evaluation = (
            EvaluationRepository(connection).latest_for_article(match.article_id)
            if match.evaluation_id
            else None
        )
        history = repo.list_versions(match.id)
    finally:
        connection.close()

    rendered = render_post(
        headline=version.title,
        body=version.body,
        source_label=version.source_attribution.split("\n")[0].split(": ", 1)[-1],
        source_url=version.source_url or article.canonical_url,
    )
    length = check_length(rendered, version.post_format) if version.post_format else None

    console.print(
        Panel(
            rendered,
            title=f"DRAFT {str(match.id)[:8]}   {match.status.value}   v{version.version_no}",
            title_align="left",
            border_style="yellow" if match.status is DraftStatus.PENDING_REVIEW else "white",
        )
    )

    meta = Table(show_header=False, box=None)
    meta.add_column("", style="dim", no_wrap=True)
    meta.add_column("")
    meta.add_row("Source", f"{article.source_id}  {article.canonical_url}")
    meta.add_row("Category", version.category.value)
    meta.add_row("Audience", version.audience.value)
    meta.add_row("Format", version.post_format.value if version.post_format else "-")
    if length:
        meta.add_row(
            "Length", f"{length.chars} chars" + ("" if length.within_target else "  (off target)")
        )
    if evaluation:
        meta.add_row("Editorial score", f"{evaluation.composite_score:.1f}")
    meta.add_row("Versions", str(len(history)))
    meta.add_row("Content hash", version.content_hash[:16] + "…")
    console.print(meta)

    if version.writer_notes:
        console.print("\n[bold]Writer notes[/bold] [dim](internal, never published)[/dim]")
        for note in version.writer_notes:
            console.print(f"  • {note}")

    console.print(
        "\n[dim]Preview only. This draft is not approved and not published; approving it "
        "is a separate, explicit human step that does not exist yet.[/dim]"
    )


def _render_problems(problems: list[str]) -> None:
    err_console.print("[bold red]Draft batch rejected. Nothing was written.[/bold red]")
    for problem in problems:
        err_console.print(f"  [red]•[/red] {problem}")
