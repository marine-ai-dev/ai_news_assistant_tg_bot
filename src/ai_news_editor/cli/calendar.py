"""``ai-news calendar`` — see the week, and what is uneven about it.

Read-only, all of it. Nothing in this command group schedules, reorders, approves or
publishes anything; the strongest thing it does is print a sentence suggesting a time.
Acting on that sentence is `ai-news queue add`, typed by a person.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import AudienceTier, ContentType
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.planning.buckets import (
    ACCESSIBLE_TARGET_MAX,
    ACCESSIBLE_TARGET_MIN,
    BUCKET_LABELS,
    DEFAULT_MIX,
    Bucket,
)
from ai_news_editor.planning.calendar import (
    Entry,
    Freshness,
    Week,
    approved_unscheduled,
    build_week,
    pending_summary,
)
from ai_news_editor.planning.suggest import suggest_slot
from ai_news_editor.scheduling.clock import to_local
from ai_news_editor.settings import Settings, get_settings

console = Console()
err_console = Console(stderr=True)

calendar_app = typer.Typer(
    name="calendar",
    help="See the publication week, its balance, and where a post could go. Read-only.",
)

_TYPE_ICONS = {
    ContentType.NEWS: "📰",
    ContentType.PROMPT: "✨",
    ContentType.EXPLAINER: "🧠",
    ContentType.TESTED_USE_CASE: "🛠",
    ContentType.RESOURCE: "📚",
}

_AUDIENCE_ICONS = {
    AudienceTier.NEWCOMER: "🌱 NEWCOMER",
    AudienceTier.BEGINNER: "🙂 BEGINNER",
    AudienceTier.GENERAL: "👤 GENERAL",
    AudienceTier.TECH_CURIOUS: "🧩 TECH_CURIOUS",
}

_FRESHNESS_MARK = {
    Freshness.FRESH: "[green]✅ fresh[/green]",
    Freshness.AGING: "[yellow]⏳ ageing[/yellow]",
    Freshness.EXPIRED: "[red]⚠️ past its window[/red]",
}


def _settings() -> Settings:
    try:
        return get_settings()
    except AiNewsError as exc:
        err_console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


def _label(entry: Entry) -> str:
    icon = _TYPE_ICONS.get(entry.content_type, "•")
    if entry.is_lifehack:
        return "💡 LIFEHACK"
    return f"{icon} {entry.content_type.value}"


def _print_week(week: Week) -> None:
    if not week.entries:
        console.print(
            f"[dim]Nothing scheduled between {week.start:%d %b} and {week.end:%d %b}.[/dim]"
        )
    for day, entries in week.by_day.items():
        console.print(f"\n[bold]{day:%a %d %b}[/bold]".replace(
            f"{day:%a %d %b}", f"{day:%a %d %b}".upper()
        ))
        for entry in entries:
            series = ""
            if entry.series is not None:
                name, order = entry.series
                series = f"  [magenta]🎯 {name} · {order}[/magenta]"
            console.print(
                f"  [cyan]{entry.local_time:%H:%M}[/cyan]  {_label(entry)}"
                f"  [dim]{entry.item.status.value}[/dim]{series}"
            )
            console.print(f"         {entry.version.title[:66]}")
            provenance = []
            if entry.source_id:
                provenance.append(entry.source_id)
            if entry.tool:
                provenance.append(f"🔧 {entry.tool}")
            if entry.source_tier:
                provenance.append(entry.source_tier.value)
            console.print(
                f"         [dim]{_AUDIENCE_ICONS[entry.audience]}"
                + (f"  ·  {' · '.join(provenance)}" if provenance else "")
                + "[/dim]  "
                + _FRESHNESS_MARK[entry.freshness]
            )


def _print_warnings(week: Week) -> None:
    if not week.warnings:
        return
    console.print()
    for warning in week.warnings:
        mark = "[yellow]⚠️[/yellow]" if warning.severity == "NOTE" else "[green]✅[/green]"
        console.print(f"{mark} {warning.message}")


@calendar_app.command("week")
def calendar_week(
    next_week: Annotated[
        bool, typer.Option("--next", help="Show next week instead of this one.")
    ] = False,
    weeks_ahead: Annotated[
        int, typer.Option("--ahead", help="Show a week this many weeks from now.", min=0)
    ] = 0,
) -> None:
    """The publication week, in Europe/Kyiv, with what is uneven about it."""
    from ai_news_editor.cli.main import open_migrated_database

    offset = 1 if next_week else weeks_ahead
    connection = open_migrated_database()
    try:
        week = build_week(connection, now=now_utc(), offset=offset)
        console.print(
            f"[bold]Week of {week.start:%d %b} – {week.end:%d %b %Y}[/bold]  "
            "[dim](Europe/Kyiv)[/dim]"
        )
        _print_week(week)
        _print_warnings(week)
    finally:
        connection.close()


@calendar_app.command("balance")
def calendar_balance(
    next_week: Annotated[bool, typer.Option("--next", help="Look at next week.")] = False,
) -> None:
    """How the week's mix compares with the editorial targets."""
    from ai_news_editor.cli.main import open_migrated_database

    connection = open_migrated_database()
    try:
        week = build_week(connection, now=now_utc(), offset=1 if next_week else 0)
        total = len(week.entries)
        if total == 0:
            console.print("[dim]Nothing scheduled — there is no mix to report yet.[/dim]")
            return

        table = Table(title=f"Editorial mix · {week.start:%d %b} – {week.end:%d %b}")
        table.add_column("bucket")
        table.add_column("scheduled", justify="right")
        table.add_column("share", justify="right")
        table.add_column("target", justify="right")
        table.add_column("")

        mix = week.mix
        for bucket in Bucket:
            count = mix[bucket]
            share = count / total
            target = DEFAULT_MIX[bucket]
            if count == 0 and target >= 0.15:
                mark = "[yellow]missing[/yellow]"
            elif share > target * 2 and count >= 3:
                mark = "[yellow]heavy[/yellow]"
            else:
                mark = "[green]ok[/green]"
            table.add_row(
                BUCKET_LABELS[bucket], str(count), f"{share:.0%}", f"{target:.0%}", mark
            )
        console.print(table)

        share = week.accessible_share
        colour = "green" if share >= ACCESSIBLE_TARGET_MIN else "yellow"
        console.print(
            f"\n[{colour}]{share:.0%}[/{colour}] of {total} posts are beginner-accessible "
            f"[dim](target {ACCESSIBLE_TARGET_MIN:.0%}–{ACCESSIBLE_TARGET_MAX:.0%})[/dim]"
        )
        console.print(
            "[dim]These are editorial targets, not quotas. Good content is not worth "
            "dropping to make a percentage tidier.[/dim]"
        )
        _print_warnings(week)
    finally:
        connection.close()


@calendar_app.command("gaps")
def calendar_gaps() -> None:
    """What is approved and waiting for a slot, and what is still awaiting review."""
    from ai_news_editor.cli.main import open_migrated_database

    settings = _settings()
    channel = settings.telegram_channel or ""
    connection = open_migrated_database()
    try:
        now = now_utc()
        waiting = approved_unscheduled(connection, now=now, channel=channel)

        if waiting:
            table = Table(title="Approved, not scheduled")
            table.add_column("draft", style="dim")
            table.add_column("type")
            table.add_column("audience")
            table.add_column("freshness")
            table.add_column("title")
            for candidate in waiting:
                table.add_row(
                    str(candidate.draft.id)[:8],
                    f"{_TYPE_ICONS.get(candidate.draft.content_type, '•')} "
                    f"{candidate.draft.content_type.value}",
                    _AUDIENCE_ICONS[candidate.version.audience],
                    _FRESHNESS_MARK[candidate.freshness],
                    candidate.version.title[:46],
                )
            console.print(table)
            console.print(
                "[dim]Nothing here is scheduled. 'ai-news calendar suggest <draft-id>' "
                "proposes a time; 'ai-news queue add' is what actually schedules it.[/dim]"
            )
        else:
            console.print("[dim]No approved drafts are waiting for a slot.[/dim]")

        pending = pending_summary(connection)
        if pending:
            summary = ", ".join(f"{count} {name}" for name, count in pending.items())
            console.print(
                f"\n[dim]Awaiting review (not publishable): {summary}. "
                "Run 'ai-news review'.[/dim]"
            )

        week = build_week(connection, now=now, offset=0)
        _print_warnings(week)
    finally:
        connection.close()


@calendar_app.command("suggest")
def calendar_suggest(
    draft_id: Annotated[str, typer.Argument(help="An approved draft to find a slot for.")],
    show: Annotated[
        int, typer.Option("--show", help="How many candidate slots to list.", min=1, max=20)
    ] = 5,
) -> None:
    """Propose publication slots for one approved draft, with the reasoning.

    This schedules nothing. It prints times and the argument for each; creating the
    queue item is a separate, deliberate act.
    """
    from ai_news_editor.cli.main import open_migrated_database

    settings = _settings()
    try:
        identifier = UUID(draft_id.strip())
    except ValueError as exc:
        err_console.print(f"[bold red]Not a draft id:[/bold red] {draft_id!r}")
        raise typer.Exit(code=1) from exc

    connection = open_migrated_database()
    try:
        try:
            suggestion = suggest_slot(
                connection, identifier, now=now_utc(),
                channel=settings.telegram_channel or "",
            )
        except AiNewsError as exc:
            err_console.print(f"[bold red]Cannot suggest a slot:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc

        console.print(f"\n[bold]{BUCKET_LABELS[suggestion.bucket]}[/bold]")
        for note in suggestion.notes:
            console.print(f"[dim]{note}[/dim]")

        usable = [c for c in suggestion.candidates if not c.blocked][:show]
        if not usable:
            console.print(
                "\n[yellow]No usable slot in the next days.[/yellow] Every candidate "
                "collides with something already scheduled or falls past this draft's "
                "freshness window."
            )
            return

        console.print("\n[bold]Suggested slots[/bold] [dim](nothing has been scheduled)[/dim]")
        for rank, candidate in enumerate(usable, start=1):
            marker = "[green]→[/green]" if rank == 1 else " "
            console.print(
                f"\n{marker} [bold]{candidate.describe()}[/bold]  "
                f"[dim]{candidate.daypart} · score {candidate.score:+d}[/dim]"
            )
            for reason in candidate.reasons:
                colour = "green" if reason.points > 0 else "yellow"
                console.print(
                    f"      [{colour}]{reason.sign}[/{colour}] {reason.text}"
                )

        best = usable[0]
        console.print(
            f"\n[dim]To schedule it: ai-news queue add {draft_id} "
            f'--at "{to_local(best.when):%d.%m %H:%M}"[/dim]'
        )
        console.print(
            "[dim]Every reason above is arithmetic over what is already scheduled — "
            "there is no engagement model here, and no claim about when readers are most "
            "likely to look.[/dim]"
        )
    finally:
        connection.close()
