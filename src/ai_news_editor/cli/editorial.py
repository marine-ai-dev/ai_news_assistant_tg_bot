"""The ``ai-news editorial`` command group.

Five commands covering the whole review loop: export candidates, validate a reviewed
file, import it, and read the results. Nothing here approves, drafts or publishes —
an evaluation says a story is worth covering, which is a long way from a post.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ai_news_editor.domain.enums import EditorialDecision
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.editorial.export import build_batch, stale_evaluations
from ai_news_editor.editorial.import_results import (
    EditorialImportError,
    import_reviewed,
    load_reviewed,
    validate_against_database,
)
from ai_news_editor.editorial.rubric import (
    CREDIBILITY_SHORTLIST_THRESHOLD,
    RUBRIC_VERSION,
    SCHEMA_VERSION,
)
from ai_news_editor.storage.repositories import ArticleRepository, EvaluationRepository

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="editorial",
    help="Export candidates for editorial review, import decisions, read the shortlist.",
    no_args_is_help=True,
)

DEFAULT_WORK_DIR = Path("editorial_work")


@app.command("export")
def export_batch(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Maximum candidates.")] = 20,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help="Restrict to this source id. Repeatable."),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Where to write the batch JSON.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Include articles that already have a current review.")
    ] = False,
    since: Annotated[
        datetime | None, typer.Option("--since", help="Only articles published on/after this date.")
    ] = None,
    until: Annotated[
        datetime | None,
        typer.Option("--until", help="Only articles published on/before this date."),
    ] = None,
) -> None:
    """Write an editorial batch for review.

    Selects articles that survived processing and have no current evaluation, spread
    across sources so one prolific feed cannot fill the batch.
    """
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        batch = build_batch(
            connection,
            limit=limit,
            source_ids=list(source) if source else None,
            since=_aware(since),
            until=_aware(until),
            force=force,
        )
    except AiNewsError as exc:
        err_console.print(f"[bold red]Export failed:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    finally:
        connection.close()

    if not batch.articles:
        console.print(
            "[yellow]No candidates need review.[/yellow] "
            "Everything eligible already has a current evaluation — use --force to re-export."
        )
        raise typer.Exit(code=0)

    path = output or (DEFAULT_WORK_DIR / f"{batch.batch_id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(batch.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    console.print(f"\n[bold]Editorial batch[/bold] [cyan]{batch.batch_id}[/cyan]")
    console.print(f"Wrote [bold]{len(batch.articles)}[/bold] candidates to [bold]{path}[/bold]")

    table = Table(show_header=True)
    table.add_column("Source", no_wrap=True)
    table.add_column("Count", justify="right")
    counts: dict[str, int] = {}
    for article in batch.articles:
        counts[article.source.name] = counts.get(article.source.name, 0) + 1
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        table.add_row(name, str(count))
    console.print(table)
    console.print(
        "[dim]Next: review it following docs/editorial_rubric.md, write the reviewed JSON, "
        "then 'ai-news editorial validate' and 'ai-news editorial import'.[/dim]"
    )


@app.command("validate")
def validate_reviewed(
    path: Annotated[Path, typer.Argument(help="Reviewed batch JSON to check.")],
) -> None:
    """Check a reviewed batch without writing anything."""
    from ai_news_editor.cli.main import open_migrated_database

    try:
        reviewed = load_reviewed(path)
    except EditorialImportError as exc:
        _render_problems(exc.problems)
        raise typer.Exit(code=1) from exc

    connection = open_migrated_database()
    try:
        problems = validate_against_database(connection, reviewed)
    finally:
        connection.close()

    if problems:
        _render_problems(problems)
        raise typer.Exit(code=1)

    decisions: dict[str, int] = {}
    for review in reviewed.reviews:
        decisions[review.decision.value] = decisions.get(review.decision.value, 0) + 1

    console.print(f"[green]Valid.[/green] {len(reviewed.reviews)} review(s) in {reviewed.batch_id}")
    for decision, count in sorted(decisions.items()):
        console.print(f"  {decision:<22} {count}")


@app.command("import")
def import_batch(
    path: Annotated[Path, typer.Argument(help="Reviewed batch JSON to import.")],
) -> None:
    """Validate a reviewed batch and store its evaluations.

    All-or-nothing: if any review is invalid, nothing is written. Re-importing the same
    file adds nothing the second time.
    """
    from ai_news_editor.cli.main import open_migrated_database

    try:
        reviewed = load_reviewed(path)
    except EditorialImportError as exc:
        _render_problems(exc.problems)
        raise typer.Exit(code=1) from exc

    connection = open_migrated_database()
    try:
        report = import_reviewed(connection, reviewed)
    except EditorialImportError as exc:
        _render_problems(exc.problems)
        raise typer.Exit(code=1) from exc
    finally:
        connection.close()

    console.print(f"\n[bold]Imported batch[/bold] [cyan]{report.batch_id}[/cyan]")
    table = Table()
    table.add_column("Outcome")
    table.add_column("Count", justify="right")
    table.add_row("New evaluations", str(report.imported))
    table.add_row("  shortlisted", str(report.shortlisted))
    table.add_row("  held for verification", str(report.held))
    table.add_row("  rejected", str(report.rejected))
    if report.already_present:
        table.add_row("Already imported", str(report.already_present))
    console.print(table)


@app.command("shortlist")
def show_shortlist(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many to show.")] = 10,
) -> None:
    """Show the ranked editorial shortlist."""
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        evaluations = EvaluationRepository(connection).shortlist(limit=limit)
        articles = ArticleRepository(connection)
        rows = [(evaluation, articles.get(evaluation.article_id)) for evaluation in evaluations]
        held = EvaluationRepository(connection).by_decision(
            EditorialDecision.HOLD_FOR_VERIFICATION, limit=10
        )
        held_rows = [(evaluation, articles.get(evaluation.article_id)) for evaluation in held]
    finally:
        connection.close()

    if not rows:
        console.print("[yellow]Nothing shortlisted yet.[/yellow] Run an editorial review first.")
        return

    console.print("\n[bold]AI NEWS — EDITORIAL SHORTLIST[/bold]\n")
    for position, (evaluation, article) in enumerate(rows, start=1):
        scores = evaluation.scores
        body = [
            f"[bold]{article.title}[/bold]",
            "",
            f"Source: {article.source_id}    "
            f"Category: {evaluation.category.value}    "
            f"Audience: {evaluation.audience.value}",
            f"Credibility {scores['credibility']}   "
            f"Reader interest {scores['reader_interest']}   "
            f"Usefulness {scores['usefulness']}   "
            f"Consumer impact {scores['consumer_impact']}",
        ]
        if evaluation.why_selected:
            body.append("")
            body.append("Why selected:")
            body.extend(f"  • {reason}" for reason in evaluation.why_selected)
        if evaluation.editorial_angle:
            body.append("")
            body.append(f"Angle: [italic]{evaluation.editorial_angle}[/italic]")
        body.append("")
        body.append(f"[dim]{article.canonical_url}[/dim]")

        console.print(
            Panel(
                "\n".join(body),
                title=f"#{position}   score {evaluation.composite_score:.1f}",
                title_align="left",
                border_style="green",
            )
        )

    for evaluation, article in held_rows:
        console.print(
            Panel(
                f"[bold]{article.title}[/bold]\n\n"
                f"Category: {evaluation.category.value}    "
                f"Credibility: {evaluation.scores['credibility']}\n"
                f"Verification: {evaluation.verification_status.value}\n"
                + (f"\n{evaluation.notes}" if evaluation.notes else "")
                + f"\n\n[dim]{article.canonical_url}[/dim]",
                title="⚠  HOLD FOR VERIFICATION",
                title_align="left",
                border_style="yellow",
            )
        )

    console.print(
        "[dim]Read-only. Nothing here is approved, drafted or published — Phase 5 turns "
        "shortlisted stories into drafts, and a human still approves every post.[/dim]"
    )


@app.command("status")
def editorial_status() -> None:
    """Show editorial progress: reviewed, shortlisted, held, rejected, stale."""
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        evaluations = EvaluationRepository(connection)
        articles = ArticleRepository(connection)
        counts = evaluations.count_by_decision()
        evaluated = len(evaluations.evaluated_article_ids())
        candidates = len(articles.list_by_status_ids())
        total_evaluations = evaluations.count()
        stale = stale_evaluations(connection)
    finally:
        connection.close()

    table = Table(title="Editorial")
    table.add_column("Stage")
    table.add_column("Count", justify="right")
    table.add_row("Candidates", str(candidates))
    table.add_row("  awaiting editorial review", str(max(0, candidates - evaluated)))
    table.add_row("  evaluated", str(evaluated))
    table.add_row("Shortlisted", str(counts.get(EditorialDecision.SHORTLIST.value, 0)))
    table.add_row(
        "Held for verification",
        str(counts.get(EditorialDecision.HOLD_FOR_VERIFICATION.value, 0)),
    )
    table.add_row("Rejected", str(counts.get(EditorialDecision.REJECT.value, 0)))
    table.add_row("Evaluations stored (all history)", str(total_evaluations))
    table.add_row("Stale — content changed since review", str(len(stale)))
    console.print(table)

    for _, title in stale[:5]:
        console.print(f"[yellow]stale:[/yellow] {title[:80]}")

    console.print(
        f"[dim]Rubric v{RUBRIC_VERSION}, schema v{SCHEMA_VERSION}. "
        f"Shortlisting requires credibility ≥ {CREDIBILITY_SHORTLIST_THRESHOLD}.[/dim]"
    )


def _render_problems(problems: list[str]) -> None:
    err_console.print("[bold red]Reviewed batch rejected. Nothing was imported.[/bold red]")
    for problem in problems:
        err_console.print(f"  [red]•[/red] {problem}")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
