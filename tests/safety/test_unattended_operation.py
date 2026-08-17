"""Running unattended on a server. Never skip these.

On a laptop a failure is visible: somebody is looking at the terminal. On a server the
same failure is a restart loop nobody notices for a week, or a process killed halfway
through recording a human's decision.

These tests cover the three things that change when nobody is watching:

**A restart must not interrupt a decision.** Both long-running processes stop at a safe
boundary on SIGTERM, because that is how a service manager asks them to stop.

**A misconfiguration must be loud and specific.** `doctor` exits non-zero and names the
missing setting, so the deploy script can refuse to start anything.

**Deployment must not be able to skip a migration.** Nothing starts a process that
would run against a schema it does not understand.
"""

from __future__ import annotations

import re
import shutil
import signal
import sqlite3
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_news_editor.bot.api import BotApi
from ai_news_editor.bot.review_bot import ReviewBot, poll, stop_on_signal
from ai_news_editor.bot.session import Session
from ai_news_editor.health import all_ok, run_health_checks
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.settings import Settings
from ai_news_editor.storage import db

pytestmark = pytest.mark.safety

TOKEN = "123456789:" + "A" * 35
OWNER = 424242
DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _api(updates: list[dict[str, Any]] | None = None) -> tuple[BotApi, TelegramClient]:
    """A bot API backed by a transport that answers everything and reaches nothing."""
    queued = list(updates or [])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getUpdates"):
            batch, queued[:] = list(queued), []
            return httpx.Response(200, json={"ok": True, "result": batch})
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": 1}}}
        )

    client = TelegramClient(TOKEN, transport=httpx.MockTransport(handler))
    return BotApi(client), client


class TestGracefulShutdown:
    """SIGTERM is how a service manager says stop. It must be obeyed cleanly."""

    def test_a_set_stop_event_ends_the_poll_loop(
        self, connection: sqlite3.Connection
    ) -> None:
        api, client = _api()
        with client:
            bot = ReviewBot(api=api, connection=connection, owner_id=OWNER, session=Session())
            stop = threading.Event()
            stop.set()
            # iterations=None means "run until stopped"; without the event this hangs.
            assert list(poll(bot, iterations=None, stop=stop)) == []

    def test_the_loop_stops_between_updates_not_inside_one(
        self, connection: sqlite3.Connection
    ) -> None:
        """A decision already committed must not lose its confirmation to a signal."""
        updates = [
            {"update_id": n, "message": {"message_id": n, "text": "/status",
                                         "chat": {"id": OWNER}, "from": {"id": OWNER}}}
            for n in (1, 2, 3)
        ]
        api, client = _api(updates)
        stop = threading.Event()

        with client:
            bot = ReviewBot(api=api, connection=connection, owner_id=OWNER, session=Session())
            handled = []
            for update_id in poll(bot, iterations=None, stop=stop):
                handled.append(update_id)
                stop.set()  # asked to stop during the first update

        # It finished the update it was on and then stopped, rather than abandoning it.
        assert handled == [1]

    def test_the_signal_handler_is_installed_and_restored(self) -> None:
        original = signal.getsignal(signal.SIGTERM)
        with stop_on_signal() as stop:
            assert not stop.is_set()
            assert signal.getsignal(signal.SIGTERM) is not original
            signal.raise_signal(signal.SIGTERM)
            assert stop.is_set(), "SIGTERM must set the stop event, not kill the process"
        assert signal.getsignal(signal.SIGTERM) is original

    def test_sigint_stops_it_too(self) -> None:
        with stop_on_signal() as stop:
            signal.raise_signal(signal.SIGINT)
            assert stop.is_set()

    def test_the_scheduler_has_the_same_protection(self) -> None:
        """Both long-running processes, not just one."""
        from ai_news_editor.scheduling.worker import _stop_on_signal

        original = signal.getsignal(signal.SIGTERM)
        with _stop_on_signal() as stop:
            signal.raise_signal(signal.SIGTERM)
            assert stop.is_set()
        assert signal.getsignal(signal.SIGTERM) is original


class TestDoctorGuardsTheDeployment:
    """The deploy script refuses to start anything unless doctor passes."""

    def _settings(self, tmp_path: Path, **overrides: object) -> Settings:
        data = {"data_dir": tmp_path, "_env_file": None, **overrides}
        return Settings(**data)  # type: ignore[arg-type]

    def test_a_stale_schema_fails_the_health_check(self, tmp_path: Path) -> None:
        """The single most likely deployment failure: code updated, schema not."""
        partial = tmp_path / "migrations"
        partial.mkdir()
        for migration in sorted(db.MIGRATIONS_DIR.glob("*.sql"))[:5]:
            shutil.copy(migration, partial / migration.name)

        connection = db.connect(tmp_path / "ai_news.sqlite3")
        db.migrate(connection, partial)
        connection.close()

        checks = run_health_checks(self._settings(tmp_path))
        schema = next(c for c in checks if c.name == "Schema up to date")
        assert not schema.ok
        assert "db migrate" in schema.detail
        assert not all_ok(checks)

    def test_a_current_schema_passes(self, tmp_path: Path) -> None:
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        db.migrate(connection)
        connection.close()

        checks = run_health_checks(self._settings(tmp_path))
        assert next(c for c in checks if c.name == "Schema up to date").ok

    def test_a_missing_owner_id_names_the_setting_and_the_process(
        self, tmp_path: Path
    ) -> None:
        """Otherwise this is a restart loop whose cause is buried in a log."""
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        db.migrate(connection)
        connection.close()

        checks = run_health_checks(
            self._settings(
                tmp_path, telegram_bot_token=TOKEN, telegram_channel="@c",
                telegram_owner_user_id=None,
            )
        )
        bot_check = next(c for c in checks if c.name == "Review bot ready")
        assert not bot_check.ok
        assert "AI_NEWS_TELEGRAM_OWNER_USER_ID" in bot_check.detail
        assert "review-bot" in bot_check.detail
        assert not all_ok(checks)

    def test_a_missing_channel_names_the_scheduler(self, tmp_path: Path) -> None:
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        db.migrate(connection)
        connection.close()

        checks = run_health_checks(
            self._settings(tmp_path, telegram_bot_token=TOKEN, telegram_owner_user_id=1)
        )
        scheduler = next(c for c in checks if c.name == "Scheduler ready")
        assert not scheduler.ok
        assert "AI_NEWS_TELEGRAM_CHANNEL" in scheduler.detail

    def test_no_telegram_configuration_is_not_a_failure(self, tmp_path: Path) -> None:
        """A laptop running the pipeline up to review is healthy, not broken."""
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        db.migrate(connection)
        connection.close()

        checks = run_health_checks(self._settings(tmp_path))
        assert all_ok(checks)
        assert any("publishing is disabled" in c.detail for c in checks)

    def test_a_fully_configured_server_passes_everything(self, tmp_path: Path) -> None:
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        db.migrate(connection)
        connection.close()

        checks = run_health_checks(
            self._settings(
                tmp_path, telegram_bot_token=TOKEN, telegram_channel="@c",
                telegram_owner_user_id=OWNER,
            )
        )
        assert all_ok(checks)

    def test_the_health_check_still_contacts_nothing(self, tmp_path: Path) -> None:
        """Adding service checks must not have made doctor reach the network."""
        source = Path(__file__).resolve().parents[2] / "src/ai_news_editor/health.py"
        text = source.read_text(encoding="utf-8")
        for forbidden in ("httpx", "requests", "urlopen", "TelegramClient", "GeminiClient",
                          "socket"):
            assert forbidden not in text, forbidden

    def test_automation_off_is_not_a_failure(self, tmp_path: Path) -> None:
        """The kill switch being off is the default, healthy state, not a warning —
        it only gates live/scheduled publishing, so dry-run and test stay unaffected."""
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        db.migrate(connection)
        connection.close()

        key = "AIzaSy" + "z" * 33
        checks = run_health_checks(
            self._settings(tmp_path, automation_enabled=False, gemini_api_key=key)
        )
        assert all_ok(checks)
        switch = next(c for c in checks if c.name == "Live automation enabled")
        assert switch.ok
        assert "AI_NEWS_AUTOMATION_ENABLED" in switch.detail
        assert "dry-run and test remain available" in switch.detail

    def test_no_gemini_key_is_not_a_failure_either(self, tmp_path: Path) -> None:
        """'ai-news auto' is opt-in, same as Telegram above — a plain laptop doing
        collection and human review only, with no Gemini key at all, is healthy, not
        broken. The actual fail-closed guarantee lives in run_automation() itself,
        which checks this unconditionally in every mode; doctor only reports."""
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        db.migrate(connection)
        connection.close()

        checks = run_health_checks(
            self._settings(tmp_path, automation_enabled=False, gemini_api_key=None)
        )
        assert all_ok(checks)
        gemini = next(c for c in checks if c.name == "Gemini configured")
        assert gemini.ok
        assert "no key set" in gemini.detail

    def test_a_configured_key_reports_the_model(self, tmp_path: Path) -> None:
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        db.migrate(connection)
        connection.close()

        key = "AIzaSy" + "z" * 33
        checks = run_health_checks(
            self._settings(
                tmp_path, automation_enabled=False, gemini_api_key=key,
                llm_model="gemini-test-model",
            )
        )
        gemini = next(c for c in checks if c.name == "Gemini configured")
        assert gemini.ok
        assert "gemini-test-model" in gemini.detail

    def test_live_automation_enabled_is_reported_when_the_switch_is_on(
        self, tmp_path: Path
    ) -> None:
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        db.migrate(connection)
        connection.close()

        key = "AIzaSy" + "z" * 33
        checks = run_health_checks(
            self._settings(tmp_path, automation_enabled=True, gemini_api_key=key)
        )
        switch = next(c for c in checks if c.name == "Live automation enabled")
        assert switch.ok
        assert "may publish" in switch.detail


class TestDeploymentArtefacts:
    """The units and the script encode the safety rules; assert they still say so."""

    def _unit(self, name: str) -> str:
        return (DEPLOY / "systemd" / name).read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "name",
        ["ai-news-bot.service", "ai-news-scheduler.service", "ai-news-collect.service"],
    )
    def test_every_unit_runs_unprivileged_and_confines_writes(self, name: str) -> None:
        unit = self._unit(name)
        assert "User=ainews" in unit
        assert "NoNewPrivileges=true" in unit
        assert "ProtectSystem=strict" in unit
        assert "ReadWritePaths=" in unit
        # The token comes from a file, never from the unit itself.
        assert "EnvironmentFile=" in unit
        assert not re.search(r"AI_NEWS_TELEGRAM_BOT_TOKEN\s*=\s*\S", unit)

    @pytest.mark.parametrize(
        "name", ["ai-news-bot.service", "ai-news-scheduler.service"]
    )
    def test_long_running_units_are_stopped_with_sigterm(self, name: str) -> None:
        unit = self._unit(name)
        assert "KillSignal=SIGTERM" in unit
        assert "Restart=always" in unit
        # A crash loop should stop and stay visible rather than spin forever.
        assert "StartLimitBurst=" in unit

    @pytest.mark.parametrize(
        "name",
        ["ai-news-bot.service", "ai-news-scheduler.service", "ai-news-collect.service"],
    )
    def test_no_unit_migrates_the_schema_on_startup(self, name: str) -> None:
        """Migrations are an explicit deploy step, never a side effect of a restart."""
        commands = [
            line for line in self._unit(name).splitlines() if line.startswith("ExecStart")
        ]
        for command in commands:
            assert "db migrate" not in command, command
            assert "db init" not in command, command

    def test_no_unit_enables_auto_publish(self) -> None:
        for path in (DEPLOY / "systemd").glob("*"):
            assert "AUTO_PUBLISH" not in path.read_text(encoding="utf-8"), path.name

    def test_the_collect_timer_survives_downtime(self) -> None:
        timer = self._unit("ai-news-collect.timer")
        assert "Persistent=true" in timer
        assert "OnCalendar=" in timer

    def test_collect_never_publishes_or_approves(self) -> None:
        """The only unattended pipeline step must stop well short of a channel.

        Asserted against the commands the unit actually executes rather than its text,
        so a comment explaining that collection cannot publish does not fail the test
        that checks collection cannot publish.
        """
        commands = [
            line.split("=", 1)[1].strip()
            for line in self._unit("ai-news-collect.service").splitlines()
            if line.startswith("ExecStart")
        ]
        assert commands, "the unit runs nothing at all"
        assert all(c.endswith(("ai-news collect", "ai-news process")) for c in commands), commands
        for command in commands:
            for forbidden in ("publish", "approve", "review", "queue"):
                assert forbidden not in command, command

    def test_the_update_script_verifies_before_it_starts_anything(self) -> None:
        script = (DEPLOY / "update.sh").read_text(encoding="utf-8")

        # The order is the whole point: migrate and doctor must both come before the
        # services are started again.
        migrate = script.index("db migrate")
        doctor = script.index("doctor")
        start = script.index("systemctl start")
        assert migrate < start, "migrations must run before services start"
        assert doctor < start, "the health check must run before services start"

        # And a failure in either must stop the deployment rather than continue.
        assert "set -Eeuo pipefail" in script
        assert script.count("|| die") >= 2

    def test_the_update_script_stops_services_before_updating_code(self) -> None:
        script = (DEPLOY / "update.sh").read_text(encoding="utf-8")
        assert script.index("systemctl stop") < script.index("git merge")

    def test_the_update_script_backs_the_database_up_before_migrating(self) -> None:
        script = (DEPLOY / "update.sh").read_text(encoding="utf-8")
        assert script.index(".backup") < script.index("db migrate")

    def test_the_deploy_documentation_is_honest_about_what_cannot_be_automated(
        self,
    ) -> None:
        """A reader must not conclude the server writes the posts."""
        readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
        assert "no LLM API in this project" in readme
        assert "you write and approve" in readme.lower()

    def test_no_deployment_file_contains_a_credential(self) -> None:
        for path in DEPLOY.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b", text), path.name
            assert "/Users/" not in text, path.name
