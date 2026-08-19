"""``ai-news auto`` — the unattended NEWS pipeline, run once per invocation.

One command runs the whole thing: collect, normalize, select, fetch, generate,
validate, approve, publish — in that order, stopping at the first step that has nothing
to do. Select-fetch-generate-validate is itself a small bounded loop, not a single
shot: a candidate rejected for a reason specific to it (a 403 fetching its article, an
incomplete generation, a failed validation) is dropped and the next remaining eligible
candidate is tried instead, up to ``AI_NEWS_MAX_CANDIDATE_ATTEMPTS`` (default 3) — see
``automation.pipeline._attempt_candidates``. A genuine infrastructure failure (a bad
key, an exhausted retry budget) still aborts the whole run immediately rather than
being retried against more candidates. There is no long-running process here and
nothing to keep alive; a scheduled GitHub Actions run is one call to ``auto once`` and
then the process exits, same as ``ai-news collect`` or ``ai-news publish`` always have.

Three modes, one function underneath (``automation.pipeline.run_pass``, which collects
and normalizes before handing off to ``run_automation`` for selection onward):

* ``--dry-run`` never contacts Telegram and never approves anything. Collection and
  normalization still run for real — a fresh GitHub Actions runner starting from an
  empty checkout genuinely selects, fetches and generates from a real candidate, not
  just "reaches the RSS feeds and stops." What makes it dry is *where* those writes
  land: an in-memory copy of the database (see ``automation.pipeline.isolated_connection``),
  seeded with the real database's history so dedup still works, discarded when the run
  ends. Runs regardless of ``AI_NEWS_AUTOMATION_ENABLED`` — a manual dry run must keep
  working while that switch stays off, which is the normal state once a scheduled
  workflow exists.
* ``--test`` runs the same real pipeline, including approval, and sends to
  ``AI_NEWS_TEST_CHANNEL`` rather than the production channel — also regardless of
  ``AI_NEWS_AUTOMATION_ENABLED``, for the same reason. Collection, normalization,
  selection, generation, validation, approval and the Publication record all land in
  that same throwaway in-memory copy, never the real database, so a test send can
  never make a candidate unavailable to a later live run, count against the live daily
  limit, or leave any trace a human reading production history would mistake for a
  real one. Only the Telegram message itself is real.
* the default (neither flag) is a live run against the production channel, writing to
  the real database throughout, and this is the ONE mode ``AI_NEWS_AUTOMATION_ENABLED``
  gates — a plain ``ai-news auto once`` (or a scheduled run, which is always this mode)
  with the kill switch off does nothing and says so, rather than needing a separate
  flag to stay safe.

Nothing here can approve a PROMPT or a TESTED_USE_CASE, because nothing here looks for
one — nothing in this module content-type-filters at all; the automation pipeline itself
only ever selects from NEWS candidates.
"""

from __future__ import annotations

import os
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from ai_news_editor.automation.pipeline import AutomationResult, Outcome, run_pass
from ai_news_editor.domain.enums import PublicationStatus
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.settings import Settings, get_settings
from ai_news_editor.storage import db

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
    """Open the canonical database and hand off to run_pass. Shared by once/run.

    Everything about collection, normalization and which database actually gets
    written to lives in automation.pipeline.run_pass now — this function's only job is
    the CLI-specific part: load settings, open and eventually close the one real,
    on-disk connection every mode starts from.
    """
    from ai_news_editor.cli.main import open_migrated_database

    settings = _settings()
    connection = open_migrated_database()
    try:
        return run_pass(connection, settings, mode=mode)  # type: ignore[arg-type]
    finally:
        # Fold the WAL into the main file before closing, rather than trusting SQLite's
        # own close-time checkpoint. The GitHub Actions workflow commits only the main
        # .sqlite3 path to git — see storage.db.checkpoint's docstring — and this is
        # the one place every 'auto once' invocation is guaranteed to pass through,
        # regardless of mode or outcome. Harmless to run even for dry-run/test, whose
        # writes never touched this connection in the first place.
        db.checkpoint(connection)
        connection.close()


def _emit_github_output(result: AutomationResult) -> None:
    """Expose this run's outcome to a GitHub Actions step, when running as one.

    A no-op everywhere else: ``GITHUB_OUTPUT`` is a path GitHub Actions sets in the
    runner's environment for exactly this purpose, and is simply absent locally or in
    tests. Written as ``outcome=<value>`` so a later workflow step can gate on it (e.g.
    "only commit persisted state after a live run that actually published something")
    without parsing this command's Rich-formatted console output.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"outcome={result.outcome.value}\n")


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
    Outcome.CANDIDATES_EXHAUSTED: "yellow",
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
    _emit_github_output(result)
    if not result.is_quiet and not result.published:
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
    _emit_github_output(result)
    if not result.is_quiet and not result.published:
        raise typer.Exit(code=1)


@auto_app.command("soak-v2")
def auto_soak_v2(
    count: Annotated[
        int, typer.Option("--count", "-n", help="How many posts to send.")
    ] = 8,
    prefer_category: Annotated[
        str | None,
        typer.Option(
            "--prefer-category",
            help="Developer override for TEST soak coverage: narrow candidates to "
            "sources capable of this EditorialCategory. Never forces an invalid "
            "classification — the real pipeline still decides the actual category.",
        ),
    ] = None,
    test: Annotated[
        bool,
        typer.Option(
            "--test",
            help="Required. This command only ever targets AI_NEWS_TEST_CHANNEL — "
            "there is no live mode for it.",
        ),
    ] = False,
) -> None:
    """Step 6: send real v2-pipeline posts to the TEST Telegram channel only.

    Runs the real, currently-collected candidate pool through the actual v2
    pipeline (source capability, diversity, primary-source preference, Gemini
    classification/generation, media, category rendering, publication planning) —
    the same isolated in-memory database copy ``auto once --test`` already uses, so
    nothing here ever touches the canonical on-disk database, the live daily-limit
    state, or the production channel. Sequential, with a pause between posts —
    never a flood, never a persistent schedule.
    """
    if not test:
        err_console.print(
            "[bold red]--test is required.[/bold red] This command never targets "
            "production — there is no live mode for a soak run."
        )
        raise typer.Exit(code=2)

    from ai_news_editor.automation import test_history
    from ai_news_editor.automation.gemini import GeminiClient
    from ai_news_editor.automation.pipeline import isolated_connection
    from ai_news_editor.automation.soak import run_soak
    from ai_news_editor.cli.main import open_migrated_database
    from ai_news_editor.domain.enums import EditorialCategory
    from ai_news_editor.planning.recent_history import recent_history
    from ai_news_editor.publishing.telegram import TelegramClient
    from ai_news_editor.sources.config import load_sources_config
    from ai_news_editor.sources.http import HttpClient
    from ai_news_editor.storage.repositories import (
        ArticleRepository,
        DraftRepository,
        EvaluationRepository,
        PublicationRepository,
    )

    settings = _settings()
    if not settings.test_channel:
        err_console.print(
            "[bold red]AI_NEWS_TEST_CHANNEL is not configured.[/bold red] Set it before "
            "running a soak — this command refuses to guess a destination."
        )
        raise typer.Exit(code=2)

    category: EditorialCategory | None = None
    if prefer_category is not None:
        try:
            category = EditorialCategory(prefer_category.upper())
        except ValueError as exc:
            err_console.print(f"[bold red]Unknown category:[/bold red] {prefer_category}")
            raise typer.Exit(code=2) from exc

    canonical = open_migrated_database()
    try:
        with isolated_connection(canonical, mode="test") as connection:
            registry = load_sources_config(settings.sources_config_path)
            recent_seed = recent_history(
                publications=PublicationRepository(connection),
                drafts=DraftRepository(connection),
                articles=ArticleRepository(connection),
                evaluations=EvaluationRepository(connection),
                sources=registry,
            )
            client = GeminiClient(
                settings.gemini_api_key.get_secret_value(), model=settings.llm_model
            )
            with (
                HttpClient() as http,
                TelegramClient(settings.telegram_bot_token.get_secret_value()) as telegram,
            ):
                results = run_soak(
                    connection,
                    client=client,
                    registry=registry,
                    http=http,
                    telegram=telegram,
                    target_channel=settings.test_channel,
                    count=count,
                    prefer_category=category,
                    test_history_path=settings.data_dir / test_history.DEFAULT_FILENAME,
                    recent_seed=recent_seed,
                )
    finally:
        canonical.close()

    if not results:
        console.print("[yellow]No real candidate produced a publishable post.[/yellow]")
        raise typer.Exit(code=0)

    table = Table(title=f"Soak — {len(results)} post(s) sent to {settings.test_channel}")
    table.add_column("#")
    table.add_column("Category")
    table.add_column("Source")
    table.add_column("Plan")
    table.add_column("Gemini calls", justify="right")
    table.add_column("Message ID(s)")
    for result in results:
        message_ids = ", ".join(
            str(c.message_id) for c in result.component_outcomes if c.message_id is not None
        )
        table.add_row(
            str(result.index),
            result.outcome.content.category.value,
            result.source_id,
            result.variant.value,
            str(result.outcome.gemini_calls),
            message_ids,
        )
    console.print(table)


@auto_app.command("publish-v2-production")
def auto_publish_v2_production(
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Required. This command targets the real production Telegram channel.",
        ),
    ] = False,
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Required in addition to --live. A second explicit flag for an "
            "action that publishes to real production and cannot be undone.",
        ),
    ] = False,
) -> None:
    """One manual, unscheduled v2 post to the REAL production Telegram channel.

    NOT part of the schedule and NOT gated the same way as ``auto once`` — this is a
    human-authorized one-off, so it still checks ``AI_NEWS_AUTOMATION_ENABLED`` (the
    same kill switch the scheduled job respects) but does not touch cron, the daily
    limit, or any other production configuration. Sends at most one message: the first
    real, eligible, geography/dystopian-filtered, non-NEWS/non-RESEARCH candidate,
    preferring AI_LIFEHACK, then AI_TOOL, PROMPT_WORKFLOW, FREE_DEAL, EXPLAINER, in
    that order (a soft preference — real classification still decides). See
    ``automation.publish_v2_production`` for why this never routes through
    ``publishing.service`` (v1's re-render-at-send-time path is incompatible with v2's
    already-rendered content).
    """
    if not (live and confirm):
        err_console.print(
            "[bold red]Both --live and --confirm are required.[/bold red] This "
            "command sends to the real production channel and cannot be undone."
        )
        raise typer.Exit(code=2)

    from ai_news_editor.automation.gemini import GeminiClient
    from ai_news_editor.automation.publish_v2_production import (
        NoEligibleCandidateError,
        publish_one_v2_post_to_production,
    )
    from ai_news_editor.cli.main import open_migrated_database
    from ai_news_editor.domain.enums import EditorialCategory
    from ai_news_editor.publishing.telegram import TelegramClient
    from ai_news_editor.sources.config import load_sources_config
    from ai_news_editor.sources.http import HttpClient

    settings = _settings()
    if not settings.automation_enabled:
        err_console.print(
            "[bold red]AI_NEWS_AUTOMATION_ENABLED is not set.[/bold red] This command "
            "respects the same kill switch the scheduled job does."
        )
        raise typer.Exit(code=2)
    if not settings.telegram_channel:
        err_console.print(
            "[bold red]AI_NEWS_TELEGRAM_CHANNEL is not configured.[/bold red] This "
            "command refuses to guess a destination."
        )
        raise typer.Exit(code=2)

    canonical = open_migrated_database()
    try:
        registry = load_sources_config(settings.sources_config_path)
        client = GeminiClient(
            settings.gemini_api_key.get_secret_value(), model=settings.llm_model
        )
        with (
            HttpClient() as http,
            TelegramClient(settings.telegram_bot_token.get_secret_value()) as telegram,
        ):
            try:
                result = publish_one_v2_post_to_production(
                    canonical,
                    client=client,
                    registry=registry,
                    http=http,
                    telegram=telegram,
                    target_channel=settings.telegram_channel,
                    category_preference=(
                        EditorialCategory.AI_LIFEHACK,
                        EditorialCategory.AI_TOOL,
                        EditorialCategory.PROMPT_WORKFLOW,
                        EditorialCategory.FREE_DEAL,
                        EditorialCategory.EXPLAINER,
                    ),
                )
            except NoEligibleCandidateError as exc:
                err_console.print(f"[yellow]{exc}[/yellow]")
                raise typer.Exit(code=0) from None
    finally:
        canonical.close()

    message_ids = ", ".join(
        str(c.message_id) for c in result.component_outcomes if c.message_id is not None
    )
    table = Table(title=f"Published to {settings.telegram_channel}")
    table.add_column("Category")
    table.add_column("Source")
    table.add_column("Headline")
    table.add_column("Plan")
    table.add_column("Message ID(s)")
    table.add_row(
        result.outcome.content.category.value,
        result.source_id,
        result.outcome.content.headline,
        result.variant.value,
        message_ids,
    )
    console.print(table)


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
