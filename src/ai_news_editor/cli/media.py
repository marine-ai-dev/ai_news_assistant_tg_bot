"""The ``ai-news media`` command group — Step 4 (AI News Agent v2), section 33.

One command: discover, download, validate and compress media for an article URL, and
report what happened. Never publishes to Telegram, never touches Gemini — a developer-
facing tool for seeing what the pipeline would do, safely, before anything real
happens to it.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from ai_news_editor.domain.enums import MediaPolicy
from ai_news_editor.media.pipeline import select_media
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.sources.http import HttpClient, HttpError
from ai_news_editor.sources.http import UnsafeUrlError as UnsafeArticleUrlError

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="media",
    help="Inspect what the media pipeline would discover/process for one article. "
    "Never publishes to Telegram.",
    no_args_is_help=True,
)


@app.command("inspect")
def inspect_media(
    url: Annotated[str, typer.Argument(help="Article page URL to inspect for media.")],
    policy: Annotated[
        MediaPolicy,
        typer.Option(
            "--policy",
            help="Media policy to simulate, as if this were the article's source.",
        ),
    ] = MediaPolicy.DISCOVER_MEDIA,
) -> None:
    """Discover, download, validate and compress media for ``url``.

    Fetches the page's HTML through the existing trusted ``sources.http.HttpClient``
    (the same boundary every source adapter uses), then runs the real media pipeline
    against it — the same discovery, policy gate, SSRF-checked download, and
    compression a real publication would use. The processed file is written to a
    temporary workspace and reported, then removed: this command never leaves a file
    behind and never calls Telegram.
    """
    try:
        with HttpClient() as http:
            response = http.get(url)
    except (HttpError, UnsafeArticleUrlError) as exc:
        err_console.print(f"[bold red]Could not fetch {url}:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc

    html = response.body.decode("utf-8", errors="replace")

    with MediaWorkspace(label="inspect") as workspace:
        outcome = select_media(
            workspace=workspace, source_url=url, media_policy=policy, html=html
        )

        if not outcome.ok:
            reason = outcome.reason.value if outcome.reason else "(none)"
            console.print(f"[yellow]No media selected.[/yellow] reason={reason}")
            if outcome.detail:
                console.print(f"[dim]{outcome.detail}[/dim]")
            console.print(
                "[dim]This is the normal text-only-fallback outcome, not an error — "
                "see docs/media.md.[/dim]"
            )
            return

        media = outcome.media
        assert media is not None  # outcome.ok already confirmed this
        table = Table(title=f"Media for {url}")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("Kind", media.kind.value)
        table.add_row("Discovery method", media.source_method.value)
        table.add_row("Source URL", media.source_url)
        table.add_row("Dimensions", f"{media.width}x{media.height}")
        table.add_row("Size", f"{media.size_bytes / 1024:.0f} KB")
        console.print(table)
        console.print(f"[dim]Workspace: {workspace.root} (removed after this command exits)[/dim]")
