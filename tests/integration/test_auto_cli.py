"""``ai-news auto once`` / ``auto run`` — the CLI's exit code, not the pipeline logic.

The pipeline (``automation.pipeline``) is exercised end-to-end elsewhere (see
``tests/safety/test_automation.py``). What's specific to this thin CLI wrapper is the
one decision it makes on its own: which ``AutomationResult`` outcomes are worth a
nonzero exit code, so a scheduled GitHub Actions run only turns red for a genuine
infrastructure failure. ``_run_once`` is monkeypatched to return a fixed result so each
outcome can be checked in isolation, without driving collection/selection/Gemini/
Telegram.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_news_editor.automation.pipeline import AutomationResult, Outcome
from ai_news_editor.cli.main import app
from ai_news_editor.settings import get_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AI_NEWS_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _patch_result(monkeypatch: pytest.MonkeyPatch, result: AutomationResult) -> None:
    import ai_news_editor.cli.auto as auto_module

    monkeypatch.setattr(auto_module, "_run_once", lambda *, mode: result)


class TestAutoOnceExitCode:
    def test_a_successful_publish_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The one outcome this bug actually broke: a real, successful send to
        Telegram must not make a scheduled workflow's 'Run automation' step red."""
        _patch_result(
            monkeypatch,
            AutomationResult(Outcome.PUBLISHED, "published to @test_channel.", message_id=3,
                              channel="@test_channel"),
        )
        result = runner.invoke(app, ["auto", "once", "--test"])
        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize(
        "outcome",
        [
            Outcome.DRY_RUN_COMPLETE,
            Outcome.DISABLED,
            Outcome.DAILY_LIMIT_REACHED,
            Outcome.NO_CANDIDATE,
            Outcome.SELECTION_REJECTED,
            Outcome.FULLTEXT_UNAVAILABLE,
            Outcome.GENERATION_REJECTED,
            Outcome.VALIDATION_FAILED,
            Outcome.CANDIDATES_EXHAUSTED,
        ],
    )
    def test_a_quiet_no_op_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, outcome: Outcome
    ) -> None:
        _patch_result(monkeypatch, AutomationResult(outcome, "detail"))
        result = runner.invoke(app, ["auto", "once"])
        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize(
        "outcome", [Outcome.CONFIG_ERROR, Outcome.GEMINI_ERROR, Outcome.PUBLISH_ERROR]
    )
    def test_a_genuine_infrastructure_failure_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, outcome: Outcome
    ) -> None:
        _patch_result(monkeypatch, AutomationResult(outcome, "detail"))
        result = runner.invoke(app, ["auto", "once"])
        assert result.exit_code == 1, result.output


class TestAutoRunExitCode:
    def test_a_successful_publish_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_result(
            monkeypatch,
            AutomationResult(Outcome.PUBLISHED, "published.", message_id=1, channel="@prod"),
        )
        result = runner.invoke(app, ["auto", "run"])
        assert result.exit_code == 0, result.output
