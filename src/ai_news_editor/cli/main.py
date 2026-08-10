"""Command-line interface.

Phase 1 exposes only what genuinely works: configuration inspection and database
lifecycle. ``collect``, ``review`` and ``publish`` are deliberately absent rather than
present as hollow stubs — a command that exists but does nothing is worse than a
command that does not exist.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from ai_news_editor import __version__
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.health import all_ok, run_health_checks
from ai_news_editor.observability.logging import configure_logging, new_run_id
from ai_news_editor.settings import Settings, get_settings
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


if __name__ == "__main__":
    app()
