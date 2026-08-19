"""``ai-news editorial preview --all-categories`` — Step 5 section 46.

Entirely offline: no database, no network, no Gemini, no Telegram. Confirmed here by
never wiring a database or a mock transport at all — if the command tried to reach
either, this test would fail for lack of one.
"""

from __future__ import annotations

import contextlib

from typer.testing import CliRunner

from ai_news_editor.cli.main import app
from ai_news_editor.domain.enums import EditorialCategory

runner = CliRunner()


def output_of(result: object) -> str:
    parts = [getattr(result, "output", "") or ""]
    with contextlib.suppress(AttributeError, ValueError):
        parts.append(result.stderr or "")  # type: ignore[attr-defined]
    return " ".join(" ".join(parts).split())


class TestAllCategoriesGallery:
    def test_exits_zero_with_no_database_or_network_setup(self) -> None:
        result = runner.invoke(app, ["editorial", "preview", "--all-categories"])
        assert result.exit_code == 0

    def test_shows_every_category(self) -> None:
        output = output_of(runner.invoke(app, ["editorial", "preview", "--all-categories"]))
        for category in EditorialCategory:
            assert category.value in output

    def test_shows_rendered_headlines_not_just_labels(self) -> None:
        output = output_of(runner.invoke(app, ["editorial", "preview", "--all-categories"]))
        assert "ExampleCorp" in output
        assert "Джерело" in output
