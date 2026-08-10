"""Migration runner: ordering, idempotency, checksums, and pragmas."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ai_news_editor.domain.errors import MigrationError
from ai_news_editor.storage import db

EXPECTED_TABLES = {
    "sources",
    "raw_items",
    "articles",
    "drafts",
    "draft_versions",
    "review_decisions",
    "schema_migrations",
}


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


class TestFreshDatabase:
    def test_creates_every_expected_table(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "fresh.sqlite3")
        db.migrate(conn)
        assert _tables(conn) >= EXPECTED_TABLES

    def test_creates_the_database_file_and_parents(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deeper" / "news.sqlite3"
        conn = db.connect(path)
        db.migrate(conn)
        conn.close()
        assert path.exists()

    def test_empty_database_reports_version_zero(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "empty.sqlite3")
        assert db.schema_version(conn) == 0

    def test_schema_version_after_migration(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "v.sqlite3")
        db.migrate(conn)
        assert db.schema_version(conn) == len(db.discover_migrations())


class TestIdempotency:
    def test_second_run_applies_nothing(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "twice.sqlite3")
        first = db.migrate(conn)
        second = db.migrate(conn)
        assert first
        assert second == []

    def test_repeated_runs_do_not_duplicate_records(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "many.sqlite3")
        for _ in range(4):
            db.migrate(conn)
        assert len(db.applied_migrations(conn)) == len(db.discover_migrations())

    def test_no_pending_after_migrating(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "p.sqlite3")
        db.migrate(conn)
        assert db.pending_migrations(conn) == []

    def test_data_survives_a_repeat_run(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "keep.sqlite3")
        db.migrate(conn)
        conn.execute(
            "INSERT INTO sources (id, name, kind, url, trust_tier, created_at, updated_at) "
            "VALUES ('s', 'S', 'RSS', 'https://example.invalid', 'OFFICIAL', 'now', 'now')"
        )
        db.migrate(conn)
        assert conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 1


class TestOrdering:
    def test_discovery_is_sorted_by_version(self, tmp_path: Path) -> None:
        for name in ("003_third.sql", "001_first.sql", "002_second.sql"):
            (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")
        versions = [m.version for m in db.discover_migrations(tmp_path)]
        assert versions == [1, 2, 3]

    def test_malformed_filename_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "initial.sql").write_text("SELECT 1;", encoding="utf-8")
        with pytest.raises(MigrationError, match="must look like"):
            db.discover_migrations(tmp_path)

    def test_duplicate_version_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
        (tmp_path / "001_b.sql").write_text("SELECT 1;", encoding="utf-8")
        with pytest.raises(MigrationError, match="duplicate migration version"):
            db.discover_migrations(tmp_path)


class TestChecksums:
    def test_editing_an_applied_migration_is_rejected(self, tmp_path: Path) -> None:
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        path = migrations / "001_initial.sql"
        path.write_text("CREATE TABLE a (id TEXT);", encoding="utf-8")

        conn = db.connect(tmp_path / "c.sqlite3")
        db.migrate(conn, migrations)

        path.write_text("CREATE TABLE a (id TEXT, extra TEXT);", encoding="utf-8")
        with pytest.raises(MigrationError, match="changed after it was applied"):
            db.migrate(conn, migrations)

    def test_a_new_migration_is_applied_on_top(self, tmp_path: Path) -> None:
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        (migrations / "001_initial.sql").write_text("CREATE TABLE a (id TEXT);", encoding="utf-8")
        conn = db.connect(tmp_path / "d.sqlite3")
        db.migrate(conn, migrations)

        (migrations / "002_more.sql").write_text("CREATE TABLE b (id TEXT);", encoding="utf-8")
        applied = db.migrate(conn, migrations)
        assert [m.version for m in applied] == [2]
        assert "b" in _tables(conn)


class TestFailureHandling:
    def test_a_failing_migration_leaves_no_partial_schema(self, tmp_path: Path) -> None:
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        (migrations / "001_broken.sql").write_text(
            "CREATE TABLE good (id TEXT);\nCREATE TABLE bad (;\n", encoding="utf-8"
        )
        conn = db.connect(tmp_path / "e.sqlite3")
        with pytest.raises(MigrationError, match="failed"):
            db.migrate(conn, migrations)

        assert "good" not in _tables(conn)
        assert db.schema_version(conn) == 0


class TestStatementSplitting:
    def test_trigger_bodies_are_not_split_on_inner_semicolons(self) -> None:
        sql = (
            "CREATE TABLE t (id TEXT);\n"
            "CREATE TRIGGER trg BEFORE UPDATE ON t\n"
            "BEGIN SELECT RAISE(ABORT, 'no'); END;\n"
        )
        statements = db.split_statements(sql)
        assert len(statements) == 2
        assert "RAISE(ABORT" in statements[1]

    def test_trailing_comments_are_tolerated(self) -> None:
        assert len(db.split_statements("SELECT 1;\n-- done\n")) == 1

    def test_incomplete_trailing_statement_is_rejected(self) -> None:
        with pytest.raises(MigrationError, match="incomplete statement"):
            db.split_statements("SELECT 1;\nCREATE TABLE oops (\n")

    def test_real_initial_migration_splits_cleanly(self) -> None:
        migration = db.discover_migrations()[0]
        assert len(db.split_statements(migration.sql)) > 10


class TestPragmas:
    def test_foreign_keys_are_enabled(self, connection: sqlite3.Connection) -> None:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_journal_mode_is_wal(self, connection: sqlite3.Connection) -> None:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    def test_rows_are_addressable_by_column_name(self, connection: sqlite3.Connection) -> None:
        row = connection.execute("SELECT 1 AS answer").fetchone()
        assert row["answer"] == 1


class TestMigration002:
    """Phase-2 additions apply cleanly on a fresh database and on an existing one."""

    def test_fresh_install_reaches_the_latest_version(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "fresh2.sqlite3")
        applied = db.migrate(conn)
        assert [m.version for m in applied] == [1, 2]
        assert "source_fetch_state" in _tables(conn)

    def test_upgrade_from_a_phase_1_database(self, tmp_path: Path) -> None:
        """A database that only ever saw 001 must upgrade without losing data."""
        path = tmp_path / "upgrade.sqlite3"
        migrations = db.MIGRATIONS_DIR
        only_001 = tmp_path / "only_001"
        only_001.mkdir()
        (only_001 / "001_initial.sql").write_text(
            (migrations / "001_initial.sql").read_text(encoding="utf-8"), encoding="utf-8"
        )

        conn = db.connect(path)
        db.migrate(conn, only_001)
        assert db.schema_version(conn) == 1
        conn.execute(
            "INSERT INTO sources (id, name, kind, url, trust_tier, created_at, updated_at) "
            "VALUES ('legacy', 'Legacy', 'RSS', 'https://legacy.invalid', 'OFFICIAL', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )

        applied = db.migrate(conn)
        assert [m.version for m in applied] == [2]
        assert db.schema_version(conn) == 2
        assert conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 1

    def test_new_source_columns_have_workable_defaults(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "defaults.sqlite3")
        db.migrate(conn)
        conn.execute(
            "INSERT INTO sources (id, name, kind, url, trust_tier, created_at, updated_at) "
            "VALUES ('s', 'S', 'RSS', 'https://s.invalid', 'OFFICIAL', 'now', 'now')"
        )
        row = conn.execute("SELECT editorial_role, tags_json FROM sources").fetchone()
        assert row["editorial_role"] is None
        assert row["tags_json"] == "[]"

    def test_fetch_state_requires_a_real_source(self, connection: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO source_fetch_state (source_id, updated_at) VALUES ('ghost', 'now')"
            )

    def test_fetch_state_rejects_an_unknown_outcome(
        self, connection: sqlite3.Connection
    ) -> None:
        connection.execute(
            "INSERT INTO sources (id, name, kind, url, trust_tier, created_at, updated_at) "
            "VALUES ('s', 'S', 'RSS', 'https://s.invalid', 'OFFICIAL', 'now', 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO source_fetch_state (source_id, last_outcome, updated_at) "
                "VALUES ('s', 'MAYBE', 'now')"
            )

    def test_migration_001_was_not_altered(self) -> None:
        """History must stay immutable; 002 is additive only."""
        text = (db.MIGRATIONS_DIR / "001_initial.sql").read_text(encoding="utf-8")
        assert "source_fetch_state" not in text
        assert "editorial_role" not in text
