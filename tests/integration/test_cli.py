"""CLI behaviour, driven through Typer's runner against temporary databases."""

from __future__ import annotations

import contextlib
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ai_news_editor.cli.main import app
from ai_news_editor.health import HealthCheck, all_ok, run_health_checks
from ai_news_editor.settings import Settings, get_settings
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.storage import db
from tests.conftest import feed_bytes

runner = CliRunner()

#: The real configuration shipped in the repository.
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"


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
        for command in ("version", "doctor", "db", "collect", "sources"):
            assert command in output_of(result)

    def test_no_arguments_shows_help(self) -> None:
        assert runner.invoke(app, []).exit_code != 0

    @pytest.mark.parametrize("command", ["review", "publish", "evaluate", "draft"])
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


class TestCollectCommand:
    """The collect command, driven through mock transports."""

    def _write_config(self, tmp_path: Path, url: str = "https://alpha.invalid/rss.xml") -> Path:
        path = tmp_path / "sources.yaml"
        path.write_text(
            "version: 1\n"
            "sources:\n"
            "  - id: alpha\n"
            "    name: Alpha Feed\n"
            "    adapter: rss\n"
            f"    url: {url}\n"
            "    trust_tier: OFFICIAL\n"
            "    editorial_role: Test source for the CLI.\n"
            "    priority: PRIMARY_NORMAL\n"
            "    content_types: [NEWS]\n"
            "    publisher_region: UNITED_STATES\n",
            encoding="utf-8",
        )
        return path

    @pytest.fixture
    def wired(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
        """Point the CLI at a temp config and swap its HTTP client for a mock."""
        self._write_config(tmp_path)
        monkeypatch.setenv("AI_NEWS_SOURCES_CONFIG_PATH", str(tmp_path / "sources.yaml"))
        get_settings.cache_clear()

        state: dict[str, object] = {"response": None}

        def fake_client(**kwargs: object) -> HttpClient:
            def handler(request: httpx.Request) -> httpx.Response:
                answer = state["response"]
                if isinstance(answer, Exception):
                    raise answer
                return httpx.Response(
                    answer.status_code,
                    content=answer.content,
                    headers=dict(answer.headers),  # type: ignore[union-attr]
                )

            return HttpClient(retry_backoff_seconds=0.0, transport=httpx.MockTransport(handler))

        monkeypatch.setattr("ai_news_editor.cli.main.HttpClient", fake_client)
        return state

    def _feed(self) -> httpx.Response:
        return httpx.Response(
            200,
            content=feed_bytes("rss_full.xml"),
            headers={"content-type": "application/rss+xml"},
        )

    def test_collects_and_reports(self, wired) -> None:  # type: ignore[no-untyped-def]
        runner.invoke(app, ["db", "init"])
        wired["response"] = self._feed()

        result = runner.invoke(app, ["collect"])
        assert result.exit_code == 0
        output = output_of(result)
        assert "COLLECTION" in output
        assert "alpha" in output

    def test_second_run_reports_nothing_new(self, wired) -> None:  # type: ignore[no-untyped-def]
        runner.invoke(app, ["db", "init"])
        wired["response"] = self._feed()
        runner.invoke(app, ["collect"])

        result = runner.invoke(app, ["collect"])
        assert result.exit_code == 0
        assert "already known" in output_of(result)

    def test_failure_exits_with_code_1(self, wired) -> None:  # type: ignore[no-untyped-def]
        runner.invoke(app, ["db", "init"])
        wired["response"] = httpx.Response(500)

        result = runner.invoke(app, ["collect"])
        assert result.exit_code == 1
        assert "ERROR" in output_of(result)

    def test_dry_run_writes_nothing(self, wired, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        runner.invoke(app, ["db", "init"])
        wired["response"] = self._feed()

        result = runner.invoke(app, ["collect", "--dry-run"])
        assert result.exit_code == 0
        assert "dry run" in output_of(result)

        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            assert connection.execute("SELECT COUNT(*) AS n FROM raw_items").fetchone()["n"] == 0
        finally:
            connection.close()

    def test_unknown_source_exits_with_code_2(self, wired) -> None:  # type: ignore[no-untyped-def]
        runner.invoke(app, ["db", "init"])
        wired["response"] = self._feed()

        result = runner.invoke(app, ["collect", "--source", "ghost"])
        assert result.exit_code == 2
        assert "unknown source id" in output_of(result)

    def test_requires_a_database(self, wired) -> None:  # type: ignore[no-untyped-def]
        wired["response"] = self._feed()
        result = runner.invoke(app, ["collect"])
        assert result.exit_code == 2
        assert "db init" in output_of(result)

    def test_refuses_to_run_against_a_stale_schema(self, wired, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """Collecting into a half-migrated database would fail confusingly later."""
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.close()

        wired["response"] = self._feed()
        result = runner.invoke(app, ["collect"])
        assert result.exit_code == 2
        assert "db migrate" in output_of(result)


class TestSourcesCommand:
    """Runs against the real config/sources.yaml that ships with the project."""

    @pytest.fixture(autouse=True)
    def _shipped_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The CLI fixture chdirs into a temp directory, so the relative default path
        # no longer resolves; point at the repository copy explicitly.
        monkeypatch.setenv("AI_NEWS_SOURCES_CONFIG_PATH", str(REPO_CONFIG))
        get_settings.cache_clear()

    def test_lists_the_configured_sources(self) -> None:
        result = runner.invoke(app, ["sources"])
        assert result.exit_code == 0
        assert "openai_news" in output_of(result)

    def test_every_source_declares_an_editorial_role(self) -> None:
        """Asserted on the configuration, not the rendering: Rich wraps narrow columns."""
        from ai_news_editor.sources.config import load_sources_config

        config = load_sources_config(REPO_CONFIG)
        assert all(len(d.editorial_role.strip()) > 20 for d in config.sources)

    def test_explains_what_trust_tier_means(self) -> None:
        assert "provenance metadata" in output_of(runner.invoke(app, ["sources"]))

    def test_lists_every_configured_source(self) -> None:
        output = output_of(runner.invoke(app, ["sources"]))
        for source_id in (
            "openai_news",
            "google_ai_blog",
            "huggingface_blog",
            "microsoft_365_blog",
            "techcrunch_ai",
            "anthropic_news",
            "notion_releases",
            "hackernews",
        ):
            assert source_id in output


class TestSourcesDoctorCommand:
    """``ai-news sources doctor`` — mocked transports only. Real endpoint verification
    is a separate, explicit smoke test (see docs/sources.md), not part of this suite."""

    def _write_config(self, tmp_path: Path) -> Path:
        path = tmp_path / "sources.yaml"
        path.write_text(
            "version: 1\n"
            "sources:\n"
            "  - id: good\n"
            "    name: Good Source\n"
            "    adapter: rss\n"
            "    url: https://good.invalid/feed.xml\n"
            "    trust_tier: OFFICIAL\n"
            "    editorial_role: A source that resolves and parses.\n"
            "    priority: PRIMARY_NORMAL\n"
            "    content_types: [NEWS]\n"
            "    publisher_region: UNITED_STATES\n"
            "  - id: broken\n"
            "    name: Broken Source\n"
            "    adapter: rss\n"
            "    url: https://broken.invalid/feed.xml\n"
            "    trust_tier: OFFICIAL\n"
            "    editorial_role: A source whose endpoint returns an error.\n"
            "    priority: PRIMARY_NORMAL\n"
            "    content_types: [NEWS]\n"
            "    publisher_region: UNITED_STATES\n"
            "  - id: disabled_source\n"
            "    name: Disabled Source\n"
            "    enabled: false\n"
            "    adapter: rss\n"
            "    url: https://off.invalid/feed.xml\n"
            "    trust_tier: OFFICIAL\n"
            "    editorial_role: A disabled source, never probed by default.\n"
            "    priority: PRIMARY_NORMAL\n"
            "    content_types: [NEWS]\n"
            "    publisher_region: UNITED_STATES\n"
            "    disabled_reason: Disabled for this test.\n",
            encoding="utf-8",
        )
        return path

    @pytest.fixture
    def wired(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
        self._write_config(tmp_path)
        monkeypatch.setenv("AI_NEWS_SOURCES_CONFIG_PATH", str(tmp_path / "sources.yaml"))
        get_settings.cache_clear()

        calls: list[str] = []

        def fake_client(**kwargs: object) -> HttpClient:
            def handler(request: httpx.Request) -> httpx.Response:
                calls.append(request.url.host)
                if request.url.host == "broken.invalid":
                    return httpx.Response(403, content=b"forbidden")
                return httpx.Response(
                    200,
                    content=feed_bytes("rss_full.xml"),
                    headers={"content-type": "application/rss+xml"},
                )

            return HttpClient(retry_backoff_seconds=0.0, transport=httpx.MockTransport(handler))

        monkeypatch.setattr("ai_news_editor.cli.sources.HttpClient", fake_client)
        return calls

    def test_a_healthy_registry_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
    ) -> None:
        path = tmp_path / "sources.yaml"
        path.write_text(
            "version: 1\n"
            "sources:\n"
            "  - id: good\n"
            "    name: Good Source\n"
            "    adapter: rss\n"
            "    url: https://good.invalid/feed.xml\n"
            "    trust_tier: OFFICIAL\n"
            "    editorial_role: A source that resolves and parses.\n"
            "    priority: PRIMARY_NORMAL\n"
            "    content_types: [NEWS]\n"
            "    publisher_region: UNITED_STATES\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AI_NEWS_SOURCES_CONFIG_PATH", str(path))
        get_settings.cache_clear()

        def fake_client(**kwargs: object) -> HttpClient:
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    content=feed_bytes("rss_full.xml"),
                    headers={"content-type": "application/rss+xml"},
                )

            return HttpClient(retry_backoff_seconds=0.0, transport=httpx.MockTransport(handler))

        monkeypatch.setattr("ai_news_editor.cli.sources.HttpClient", fake_client)

        result = runner.invoke(app, ["sources", "doctor"])
        assert result.exit_code == 0, output_of(result)

    def test_reports_discovery_and_parse_for_every_enabled_source(self, wired) -> None:  # type: ignore[no-untyped-def]
        result = runner.invoke(app, ["sources", "doctor"])
        output = output_of(result)
        assert "Good Source" in output
        assert "Broken Source" in output
        assert "OK" in output
        assert "FAIL" in output

    def test_a_failing_enabled_source_exits_nonzero(self, wired) -> None:  # type: ignore[no-untyped-def]
        result = runner.invoke(app, ["sources", "doctor"])
        assert result.exit_code == 1

    def test_disabled_sources_are_not_probed_by_default(self, wired) -> None:  # type: ignore[no-untyped-def]
        calls = wired
        runner.invoke(app, ["sources", "doctor"])
        assert "off.invalid" not in calls

    def test_include_disabled_probes_them_too(self, wired) -> None:  # type: ignore[no-untyped-def]
        calls = wired
        result = runner.invoke(app, ["sources", "doctor", "--include-disabled"])
        assert "off.invalid" in calls
        assert "Disabled Source" in output_of(result)

    def test_nothing_is_written_to_the_database(
        self, wired, tmp_path: Path  # type: ignore[no-untyped-def]
    ) -> None:
        """No `db init` was ever run — if doctor tried to touch a database, this would
        fail with a missing-database error instead of a clean diagnostic report."""
        result = runner.invoke(app, ["sources", "doctor"])
        assert "Good Source" in output_of(result)
        assert not (tmp_path / "ai_news.sqlite3").exists()


class TestProcessAndStatusCommands:
    def _seed(self, tmp_path: Path) -> None:
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            connection.execute(
                "INSERT INTO sources (id, name, kind, url, trust_tier, created_at, updated_at) "
                "VALUES ('alpha', 'Alpha', 'RSS', 'https://alpha.invalid/f', 'OFFICIAL', "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
            )
            for i in range(3):
                connection.execute(
                    "INSERT INTO raw_items (id, source_id, external_id, title_original, "
                    "url_original, summary_raw, payload_raw, content_type, fetched_at) "
                    "VALUES (?, 'alpha', ?, ?, ?, ?, '{}', 'application/rss+xml', "
                    "'2026-08-01T00:00:00+00:00')",
                    (
                        f"00000000-0000-4000-8000-00000000000{i}",
                        f"e{i}",
                        f"Headline number {i} about an AI product update",
                        f"https://alpha.invalid/story-{i}",
                        f"Distinct body number {i} with enough words to fingerprint reliably here",
                    ),
                )
        finally:
            connection.close()

    def test_process_reports_the_funnel(self, tmp_path: Path) -> None:
        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)

        result = runner.invoke(app, ["process"])
        assert result.exit_code == 0
        output = output_of(result)
        assert "PROCESSING" in output
        assert "Ready for evaluation" in output

    def test_process_is_idempotent(self, tmp_path: Path) -> None:
        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)
        runner.invoke(app, ["process"])

        second = runner.invoke(app, ["process"])
        assert second.exit_code == 0

        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            assert connection.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"] == 3
        finally:
            connection.close()

    def test_process_requires_a_database(self) -> None:
        result = runner.invoke(app, ["process"])
        assert result.exit_code == 2
        assert "db init" in output_of(result)

    def test_status_shows_pipeline_counts(self, tmp_path: Path) -> None:
        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)
        runner.invoke(app, ["process"])

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        output = output_of(result)
        assert "Raw items collected" in output
        assert "awaiting AI evaluation" in output

    def test_status_explains_community_signal_semantics(self, tmp_path: Path) -> None:
        runner.invoke(app, ["db", "init"])
        assert "never provenance" in output_of(runner.invoke(app, ["status"]))

    def test_process_limit_option(self, tmp_path: Path) -> None:
        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)
        runner.invoke(app, ["process", "--limit", "1"])

        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            assert connection.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"] == 1
        finally:
            connection.close()


class TestEditorialCommands:
    """The editorial CLI group, end to end against a temporary database."""

    def _seed(self, tmp_path: Path, count: int = 3) -> None:
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            connection.execute(
                "INSERT INTO sources (id, name, kind, url, trust_tier, editorial_role, "
                "created_at, updated_at) VALUES ('alpha', 'Alpha Co', 'RSS', "
                "'https://alpha.invalid/f', 'OFFICIAL', 'Test source.', "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
            )
            for i in range(count):
                raw_id = f"00000000-0000-4000-8000-0000000000{i:02d}"
                art_id = f"00000000-0000-4000-9000-0000000000{i:02d}"
                connection.execute(
                    "INSERT INTO raw_items (id, source_id, external_id, title_original, "
                    "url_original, payload_raw, content_type, fetched_at) "
                    "VALUES (?, 'alpha', ?, ?, ?, '{}', 'application/rss+xml', "
                    "'2026-08-01T00:00:00+00:00')",
                    (raw_id, f"e{i}", f"Story number {i}", f"https://alpha.invalid/{i}"),
                )
                connection.execute(
                    "INSERT INTO articles (id, raw_item_id, source_id, title, canonical_url, "
                    "clean_text, published_at, status, created_at, updated_at) "
                    "VALUES (?, ?, 'alpha', ?, ?, ?, '2026-08-01T00:00:00+00:00', "
                    "'NORMALIZED', '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')",
                    (
                        art_id,
                        raw_id,
                        f"Alpha ships feature number {i} for everyone",
                        f"https://alpha.invalid/{i}",
                        f"Body text number {i} describing what changed for users.",
                    ),
                )
        finally:
            connection.close()

    def _reviewed_from(self, batch_path: Path, **overrides: object) -> dict:
        import json as _json

        batch = _json.loads(batch_path.read_text(encoding="utf-8"))
        reviews = []
        for article in batch["articles"]:
            review = {
                "article_id": article["article_id"],
                "content_fingerprint": article["content_fingerprint"],
                "decision": "SHORTLIST",
                "category": "PRODUCT_UPDATE",
                "audience": "GENERAL",
                "scores": {
                    "credibility": 92,
                    "general_ai_relevance": 88,
                    "reader_interest": 85,
                    "usefulness": 80,
                    "novelty": 70,
                    "wow_factor": 60,
                    "virality_potential": 65,
                    "accessibility": 90,
                    "consumer_impact": 82,
                },
                "verification_status": "NOT_REQUIRED",
                "verification_sources": [],
                "why_selected": ["new user-facing capability"],
                "editorial_angle": "What this changes for an ordinary user.",
            }
            review.update(overrides)
            reviews.append(review)
        return {
            "schema_version": batch["schema_version"],
            "rubric_version": batch["rubric_version"],
            "batch_id": batch["batch_id"],
            "reviewer": "test",
            "reviews": reviews,
        }

    def _latest_batch(self, tmp_path: Path) -> Path:
        return next((tmp_path / "editorial_work").glob("batch-*.json"))

    def test_export_writes_a_batch(self, tmp_path: Path) -> None:
        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)

        result = runner.invoke(app, ["editorial", "export", "--limit", "3"])
        assert result.exit_code == 0
        assert "Editorial batch" in output_of(result)
        assert self._latest_batch(tmp_path).exists()

    def test_export_reports_nothing_to_do_when_all_evaluated(self, tmp_path: Path) -> None:
        import json as _json

        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)
        runner.invoke(app, ["editorial", "export"])
        batch_path = self._latest_batch(tmp_path)

        reviewed_path = tmp_path / "reviewed.json"
        reviewed_path.write_text(_json.dumps(self._reviewed_from(batch_path)), encoding="utf-8")
        runner.invoke(app, ["editorial", "import", str(reviewed_path)])

        result = runner.invoke(app, ["editorial", "export"])
        assert "No candidates need review" in output_of(result)

    def test_validate_accepts_a_good_file(self, tmp_path: Path) -> None:
        import json as _json

        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)
        runner.invoke(app, ["editorial", "export"])

        reviewed_path = tmp_path / "reviewed.json"
        reviewed_path.write_text(
            _json.dumps(self._reviewed_from(self._latest_batch(tmp_path))), encoding="utf-8"
        )
        result = runner.invoke(app, ["editorial", "validate", str(reviewed_path)])
        assert result.exit_code == 0
        assert "Valid" in output_of(result)

    def test_validate_rejects_a_bad_file_without_writing(self, tmp_path: Path) -> None:
        import json as _json

        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)
        runner.invoke(app, ["editorial", "export"])

        payload = self._reviewed_from(self._latest_batch(tmp_path))
        payload["reviews"][0]["scores"]["credibility"] = 5  # SHORTLIST below the gate
        reviewed_path = tmp_path / "bad.json"
        reviewed_path.write_text(_json.dumps(payload), encoding="utf-8")

        result = runner.invoke(app, ["editorial", "validate", str(reviewed_path)])
        assert result.exit_code == 1
        assert "rejected" in output_of(result).lower()

    def test_import_stores_evaluations(self, tmp_path: Path) -> None:
        import json as _json

        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)
        runner.invoke(app, ["editorial", "export"])

        reviewed_path = tmp_path / "reviewed.json"
        reviewed_path.write_text(
            _json.dumps(self._reviewed_from(self._latest_batch(tmp_path))), encoding="utf-8"
        )
        result = runner.invoke(app, ["editorial", "import", str(reviewed_path)])
        assert result.exit_code == 0

        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            assert connection.execute("SELECT COUNT(*) AS n FROM evaluations").fetchone()["n"] == 3
        finally:
            connection.close()

    def test_import_is_idempotent(self, tmp_path: Path) -> None:
        import json as _json

        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)
        runner.invoke(app, ["editorial", "export"])
        reviewed_path = tmp_path / "reviewed.json"
        reviewed_path.write_text(
            _json.dumps(self._reviewed_from(self._latest_batch(tmp_path))), encoding="utf-8"
        )
        runner.invoke(app, ["editorial", "import", str(reviewed_path)])
        second = runner.invoke(app, ["editorial", "import", str(reviewed_path)])
        assert second.exit_code == 0
        assert "Already imported" in output_of(second)

    def test_shortlist_displays_stories(self, tmp_path: Path) -> None:
        import json as _json

        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)
        runner.invoke(app, ["editorial", "export"])
        reviewed_path = tmp_path / "reviewed.json"
        reviewed_path.write_text(
            _json.dumps(self._reviewed_from(self._latest_batch(tmp_path))), encoding="utf-8"
        )
        runner.invoke(app, ["editorial", "import", str(reviewed_path)])

        result = runner.invoke(app, ["editorial", "shortlist"])
        assert result.exit_code == 0
        output = output_of(result)
        assert "EDITORIAL SHORTLIST" in output
        assert "Alpha ships feature" in output

    def test_shortlist_states_nothing_is_published(self, tmp_path: Path) -> None:
        import json as _json

        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)
        runner.invoke(app, ["editorial", "export"])
        reviewed_path = tmp_path / "reviewed.json"
        reviewed_path.write_text(
            _json.dumps(self._reviewed_from(self._latest_batch(tmp_path))), encoding="utf-8"
        )
        runner.invoke(app, ["editorial", "import", str(reviewed_path)])
        assert "approved, drafted or published" in output_of(
            runner.invoke(app, ["editorial", "shortlist"])
        )

    def test_status_reports_editorial_progress(self, tmp_path: Path) -> None:
        runner.invoke(app, ["db", "init"])
        self._seed(tmp_path)
        result = runner.invoke(app, ["editorial", "status"])
        assert result.exit_code == 0
        output = output_of(result)
        assert "awaiting editorial review" in output
        assert "Stale" in output

    def test_no_publish_or_approve_command_exists(self) -> None:
        for command in ("publish", "approve", "send"):
            assert runner.invoke(app, ["editorial", command]).exit_code != 0


class TestDraftCommands:
    """The draft CLI group, end to end against a temporary database."""

    def _seed_shortlist(self, tmp_path: Path, count: int = 2) -> None:
        """A source, articles, and a SHORTLIST evaluation for each."""
        from uuid import uuid4

        from ai_news_editor.editorial.export import build_batch
        from ai_news_editor.editorial.import_results import import_reviewed
        from ai_news_editor.editorial.rubric import RUBRIC_VERSION, SCHEMA_VERSION
        from ai_news_editor.editorial.schema import ReviewedBatch

        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            connection.execute(
                "INSERT INTO sources (id, name, kind, url, trust_tier, created_at, updated_at) "
                "VALUES ('alpha', 'Alpha Co', 'RSS', 'https://alpha.invalid/f', 'OFFICIAL', "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
            )
            for i in range(count):
                raw_id, art_id = str(uuid4()), str(uuid4())
                connection.execute(
                    "INSERT INTO raw_items (id, source_id, external_id, title_original, "
                    "url_original, payload_raw, content_type, fetched_at) "
                    "VALUES (?, 'alpha', ?, ?, ?, '{}', 'application/rss+xml', "
                    "'2026-08-01T00:00:00+00:00')",
                    (raw_id, f"e{i}", f"Story {i}", f"https://alpha.invalid/{i}"),
                )
                connection.execute(
                    "INSERT INTO articles (id, raw_item_id, source_id, title, canonical_url, "
                    "clean_text, published_at, status, created_at, updated_at) "
                    "VALUES (?, ?, 'alpha', ?, ?, ?, '2026-08-01T00:00:00+00:00', 'NORMALIZED', "
                    "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')",
                    (
                        art_id,
                        raw_id,
                        f"Alpha ships feature number {i}",
                        f"https://alpha.invalid/{i}",
                        f"Body number {i} describing what changed for users of the app.",
                    ),
                )

            batch = build_batch(connection, limit=10)
            reviewed = ReviewedBatch.model_validate(
                {
                    "schema_version": SCHEMA_VERSION,
                    "rubric_version": RUBRIC_VERSION,
                    "batch_id": batch.batch_id,
                    "reviewer": "test",
                    "reviews": [
                        {
                            "article_id": str(a.article_id),
                            "content_fingerprint": a.content_fingerprint,
                            "decision": "SHORTLIST",
                            "category": "PRODUCT_UPDATE",
                            "audience": "GENERAL",
                            "scores": {
                                "credibility": 92,
                                "general_ai_relevance": 88,
                                "reader_interest": 85,
                                "usefulness": 80,
                                "novelty": 70,
                                "wow_factor": 60,
                                "virality_potential": 65,
                                "accessibility": 90,
                                "consumer_impact": 82,
                            },
                            "verification_status": "NOT_REQUIRED",
                            "why_selected": ["new user-facing capability"],
                            "editorial_angle": "What changes for an ordinary user.",
                        }
                        for a in batch.articles
                    ],
                }
            )
            import_reviewed(connection, reviewed)
        finally:
            connection.close()

    def _drafts_from(self, assignment_path: Path) -> dict:
        import json as _json

        batch = _json.loads(assignment_path.read_text(encoding="utf-8"))
        return {
            "schema_version": batch["schema_version"],
            "style_version": batch["style_version"],
            "batch_id": batch["batch_id"],
            "writer": "test",
            "drafts": [
                {
                    "article_id": a["article_id"],
                    "evaluation_id": a["evaluation"]["evaluation_id"],
                    "article_fingerprint": a["article_fingerprint"],
                    "post_format": "STANDARD",
                    "headline": "🆕 Застосунок отримав нову функцію",
                    "body": (
                        "Компанія оновила застосунок: тепер він уміє більше, ніж раніше. "
                        "Це помітно тим, хто користується ним щодня — зникає зайвий крок. "
                        "Функція вже доступна, компанія заявляє про поступовий запуск. "
                        "Варто памʼятати: доступність у регіонах може відрізнятися."
                    ),
                    "source_label": "Alpha Co",
                    "source_url": a["source"]["url"],
                    "writer_notes": ["доступність не вказана"],
                }
                for a in batch["assignments"]
            ],
        }

    def _latest_assignment(self, tmp_path: Path) -> Path:
        return next((tmp_path / "writing_work").glob("write-*.json"))

    def test_export_writes_assignments(self, tmp_path: Path) -> None:
        runner.invoke(app, ["db", "init"])
        self._seed_shortlist(tmp_path)

        result = runner.invoke(app, ["draft", "export", "--limit", "2"])
        assert result.exit_code == 0
        assert "Writing batch" in output_of(result)
        assert self._latest_assignment(tmp_path).exists()

    def test_export_reports_nothing_when_no_shortlist(self) -> None:
        runner.invoke(app, ["db", "init"])
        result = runner.invoke(app, ["draft", "export"])
        assert "Nothing eligible to write" in output_of(result)

    def test_validate_accepts_good_drafts(self, tmp_path: Path) -> None:
        import json as _json

        runner.invoke(app, ["db", "init"])
        self._seed_shortlist(tmp_path)
        runner.invoke(app, ["draft", "export"])

        path = tmp_path / "drafts.json"
        path.write_text(
            _json.dumps(self._drafts_from(self._latest_assignment(tmp_path)), ensure_ascii=False),
            encoding="utf-8",
        )
        result = runner.invoke(app, ["draft", "validate", str(path)])
        assert result.exit_code == 0
        assert "Valid" in output_of(result)

    def test_validate_rejects_bad_drafts(self, tmp_path: Path) -> None:
        import json as _json

        runner.invoke(app, ["db", "init"])
        self._seed_shortlist(tmp_path)
        runner.invoke(app, ["draft", "export"])

        payload = self._drafts_from(self._latest_assignment(tmp_path))
        payload["drafts"][0]["source_url"] = "javascript:alert(1)"
        path = tmp_path / "bad.json"
        path.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        result = runner.invoke(app, ["draft", "validate", str(path)])
        assert result.exit_code == 1
        assert "rejected" in output_of(result).lower()

    def test_import_creates_pending_review_drafts(self, tmp_path: Path) -> None:
        import json as _json

        runner.invoke(app, ["db", "init"])
        self._seed_shortlist(tmp_path)
        runner.invoke(app, ["draft", "export"])

        path = tmp_path / "drafts.json"
        path.write_text(
            _json.dumps(self._drafts_from(self._latest_assignment(tmp_path)), ensure_ascii=False),
            encoding="utf-8",
        )
        result = runner.invoke(app, ["draft", "import", str(path)])
        assert result.exit_code == 0
        assert "PENDING_REVIEW" in output_of(result)

        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            statuses = {
                row["status"] for row in connection.execute("SELECT status FROM drafts").fetchall()
            }
            assert statuses == {"PENDING_REVIEW"}
        finally:
            connection.close()

    def test_import_is_idempotent(self, tmp_path: Path) -> None:
        import json as _json

        runner.invoke(app, ["db", "init"])
        self._seed_shortlist(tmp_path)
        runner.invoke(app, ["draft", "export"])
        path = tmp_path / "drafts.json"
        path.write_text(
            _json.dumps(self._drafts_from(self._latest_assignment(tmp_path)), ensure_ascii=False),
            encoding="utf-8",
        )
        runner.invoke(app, ["draft", "import", str(path)])
        runner.invoke(app, ["draft", "import", str(path)])

        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            assert connection.execute("SELECT COUNT(*) AS n FROM drafts").fetchone()["n"] == 2
        finally:
            connection.close()

    def test_list_and_show(self, tmp_path: Path) -> None:
        import json as _json

        runner.invoke(app, ["db", "init"])
        self._seed_shortlist(tmp_path)
        runner.invoke(app, ["draft", "export"])
        path = tmp_path / "drafts.json"
        path.write_text(
            _json.dumps(self._drafts_from(self._latest_assignment(tmp_path)), ensure_ascii=False),
            encoding="utf-8",
        )
        runner.invoke(app, ["draft", "import", str(path)])

        listing = runner.invoke(app, ["draft", "list"])
        assert listing.exit_code == 0
        assert "PENDING_REVIEW" in output_of(listing)

        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            draft_id = connection.execute("SELECT id FROM drafts LIMIT 1").fetchone()["id"]
        finally:
            connection.close()

        shown = runner.invoke(app, ["draft", "show", draft_id[:8]])
        assert shown.exit_code == 0
        output = output_of(shown)
        assert "Застосунок" in output
        assert "not approved and not published" in output

    def test_no_approve_or_publish_command_exists(self) -> None:
        for command in ("approve", "publish", "reject", "send"):
            assert runner.invoke(app, ["draft", command]).exit_code != 0
