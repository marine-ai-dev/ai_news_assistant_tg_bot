"""Command-line interface.

Phase 1 exposes only what genuinely works: configuration inspection and database
lifecycle. ``collect``, ``review`` and ``publish`` are deliberately absent rather than
present as hollow stubs — a command that exists but does nothing is worse than a
command that does not exist.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from ai_news_editor import __version__
from ai_news_editor.domain.enums import FetchOutcome
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.health import all_ok, run_health_checks
from ai_news_editor.observability.logging import configure_logging, current_run_id, new_run_id
from ai_news_editor.pipeline.collect import CollectionReport, collection_timestamp
from ai_news_editor.pipeline.collect import collect as collect_sources
from ai_news_editor.pipeline.process import ProcessingReport, pipeline_stats
from ai_news_editor.pipeline.process import process as run_processing
from ai_news_editor.settings import Settings, get_settings
from ai_news_editor.sources.config import SourcesConfig, load_sources_config
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.storage import db

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="ai-news",
    help="Human-in-the-loop AI editorial pipeline for a Ukrainian Telegram channel.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(name="db", help="Database lifecycle.", no_args_is_help=True)
app.add_typer(db_app)

#: Literal statements rather than an interpolated table name: no SQL in this codebase
#: is built by string formatting, not even from trusted constants.
_COUNT_QUERIES = {
    "sources": "SELECT COUNT(*) AS n FROM sources",
    "raw_items": "SELECT COUNT(*) AS n FROM raw_items",
    "articles": "SELECT COUNT(*) AS n FROM articles",
    "drafts": "SELECT COUNT(*) AS n FROM drafts",
    "draft_versions": "SELECT COUNT(*) AS n FROM draft_versions",
    "review_decisions": "SELECT COUNT(*) AS n FROM review_decisions",
}


@app.callback()
def _root(
    log_level: Annotated[
        str | None, typer.Option("--log-level", help="Override the configured log level.")
    ] = None,
) -> None:
    """Configure logging before any command runs."""
    settings = _load_settings()
    configure_logging(level=log_level or settings.log_level, fmt=settings.log_format)
    new_run_id()


def _load_settings() -> Settings:
    try:
        return get_settings()
    except AiNewsError as exc:
        err_console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


@app.command()
def version() -> None:
    """Print the application version."""
    console.print(f"ai-news {__version__}")


@app.command()
def doctor() -> None:
    """Check local application health.

    Inspects this machine only — Python version, configuration, data directory and
    database state. It makes no network calls; there are no external integrations yet.
    """
    settings = _load_settings()
    checks = run_health_checks(settings)

    table = Table(title="ai-news doctor", show_lines=False)
    table.add_column("Check", style="bold", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    for check in checks:
        table.add_row(
            check.name, "[green]OK[/green]" if check.ok else "[red]FAIL[/red]", check.detail
        )
    console.print(table)
    console.print("[dim]No external services were contacted.[/dim]")

    if not all_ok(checks):
        raise typer.Exit(code=1)


@db_app.command("init")
def db_init() -> None:
    """Create the database file and apply all migrations."""
    settings = _load_settings()
    settings.ensure_data_dir()
    path = settings.resolved_database_path
    existed = path.exists()

    connection = db.connect(path)
    try:
        applied = db.migrate(connection)
        current = db.schema_version(connection)
    except AiNewsError as exc:
        err_console.print(f"[bold red]Migration failed:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    finally:
        connection.close()

    verb = "Updated" if existed else "Initialized"
    names = ", ".join(f"{m.version:03d}_{m.name}" for m in applied) or "none"
    console.print(f"{verb} database at [bold]{path}[/bold]")
    console.print(f"Applied migrations: {names}")
    console.print(f"Schema version: [bold]{current}[/bold]")


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply any pending migrations. Safe to run repeatedly."""
    db_init()


@db_app.command("status")
def db_status() -> None:
    """Show schema version, pending migrations, and row counts."""
    settings = _load_settings()
    path = settings.resolved_database_path
    if not path.exists():
        err_console.print(f"No database at [bold]{path}[/bold]. Run 'ai-news db init'.")
        raise typer.Exit(code=1)

    connection = db.connect(path)
    try:
        applied = db.applied_migrations(connection)
        pending = db.pending_migrations(connection)

        table = Table(title=f"Migrations — {path}")
        table.add_column("Version")
        table.add_column("Name")
        table.add_column("State")
        table.add_column("Applied at")
        for migration in applied:
            table.add_row(
                f"{migration.version:03d}",
                migration.name,
                "[green]applied[/green]",
                migration.applied_at,
            )
        for migration in pending:
            table.add_row(
                f"{migration.version:03d}",
                migration.name,
                "[yellow]pending[/yellow]",
                "-",
            )
        console.print(table)

        counts = Table(title="Row counts")
        counts.add_column("Table")
        counts.add_column("Rows", justify="right")
        for name, sql in _COUNT_QUERIES.items():
            counts.add_row(name, str(connection.execute(sql).fetchone()["n"]))
        console.print(counts)
    finally:
        connection.close()


@app.command()
def sources() -> None:
    """List configured sources and their last fetch outcome."""
    settings = _load_settings()
    config = _load_sources_config(settings)

    table = Table(title="Configured sources", show_lines=True)
    table.add_column("Source", no_wrap=True)
    table.add_column("Kind / trust", no_wrap=True)
    table.add_column("Editorial role", ratio=1)

    for definition in config.sources:
        marker = "" if definition.enabled else " [dim](disabled)[/dim]"
        trust = definition.trust_tier.value.replace("_", " ").lower()
        table.add_row(
            f"{definition.id}{marker}",
            f"{definition.adapter.value.lower()}\n[dim]{trust}[/dim]",
            " ".join(definition.editorial_role.split()),
        )
    console.print(table)
    console.print(
        "[dim]Trust tier is provenance metadata, not a verdict on truth: it records where a "
        "claim came from. Community signals never establish that a claim is true.[/dim]"
    )


@app.command()
def collect(
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help="Collect only this source id. Repeatable."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Fetch and parse, but write nothing to the database."),
    ] = False,
) -> None:
    """Fetch configured sources and store new items.

    Collects every enabled source by default. Ingestion only: items are stored with
    their provenance exactly as the feed supplied them. No filtering, ranking or
    rewriting happens here.
    """
    settings = _load_settings()
    config = _load_sources_config(settings)
    connection = _open_migrated_database()
    try:
        with HttpClient(timeout=config.defaults.timeout_seconds) as http:
            report = collect_sources(
                connection,
                http,
                config,
                run_id=current_run_id() or "-",
                source_ids=list(source) if source else None,
                dry_run=dry_run,
            )
    except AiNewsError as exc:
        err_console.print(f"[bold red]Collection failed:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    finally:
        connection.close()

    _render_collection(report)

    if not report.all_ok:
        raise typer.Exit(code=1)


def _load_sources_config(settings: Settings) -> SourcesConfig:
    try:
        return load_sources_config(settings.sources_config_path)
    except AiNewsError as exc:
        err_console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


def _render_collection(report: CollectionReport) -> None:
    heading = "AI NEWS - COLLECTION" + (" (dry run - nothing written)" if report.dry_run else "")
    console.print(f"\n[bold]{heading}[/bold]  {collection_timestamp()}")

    table = Table(show_footer=False)
    table.add_column("Source", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Fetched", justify="right")
    table.add_column("New", justify="right")
    table.add_column("Known", justify="right")
    table.add_column("Detail", overflow="fold")

    for item in report.sources:
        if item.outcome is FetchOutcome.ERROR:
            status = "[red]ERROR[/red]"
            detail = item.error or ""
        elif item.outcome is FetchOutcome.NOT_MODIFIED:
            status = "[cyan]304[/cyan]"
            detail = "not modified since last fetch"
        else:
            status = "[green]OK[/green]"
            detail = f"{item.duration_ms} ms"
            if item.warnings:
                detail += f" - {len(item.warnings)} warning(s)"
        table.add_row(
            item.source_id,
            status,
            str(item.fetched),
            str(item.inserted),
            str(item.existing),
            detail,
        )

    console.print(table)

    verb = "would be new" if report.dry_run else "new raw items"
    console.print(
        f"Sources checked: [bold]{len(report.sources)}[/bold]   "
        f"succeeded: [green]{report.succeeded}[/green]   "
        f"failed: {'[red]' if report.failed else ''}{report.failed}"
        f"{'[/red]' if report.failed else ''}"
    )
    console.print(
        f"Fetched entries: [bold]{report.fetched}[/bold]   "
        f"{verb}: [bold]{report.inserted}[/bold]   "
        f"already known: [bold]{report.existing}[/bold]"
    )
    if report.failed:
        console.print("[yellow]Collection completed with failures (exit code 1).[/yellow]")


@app.command()
def process(
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Process at most this many raw items.")
    ] = None,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help="Process only this source id. Repeatable."),
    ] = None,
) -> None:
    """Turn collected raw items into deduplicated editorial candidates.

    Deterministic and resumable: normalization, duplicate detection and rule-based
    screening only. No LLM is involved, and nothing here decides whether a story is
    interesting — that is a later phase.
    """
    connection = _open_migrated_database()
    try:
        report = run_processing(
            connection, limit=limit, source_ids=list(source) if source else None
        )
    except AiNewsError as exc:
        err_console.print(f"[bold red]Processing failed:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    finally:
        connection.close()

    _render_processing(report)


@app.command()
def status() -> None:
    """Show the pipeline funnel: what has been collected, processed and screened."""
    connection = _open_migrated_database()
    try:
        stats = pipeline_stats(connection)
    finally:
        connection.close()

    table = Table(title="Pipeline")
    table.add_column("Stage")
    table.add_column("Count", justify="right")
    rows = (
        ("Raw items collected", stats["raw_items"]),
        ("  not yet processed", stats["unprocessed"]),
        ("Articles created", stats["articles"]),
        ("  duplicates", stats["duplicates"]),
        ("  screened out", stats["screened_out"]),
        ("  awaiting AI evaluation", stats["awaiting_evaluation"]),
        ("Community signals", stats["community_signals"]),
        ("  matched to an article", stats["signals_attached"]),
    )
    for label, value in rows:
        table.add_row(label, str(value))
    console.print(table)
    console.print(
        "[dim]Community signals are attention metadata. They are never provenance for a "
        "claim and never become article content.[/dim]"
    )


def _open_migrated_database() -> sqlite3.Connection:
    """Open the database, refusing to proceed on a stale schema."""
    settings = _load_settings()
    path = settings.resolved_database_path
    if not path.exists():
        err_console.print(f"No database at [bold]{path}[/bold]. Run 'ai-news db init'.")
        raise typer.Exit(code=2)

    connection = db.connect(path)
    if db.pending_migrations(connection):
        connection.close()
        err_console.print("Database schema is out of date. Run 'ai-news db migrate'.")
        raise typer.Exit(code=2)
    return connection


def _render_processing(report: ProcessingReport) -> None:
    console.print("\n[bold]AI NEWS - PROCESSING[/bold]")

    table = Table()
    table.add_column("Stage")
    table.add_column("Count", justify="right")
    table.add_row("Raw items considered", str(report.considered))
    table.add_row("Articles normalized", str(report.normalized))
    table.add_row("  exact duplicates", str(report.exact_duplicates))
    table.add_row("  near duplicates", str(report.near_duplicates))
    table.add_row("  possible cross-source", str(report.possible_cross_source))
    table.add_row("  screened out", str(report.screened_out))
    table.add_row("Ready for evaluation", str(report.ready))
    if report.rejected:
        table.add_row("Could not normalize", str(report.rejected))
    if report.signals_recorded or report.signals_attached:
        table.add_row("Community signals recorded", str(report.signals_recorded))
        table.add_row("  matched to an article", str(report.signals_attached))
    console.print(table)

    if report.screening_reasons:
        reasons = Table(title="Screening reasons")
        reasons.add_column("Reason")
        reasons.add_column("Count", justify="right")
        for reason, count in sorted(report.screening_reasons.items()):
            reasons.add_row(reason, str(count))
        console.print(reasons)

    for rejection in report.rejections[:10]:
        console.print(f"[yellow]could not normalize:[/yellow] {rejection}")


if __name__ == "__main__":
    app()
