"""The ``ai-news content`` command group — prompts and explainers.

Three commands, because three is what the job needs: write a skeleton for a Claude Code
session to fill in, check the result without touching the database, and import it.

The news pipeline has an export step because a story exists before anyone decides to
cover it. A prompt does not exist until someone writes it, so there is nothing to
export — ``template`` produces an empty form rather than an assignment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ai_news_editor.content.import_items import (
    ContentImportError,
    import_batch,
    load_batch,
    rendered_preview,
    review_notes,
)
from ai_news_editor.content.schema import CONTENT_SCHEMA_VERSION
from ai_news_editor.domain.enums import ContentType
from ai_news_editor.storage.repositories import ContentItemRepository
from ai_news_editor.writing.schema import STYLE_VERSION

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="content",
    help="Create prompts and explainers: template, validate, import, list.",
)

DEFAULT_WORK_DIR = Path("content_work")


@app.command("template")
def template(
    out_dir: Annotated[
        Path, typer.Option("--out", help="Where to write the skeleton.")
    ] = DEFAULT_WORK_DIR,
) -> None:
    """Write an empty batch file for a Claude Code session to fill in.

    The skeleton carries the schema and style versions, so a session cannot silently
    write against an older contract than the one the importer enforces.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    batch_id = f"content-{uuid4().hex[:8]}"
    path = out_dir / f"{batch_id}.content.json"

    skeleton = {
        "schema_version": CONTENT_SCHEMA_VERSION,
        "style_version": STYLE_VERSION,
        "batch_id": batch_id,
        "author": "claude-code",
        "items": [
            {
                "content_type": "PROMPT",
                "title": "internal working title",
                "audience": "NEWCOMER",
                "topic": "EVERYDAY_LIFE",
                "what_you_can_do": "what this helps the reader do",
                "prompt_text": "the prompt the reader copies",
                "customization_tips": ["what to change to make it yours"],
                "works_with": None,
                "references": [],
                "post": {
                    "headline": "🆕 …",
                    "body": "the Ukrainian post, containing the prompt itself",
                    "category": "EVERYDAY_AI",
                    "post_format": "QUICK",
                    "hashtags": [],
                    "writer_notes": [],
                },
            },
            {
                "content_type": "EXPLAINER",
                "title": "internal working title",
                "audience": "NEWCOMER",
                "concept": "the single concept explained",
                "simple_explanation": "in ordinary language",
                "real_life_example": "from the reader's life, not from technology",
                "why_it_matters": "why this is worth knowing",
                "try_this": None,
                "references": [],
                "post": {
                    "headline": "🧠 …",
                    "body": "the Ukrainian post",
                    "category": "EXPLAINED_SIMPLY",
                    "post_format": "QUICK",
                    "hashtags": [],
                    "writer_notes": [],
                },
            },
        ],
    }
    path.write_text(json.dumps(skeleton, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    console.print(f"Wrote [bold]{path}[/bold]")
    console.print(
        "\nFill it in following [bold]docs/telegram_style_guide.md[/bold], then:\n"
        f"  ai-news content validate {path}\n"
        f"  ai-news content import {path}"
    )


@app.command("validate")
def validate(
    path: Annotated[Path, typer.Argument(help="The batch file to check.")],
    show: Annotated[
        bool, typer.Option("--show", help="Print each post as it would appear.")
    ] = False,
) -> None:
    """Check a batch without writing anything.

    Reports the same jargon warnings the import would, so a session can fix wording
    before anything reaches the database.
    """
    try:
        batch = load_batch(path)
    except ContentImportError as exc:
        err_console.print(f"[bold red]Invalid:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"{path.name} — {len(batch.items)} item(s)")
    table.add_column("Type")
    table.add_column("Audience")
    table.add_column("Subject")
    table.add_column("Title")
    for item in batch.items:
        subject = (
            getattr(item, "topic", None)
            or getattr(item, "theme", None)
            or getattr(item, "concept", None)
            or getattr(getattr(item, "resource", None), "resource_type", "")
        )
        table.add_row(
            item.content_type.value,
            item.audience.value,
            str(getattr(subject, "value", subject)),
            item.title,
        )
    console.print(table)

    if show:
        for item in batch.items:
            console.print(Panel(rendered_preview(item), title=item.title))

    notes = review_notes(batch)
    if notes:
        console.print("\n[yellow]Worth a second look[/yellow] — jargon without a nearby "
                      "explanation, for readers who may not know these words:")
        for note in notes:
            console.print(f"  • {note}")
        console.print(
            "[dim]These are notes, not errors. If the term is explained in a way this "
            "check did not recognise, ignore it.[/dim]"
        )
    else:
        console.print("\n[green]No unexplained jargon found.[/green]")

    console.print("\nValid. Nothing was written.")


@app.command("import")
def import_content(
    path: Annotated[Path, typer.Argument(help="The batch file to import.")],
) -> None:
    """Import a batch: content items plus drafts, all awaiting review."""
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        try:
            outcome = import_batch(connection, path)
        except ContentImportError as exc:
            err_console.print(f"[bold red]Not imported:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc

        for draft_id, title in outcome.created:
            console.print(f"[green]created[/green] {str(draft_id)[:8]}  {title}")
        for title in outcome.skipped:
            console.print(f"[dim]already imported[/dim]  {title}")

        if outcome.warnings:
            console.print("\n[yellow]For the reviewer's attention:[/yellow]")
            for note in outcome.warnings:
                console.print(f"  • {note}")

        console.print(
            f"\n{outcome.count} draft(s) created, awaiting review. "
            "Nothing is approved and nothing is published."
        )
        if outcome.count:
            console.print("Read them with [bold]ai-news review[/bold].")
    finally:
        connection.close()


@app.command("list")
def list_items(
    content_type: Annotated[
        str | None, typer.Option("--type", "-t", help="PROMPT or EXPLAINER.")
    ] = None,
) -> None:
    """List stored prompts and explainers."""
    from ai_news_editor.cli.main import open_migrated_database

    chosen: ContentType | None = None
    if content_type is not None:
        try:
            chosen = ContentType(content_type.upper())
        except ValueError as exc:
            err_console.print(
                f"[bold red]Unknown content type:[/bold red] {content_type}. "
                f"Use one of: {', '.join(t.value for t in ContentType)}"
            )
            raise typer.Exit(code=2) from exc

    connection = open_migrated_database()
    try:
        items = ContentItemRepository(connection).list_by_type(chosen)
        if not items:
            console.print("No editorial content yet. Start with [bold]ai-news content "
                          "template[/bold].")
            return

        table = Table(title="Editorial content")
        table.add_column("Type")
        table.add_column("Audience")
        table.add_column("Subject")
        table.add_column("Title")
        table.add_column("Created")
        for item in items:
            table.add_row(
                item.content_type.value,
                item.audience.value,
                item.subject,
                item.title,
                item.created_at.strftime("%Y-%m-%d"),
            )
        console.print(table)
    finally:
        connection.close()
