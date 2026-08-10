"""Local health checks behind the ``doctor`` command.

Returns structured results rather than printing, so the CLI stays a presentation layer
and the checks can be asserted on directly.

Deliberately local-only: this inspects the interpreter, configuration, data directory
and database. It contacts nothing. There are no external integrations yet, and when
there are, reaching them must be an explicit opt-in rather than a side effect of asking
whether the app is healthy.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.settings import Settings
from ai_news_editor.storage import db

MIN_PYTHON = (3, 11)


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """One check and its outcome."""

    name: str
    ok: bool
    detail: str


def _python_check() -> HealthCheck:
    version = sys.version_info
    return HealthCheck(
        name="Python version",
        ok=version >= MIN_PYTHON,
        detail=(
            f"{version.major}.{version.minor}.{version.micro} "
            f"(requires >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})"
        ),
    )


def _data_dir_check(settings: Settings) -> HealthCheck:
    try:
        directory = settings.ensure_data_dir()
        probe = directory / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return HealthCheck("Data directory writable", False, f"{exc}")
    return HealthCheck("Data directory writable", True, str(directory))


def _database_checks(path: Path) -> list[HealthCheck]:
    if not path.exists():
        return [
            HealthCheck(
                "Database present",
                False,
                f"{path} does not exist - run 'ai-news db init'",
            )
        ]

    try:
        connection = db.connect(path)
    except (sqlite3.Error, OSError) as exc:
        return [HealthCheck("Database connectivity", False, str(exc))]

    try:
        applied = db.schema_version(connection)
        pending = db.pending_migrations(connection)
    except (sqlite3.Error, AiNewsError) as exc:
        return [HealthCheck("Database connectivity", False, str(exc))]
    finally:
        connection.close()

    detail = f"applied version {applied}"
    if pending:
        detail += f", {len(pending)} pending - run 'ai-news db migrate'"
    return [
        HealthCheck("Database connectivity", True, str(path)),
        HealthCheck("Schema up to date", not pending, detail),
    ]


def run_health_checks(settings: Settings) -> list[HealthCheck]:
    """Run every local check, in display order."""
    checks = [
        _python_check(),
        HealthCheck("Configuration", True, f"environment={settings.environment}"),
        HealthCheck(
            "Auto-publish disabled",
            not settings.auto_publish_enabled,
            "every post requires explicit human approval",
        ),
        _data_dir_check(settings),
    ]
    checks.extend(_database_checks(settings.resolved_database_path))
    return checks


def all_ok(checks: list[HealthCheck]) -> bool:
    """Whether every check passed."""
    return all(check.ok for check in checks)
