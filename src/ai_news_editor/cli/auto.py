"""``ai-news auto`` — the unattended NEWS pipeline, run once per invocation.

One command runs the whole thing: collect, normalize, select, fetch, generate,
validate, approve, publish — in that order, stopping at the first step that has nothing
to do. There is no long-running process here and nothing to keep alive; a scheduled
GitHub Actions run is one call to ``auto once`` and then the process exits, same as
``ai-news collect`` or ``ai-news publish`` always have.

Three modes, one function underneath (``automation.pipeline.run_automation``):

* ``--dry-run`` never contacts Telegram and never approves anything. It proves the
  same prompts and the same validation a real run would use.
* ``--test`` runs the whole pipeline, including approval, but sends to
  ``AI_NEWS_TEST_CHANNEL`` rather than the production channel.
* the default (neither flag) is a live run against the production channel, still
  gated by ``AI_NEWS_AUTOMATION_ENABLED`` — a plain ``ai-news auto once`` with the kill
  switch off does nothing and says so, rather than needing a separate flag to stay safe.

Nothing here can approve a PROMPT or a TESTED_USE_CASE, because nothing here looks for
one — nothing in this module content-type-filters at all; the automation pipeline itself
only ever selects from NEWS candidates.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from ai_news_editor.automation.pipeline import AutomationResult, Outcome, run_automation
from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import PublicationStatus
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.settings import Settings, get_settings

console = Console()
err_console = Console(stderr=True)
logger = get_logger(__name__)

auto_app = typer.Typer(
    name="auto",
    help="Unattended NEWS collection, generation and publishing. Off by default.",
)


def _settings() -> Settings:
    try:
        return get_settings()
    except AiNewsError as exc:
        err_console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


def _run_once(*, mode: str) -> AutomationResult:
    """Collect, normalize, then hand off to run_automation. Shared by once/run."""
    from ai_news_editor.cli.main import open_migrated_database
    from ai_news_editor.pipeline.collect import collect as collect_sources
    from ai_news_editor.pipeline.process import process as run_processing
    from ai_news_editor.sources.config import load_sources_config
    from ai_news_editor.sources.http import HttpClient

    settings = _settings()
    connection = open_migrated_database()
    try:
        # Collection and normalization run through the same functions 'ai-news collect'
        # and 'ai-news process' use, for the same reason a human would run them before
        # reviewing: nothing downstream should ever look at raw, undeduplicated items.
        # A dry run passes collect()'s own dry_run flag, which fetches and parses but
        # writes nothing — so a dry run genuinely leaves no trace, exactly as promised.
        try:
            config = load_sources_config(settings.sources_config_path)
            with HttpClient() as http:
                collect_sources(
                    connection, http, config,
                    run_id=now_utc().strftime("%Y%m%dT%H%M%SZ"),
                    dry_run=(mode == "dry-run"),
                )
            if mode != "dry-run":
                run_processing(connection)
        except AiNewsError as exc:
            return AutomationResult(Outcome.CONFIG_ERROR, f"could not collect sources: {exc}")

        return run_automation(connection, settings, mode=mode)  # type: ignore[arg-type]
    finally:
        connection.close()


_OUTCOME_STYLE = {
    Outcome.PUBLISHED: "bold green",
    Outcome.DRY_RUN_COMPLETE: "cyan",
    Outcome.DISABLED: "dim",
    Outcome.DAILY_LIMIT_REACHED: "yellow",
    Outcome.NO_CANDIDATE: "dim",
    Outcome.SELECTION_REJECTED: "yellow",
    Outcome.FULLTEXT_UNAVAILABLE: "yellow",
    Outcome.GENERATION_REJECTED: "yellow",
    Outcome.VALIDATION_FAILED: "red",
    Outcome.CONFIG_ERROR: "bold red",
    Outcome.GEMINI_ERROR: "bold red",
    Outcome.PUBLISH_ERROR: "bold red",
}


def _report(result: AutomationResult) -> None:
    style = _OUTCOME_STYLE.get(result.outcome, "white")
    console.print(f"[{style}]{result.outcome.value}[/{style}]  {result.detail}")
    if result.candidates_considered:
        console.print(f"[dim]candidates considered: {result.candidates_considered}[/dim]")
    if result.draft_id:
        console.print(f"[dim]draft: {result.draft_id}[/dim]")
    if result.published:
        console.print(
            f"[dim]message {result.message_id} in {result.channel}[/dim]"
        )


@auto_app.command("once")
def auto_once(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Select, fetch and generate, but approve and publish nothing.",
        ),
    ] = False,
    test: Annotated[
        bool,
        typer.Option(
            "--test",
            help="Run the full pipeline, but send to AI_NEWS_TEST_CHANNEL, not production.",
        ),
    ] = False,
) -> None:
    """One automation pass: collect, select, generate, validate, and — unless
    --dry-run — approve and publish at most one NEWS post.

    Exits 0 for a normal quiet outcome (nothing to publish, automation disabled,
    Gemini declined, the daily limit was already reached) and 1 only for a genuine
    infrastructure failure, so a scheduled workflow does not turn red on an ordinary
    quiet day.
    """
    if dry_run and test:
        err_console.print("[bold red]--dry-run and --test are mutually exclusive.[/bold red]")
        raise typer.Exit(code=2)

    mode = "dry-run" if dry_run else ("test" if test else "live")
    result = _run_once(mode=mode)
    _report(result)
    if not result.is_quiet:
        raise typer.Exit(code=1)


@auto_app.command("run")
def auto_run() -> None:
    """Alias for 'auto once' against the production channel.

    There is no long-running loop here to distinguish 'run' from 'once' by — GitHub
    Actions is the scheduler, not this process. The name exists so a workflow step
    reads as "run the automation" rather than the more code-shaped "once".
    """
    result = _run_once(mode="live")
    _report(result)
    if not result.is_quiet:
        raise typer.Exit(code=1)


@auto_app.command("stats")
def auto_stats() -> None:
    """How much gemini:auto activity is on record. Read-only."""
    from ai_news_editor.automation.pipeline import AUTOMATION_ACTOR
    from ai_news_editor.cli.main import open_migrated_database
    from ai_news_editor.storage.repositories import PublicationRepository, ReviewDecisionRepository

    connection = open_migrated_database()
    try:
        decisions = ReviewDecisionRepository(connection)
        publications = PublicationRepository(connection)

        approved = connection.execute(
            "SELECT COUNT(*) AS n FROM review_decisions WHERE actor = ? AND action = 'APPROVE'",
            (AUTOMATION_ACTOR,),
        ).fetchone()["n"]
        published = sum(
            1
            for p in publications.list_recent(limit=500)
            if p.status is PublicationStatus.SUCCEEDED
            and decisions.get(p.review_decision_id).actor == AUTOMATION_ACTOR
        )

        table = Table(title="Automation activity (gemini:auto)")
        table.add_column("metric")
        table.add_column("count", justify="right")
        table.add_row("approved", str(approved))
        table.add_row("published", str(published))
        console.print(table)
        console.print(
            "[dim]Per-attempt generated/rejected counts are not stored — a Gemini "
            "rejection or a validation failure creates no row by design (nothing was "
            "produced to count). See each run's own log output, and 'auto once "
            "--dry-run' for a live check, for that detail.[/dim]"
        )
    finally:
        connection.close()
