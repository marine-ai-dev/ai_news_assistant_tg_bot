"""CLI behaviour, driven through Typer's runner against temporary databases."""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_news_editor.cli.main import app
from ai_news_editor.health import HealthCheck, all_ok, run_health_checks
from ai_news_editor.settings import Settings, get_settings
from ai_news_editor.storage import db

runner = CliRunner()


def output_of(result: object) -> str:
    """All output as one whitespace-normalized string.

    Rich wraps table cells at the terminal width, so a phrase can be split across
    lines; errors go to stderr while normal output goes to stdout. Both would make
    naive substring assertions flaky for reasons that have nothing to do with
    behaviour.
    """
    parts = [getattr(result, "output", "") or ""]
    with contextlib.suppress(AttributeError, ValueError):
        parts.append(result.stderr or "")  # type: ignore[attr-defined]
    return " ".join(" ".join(parts).split())


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every CLI invocation at a throwaway data directory."""
    monkeypatch.setenv("AI_NEWS_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestHelp:
    def test_root_help_lists_the_real_commands(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for command in ("version", "doctor", "db"):
            assert command in output_of(result)

    def test_no_arguments_shows_help(self) -> None:
        assert runner.invoke(app, []).exit_code != 0

    @pytest.mark.parametrize("command", ["collect", "review", "publish", "evaluate", "draft"])
    def test_future_commands_are_absent_rather_than_hollow(self, command: str) -> None:
        """A command that exists but does nothing is worse than one that does not exist."""
        assert runner.invoke(app, [command]).exit_code != 0

    def test_version(self) -> None:
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "ai-news" in output_of(result)


class TestDbInit:
    def test_creates_the_database(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["db", "init"])
        assert result.exit_code == 0
        assert (tmp_path / "ai_news.sqlite3").exists()
        assert "001_initial" in output_of(result)

    def test_running_twice_is_safe(self) -> None:
        assert runner.invoke(app, ["db", "init"]).exit_code == 0
        second = runner.invoke(app, ["db", "init"])
        assert second.exit_code == 0
        assert "none" in output_of(second)

    def test_migrate_is_equivalent(self, tmp_path: Path) -> None:
        assert runner.invoke(app, ["db", "migrate"]).exit_code == 0
        assert (tmp_path / "ai_news.sqlite3").exists()


class TestDbStatus:
    def test_reports_applied_migrations_and_counts(self) -> None:
        runner.invoke(app, ["db", "init"])
        result = runner.invoke(app, ["db", "status"])
        assert result.exit_code == 0
        assert "initial" in output_of(result)
        assert "review_decisions" in output_of(result)

    def test_fails_clearly_without_a_database(self) -> None:
        result = runner.invoke(app, ["db", "status"])
        assert result.exit_code == 1
        assert "db init" in output_of(result)


class TestDoctorCommand:
    def test_passes_on_a_migrated_database(self) -> None:
        runner.invoke(app, ["db", "init"])
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "FAIL" not in output_of(result)

    def test_fails_before_the_database_exists(self) -> None:
        assert runner.invoke(app, ["doctor"]).exit_code == 1

    def test_states_that_nothing_was_contacted(self) -> None:
        runner.invoke(app, ["db", "init"])
        assert "No external services were contacted" in output_of(runner.invoke(app, ["doctor"]))

    def test_refuses_to_start_with_auto_publish_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_NEWS_AUTO_PUBLISH_ENABLED", "true")
        get_settings.cache_clear()
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 2
        assert "Configuration error" in output_of(result)


class TestHealthChecks:
    """The checks themselves, asserted as data rather than as rendered terminal output."""

    def _checks(self, tmp_path: Path) -> dict[str, HealthCheck]:
        settings = Settings(data_dir=tmp_path, _env_file=None)  # type: ignore[call-arg]
        return {check.name: check for check in run_health_checks(settings)}

    def test_reports_python_and_configuration(self, tmp_path: Path) -> None:
        checks = self._checks(tmp_path)
        assert checks["Python version"].ok
        assert checks["Configuration"].ok

    def test_reports_the_approval_guarantee(self, tmp_path: Path) -> None:
        check = self._checks(tmp_path)["Auto-publish disabled"]
        assert check.ok
        assert "human approval" in check.detail

    def test_data_directory_is_writable(self, tmp_path: Path) -> None:
        assert self._checks(tmp_path)["Data directory writable"].ok

    def test_missing_database_is_reported_with_the_remedy(self, tmp_path: Path) -> None:
        check = self._checks(tmp_path)["Database present"]
        assert check.ok is False
        assert "db init" in check.detail

    def test_migrated_database_reports_schema_up_to_date(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, _env_file=None)  # type: ignore[call-arg]
        connection = db.connect(settings.resolved_database_path)
        db.migrate(connection)
        connection.close()

        checks = {check.name: check for check in run_health_checks(settings)}
        assert checks["Database connectivity"].ok
        assert checks["Schema up to date"].ok
        assert all_ok(list(checks.values()))

    def test_pending_migration_is_reported_with_the_remedy(self, tmp_path: Path) -> None:
        """An out-of-date schema must fail loudly rather than be silently tolerated."""
        settings = Settings(data_dir=tmp_path, _env_file=None)  # type: ignore[call-arg]
        connection = db.connect(settings.resolved_database_path)
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.close()

        check = {c.name: c for c in run_health_checks(settings)}["Schema up to date"]
        assert check.ok is False
        assert "pending" in check.detail

    def test_unreadable_database_file_is_reported_not_raised(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, _env_file=None)  # type: ignore[call-arg]
        settings.resolved_database_path.write_text("not a database", encoding="utf-8")

        checks = {c.name: c for c in run_health_checks(settings)}
        assert checks["Database connectivity"].ok is False
