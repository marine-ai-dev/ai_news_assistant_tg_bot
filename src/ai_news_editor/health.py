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


def _service_checks(settings: Settings) -> list[HealthCheck]:
    """Whether the long-running processes could actually start.

    Added for unattended operation. On a laptop a missing Telegram setting is normal —
    the whole pipeline up to review works without one — so an absent configuration is
    reported rather than failed. What *is* a failure is a half-configuration: a token
    with no channel, or a token with no owner id. On a server those produce a process
    that exits on startup and is restarted forever, with the real reason buried in a
    log nobody is reading.

    Local only, like every other check here. Nothing below contacts Telegram; use
    'ai-news telegram doctor' for that.
    """
    token = settings.telegram_bot_token is not None
    channel = bool(settings.telegram_channel)
    owner = settings.telegram_owner_user_id is not None

    if not token:
        return [
            HealthCheck(
                "Telegram configured",
                True,
                "no token set - collection and review work; publishing is disabled",
            )
        ]

    checks = [
        HealthCheck(
            "Scheduler ready",
            channel,
            "token and channel set"
            if channel
            else "AI_NEWS_TELEGRAM_CHANNEL is empty - 'scheduler run' will exit at startup",
        ),
        HealthCheck(
            "Review bot ready",
            owner,
            "token and owner id set"
            if owner
            else (
                "AI_NEWS_TELEGRAM_OWNER_USER_ID is empty - 'telegram review-bot' will "
                "exit at startup. Find it with 'ai-news telegram whoami'"
            ),
        ),
    ]
    return checks


def _automation_checks(settings: Settings) -> list[HealthCheck]:
    """Whether the unattended NEWS pipeline (``ai-news auto``) could actually run.

    Local and lightweight, like every other check here: it reads settings, never calls
    Gemini. Both checks are informational either way, same as the Telegram checks
    above and for the same reason: a laptop doing collection and human review only,
    with no Gemini key and the kill switch off, is a normal, healthy state, not a
    warning — ``ai-news auto`` is opt-in, and this command must not start insisting on
    configuration a reader who never touches it does not need. The actual fail-closed
    guarantee for a missing key lives in ``automation.pipeline.run_automation`` itself
    (checked in every mode, including ``--dry-run``), which is what makes it safe for
    doctor to only report here rather than enforce.
    """
    has_key = settings.gemini_api_key is not None
    return [
        HealthCheck(
            "Gemini configured",
            True,
            f"model={settings.llm_model}"
            if has_key
            else "no key set - 'ai-news auto' will fail closed in every mode, "
            "including --dry-run, if it is ever run",
        ),
        HealthCheck(
            "Live automation enabled",
            True,
            "AI_NEWS_AUTOMATION_ENABLED is set - scheduled and live runs may publish"
            if settings.automation_enabled
            else "AI_NEWS_AUTOMATION_ENABLED is not set - scheduled and live runs are "
            "a safe no-op; dry-run and test remain available",
        ),
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
    checks.extend(_service_checks(settings))
    checks.extend(_automation_checks(settings))
    return checks


def all_ok(checks: list[HealthCheck]) -> bool:
    """Whether every check passed."""
    return all(check.ok for check in checks)
