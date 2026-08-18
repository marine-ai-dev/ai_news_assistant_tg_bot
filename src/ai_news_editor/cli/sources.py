"""Source registry diagnostics.

Read-only, by the same discipline as ``telegram doctor``: it fetches each source's
discovery endpoint through the exact adapter collection would use, parses what comes
back, and reports whether at least one item was found — nothing is written to the
database, and there is no Gemini or Telegram call anywhere in this module.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.settings import Settings, get_settings
from ai_news_editor.sources.base import FetchContext
from ai_news_editor.sources.config import TIER_LABELS, SourcesConfig, load_sources_config
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.sources.registry import build_adapter

console = Console()
err_console = Console(stderr=True)

sources_app = typer.Typer(
    name="sources", help="Source registry: list and diagnose.", invoke_without_command=True
)

#: Small — this is a reachability probe, not a real collection run. Enough to tell
#: "the endpoint returns at least one parseable item" from "it doesn't."
_PROBE_MAX_ITEMS = 5


def _settings() -> Settings:
    try:
        return get_settings()
    except AiNewsError as exc:
        err_console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


def _config(settings: Settings) -> SourcesConfig:
    try:
        return load_sources_config(settings.sources_config_path)
    except AiNewsError as exc:
        err_console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


@sources_app.callback()
def _sources_default(ctx: typer.Context) -> None:
    """List configured sources and their last fetch outcome.

    Bare ``ai-news sources`` lists; ``ai-news sources doctor`` probes them live.
    """
    if ctx.invoked_subcommand is not None:
        return

    config = _config(_settings())

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


@sources_app.command("doctor")
def sources_doctor(
    include_disabled: Annotated[
        bool,
        typer.Option(
            "--include-disabled",
            help="Also probe disabled/future sources (most are expected to still fail "
            "— that's why they're disabled — but this checks the documented reason is "
            "still current).",
        ),
    ] = False,
) -> None:
    """Smoke-test every enabled source's discovery endpoint.

    For each source, calls the same adapter ``collect`` uses, once, and reports
    whether the endpoint resolved and at least one item parsed. Nothing is written to
    the database and no Gemini/Telegram call is made anywhere in this command.
    """
    config = _config(_settings())
    targets = config.sources if include_disabled else config.enabled()

    table = Table(title="ai-news sources doctor")
    table.add_column("Source", overflow="fold")
    table.add_column("Tier")
    table.add_column("Enabled")
    table.add_column("Discovery")
    table.add_column("Parse")

    failures = 0
    with HttpClient() as http:
        for definition in targets:
            adapter = build_adapter(definition.adapter, http)
            source = definition.to_source(config.defaults)
            context = FetchContext(run_id="sources-doctor", max_items=_PROBE_MAX_ITEMS)
            result = adapter.fetch(source, context)

            tier = TIER_LABELS.get(definition.trust_tier, definition.trust_tier.value)
            enabled_cell = "[green]YES[/green]" if definition.enabled else "[dim]NO[/dim]"

            if result.ok:
                discovery = "[green]OK[/green]"
                if result.items:
                    parse = f"[green]OK[/green] ({len(result.items)})"
                else:
                    parse = "[yellow]EMPTY[/yellow]"
                    if definition.enabled:
                        failures += 1
            else:
                status = result.http_status if result.http_status is not None else "n/a"
                discovery = f"[red]FAIL[/red] ({status})"
                parse = "[dim]—[/dim]"
                if definition.enabled:
                    failures += 1

            table.add_row(definition.name, tier, enabled_cell, discovery, parse)

    console.print(table)
    console.print("[dim]No Gemini or Telegram calls were made; nothing was written.[/dim]")

    if failures:
        err_console.print(f"\n[bold red]{failures} enabled source(s) failed.[/bold red]")
        raise typer.Exit(code=1)
