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

    def test_fresh_install_includes_the_phase_2_tables(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "fresh2.sqlite3")
        applied = db.migrate(conn)
        assert [m.version for m in applied][:2] == [1, 2]
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
        assert applied[0].version == 2
        assert db.schema_version(conn) == len(db.discover_migrations())
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

    def test_fetch_state_rejects_an_unknown_outcome(self, connection: sqlite3.Connection) -> None:
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


class TestMigration003:
    """Phase-3 additions apply cleanly on a fresh database and on a Phase-2 one."""

    def test_fresh_install_includes_the_phase_3_tables(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "fresh3.sqlite3")
        applied = db.migrate(conn)
        assert [m.version for m in applied][:3] == [1, 2, 3]
        assert "community_signals" in _tables(conn)

    def test_upgrade_from_a_phase_2_database(self, tmp_path: Path) -> None:
        """A database that stopped at 002 must upgrade without losing data."""
        staged = tmp_path / "upto_002"
        staged.mkdir()
        for name in ("001_initial.sql", "002_source_fetch_state.sql"):
            (staged / name).write_text(
                (db.MIGRATIONS_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
            )

        conn = db.connect(tmp_path / "upgrade3.sqlite3")
        db.migrate(conn, staged)
        assert db.schema_version(conn) == 2
        conn.execute(
            "INSERT INTO sources (id, name, kind, url, trust_tier, created_at, updated_at) "
            "VALUES ('legacy', 'Legacy', 'RSS', 'https://legacy.invalid', 'OFFICIAL', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )

        applied = db.migrate(conn)
        assert applied[0].version == 3
        assert db.schema_version(conn) == len(db.discover_migrations())
        assert conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 1

    def test_new_article_columns_exist(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(articles)").fetchall()
        }
        assert {
            "title_fingerprint",
            "simhash",
            "duplicate_reason",
            "possible_duplicate_of_id",
            "normalized_at",
        } <= columns

    def test_community_signals_require_a_real_source(self, connection: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO community_signals (id, source_id, external_id, observed_at) "
                "VALUES ('x', 'ghost', 'e1', 'now')"
            )

    def test_a_discussion_is_recorded_once_per_source(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO sources (id, name, kind, url, trust_tier, signal_only, "
            "created_at, updated_at) VALUES ('hn', 'HN', 'HN_SIGNAL', "
            "'https://hn.invalid', 'COMMUNITY_SIGNAL', 1, 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO community_signals (id, source_id, external_id, observed_at) "
            "VALUES ('a', 'hn', 'story-1', 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO community_signals (id, source_id, external_id, observed_at) "
                "VALUES ('b', 'hn', 'story-1', 'now')"
            )

    def test_earlier_migrations_were_not_altered(self) -> None:
        """History stays immutable; 003 is additive only."""
        first = (db.MIGRATIONS_DIR / "001_initial.sql").read_text(encoding="utf-8")
        second = (db.MIGRATIONS_DIR / "002_source_fetch_state.sql").read_text(encoding="utf-8")
        assert "community_signals" not in first
        assert "community_signals" not in second
        assert "simhash" not in first
        assert "simhash" not in second


class TestMigration004:
    """Phase-4 evaluations apply cleanly on a fresh database and on a Phase-3 one."""

    def test_fresh_install_includes_the_evaluations_table(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "fresh4.sqlite3")
        applied = db.migrate(conn)
        assert [m.version for m in applied][:4] == [1, 2, 3, 4]
        assert "evaluations" in _tables(conn)

    def test_upgrade_from_a_phase_3_database(self, tmp_path: Path) -> None:
        staged = tmp_path / "upto_003"
        staged.mkdir()
        for name in (
            "001_initial.sql",
            "002_source_fetch_state.sql",
            "003_normalization_and_signals.sql",
        ):
            (staged / name).write_text(
                (db.MIGRATIONS_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
            )

        conn = db.connect(tmp_path / "upgrade4.sqlite3")
        db.migrate(conn, staged)
        assert db.schema_version(conn) == 3
        conn.execute(
            "INSERT INTO sources (id, name, kind, url, trust_tier, created_at, updated_at) "
            "VALUES ('legacy', 'Legacy', 'RSS', 'https://legacy.invalid', 'OFFICIAL', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )

        applied = db.migrate(conn)
        assert applied[0].version == 4
        assert db.schema_version(conn) == len(db.discover_migrations())
        assert conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 1

    def test_evaluations_require_a_real_article(self, connection: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO evaluations (id, article_id, schema_version, rubric_version, "
                "evaluator_type, content_fingerprint, decision, category, audience, "
                "credibility, general_ai_relevance, reader_interest, usefulness, novelty, "
                "wow_factor, virality_potential, accessibility, consumer_impact, "
                "composite_score, verification_status, created_at) "
                "VALUES ('e1', 'ghost', '1', '1', 'CLAUDE_CODE', 'fp', 'SHORTLIST', "
                "'PRODUCT_UPDATE', 'GENERAL', 90, 90, 90, 90, 90, 90, 90, 90, 90, 90.0, "
                "'NOT_REQUIRED', 'now')"
            )

    @pytest.mark.parametrize("column", ["credibility", "reader_interest", "consumer_impact"])
    def test_scores_outside_zero_to_hundred_are_refused(
        self, connection: sqlite3.Connection, seeded_article, column: str
    ) -> None:
        values = dict.fromkeys(
            [
                "credibility",
                "general_ai_relevance",
                "reader_interest",
                "usefulness",
                "novelty",
                "wow_factor",
                "virality_potential",
                "accessibility",
                "consumer_impact",
            ],
            90,
        )
        values[column] = 150
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO evaluations (id, article_id, schema_version, rubric_version, "  # noqa: S608
                f"evaluator_type, content_fingerprint, decision, category, audience, {columns}, "
                "composite_score, verification_status, created_at) "
                f"VALUES ('e1', ?, '1', '1', 'CLAUDE_CODE', 'fp', 'SHORTLIST', 'PRODUCT_UPDATE', "
                f"'GENERAL', {placeholders}, 90.0, 'NOT_REQUIRED', 'now')",
                (str(seeded_article.id), *values.values()),
            )

    def test_unknown_decision_is_refused(
        self, connection: sqlite3.Connection, seeded_article
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO evaluations (id, article_id, schema_version, rubric_version, "
                "evaluator_type, content_fingerprint, decision, category, audience, "
                "credibility, general_ai_relevance, reader_interest, usefulness, novelty, "
                "wow_factor, virality_potential, accessibility, consumer_impact, "
                "composite_score, verification_status, created_at) "
                "VALUES ('e1', ?, '1', '1', 'CLAUDE_CODE', 'fp', 'PUBLISH_NOW', "
                "'PRODUCT_UPDATE', 'GENERAL', 90, 90, 90, 90, 90, 90, 90, 90, 90, 90.0, "
                "'NOT_REQUIRED', 'now')",
                (str(seeded_article.id),),
            )

    def test_earlier_migrations_were_not_altered(self) -> None:
        """History stays immutable; 004 is additive only.

        Checks for the DDL rather than the bare word: migration 001 legitimately
        mentions evaluations in a comment explaining which tables it defers.
        """
        for name in (
            "001_initial.sql",
            "002_source_fetch_state.sql",
            "003_normalization_and_signals.sql",
        ):
            text = (db.MIGRATIONS_DIR / name).read_text(encoding="utf-8").lower()
            assert "create table evaluations" not in text
            assert "alter table evaluations" not in text


class TestMigration005:
    """Phase-5 writing metadata applies cleanly and leaves Phase-1 guarantees intact."""

    def test_fresh_install_reaches_the_latest_version(self, tmp_path: Path) -> None:
        conn = db.connect(tmp_path / "fresh5.sqlite3")
        applied = db.migrate(conn)
        assert [m.version for m in applied] == [1, 2, 3, 4, 5, 6, 7, 8]

    def test_upgrade_from_a_phase_4_database(self, tmp_path: Path) -> None:
        staged = tmp_path / "upto_004"
        staged.mkdir()
        for name in sorted(p.name for p in db.MIGRATIONS_DIR.glob("00[1-4]_*.sql")):
            (staged / name).write_text(
                (db.MIGRATIONS_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
            )

        conn = db.connect(tmp_path / "upgrade5.sqlite3")
        db.migrate(conn, staged)
        assert db.schema_version(conn) == 4

        applied = db.migrate(conn)
        assert [m.version for m in applied] == [5, 6, 7, 8]

    def test_new_draft_columns_exist(self, connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(draft_versions)")}
        assert {"post_format", "source_url", "writer_notes_json", "style_version"} <= columns
        draft_columns = {row["name"] for row in connection.execute("PRAGMA table_info(drafts)")}
        assert "evaluation_id" in draft_columns

    def test_unknown_post_format_is_refused(
        self, connection: sqlite3.Connection, seeded_article
    ) -> None:
        connection.execute(
            "INSERT INTO drafts (id, article_id, status, created_at, updated_at) "
            "VALUES ('d1', ?, 'DRAFTED', 'now', 'now')",
            (str(seeded_article.id),),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO draft_versions (id, draft_id, version_no, title, body, category, "
                "audience, source_attribution, post_format, content_hash, created_by, created_at) "
                "VALUES ('v1', 'd1', 1, 't', 'b', 'WOW', 'GENERAL', 'a', 'EPIC_SAGA', 'h', "
                "'test', 'now')"
            )

    def test_phase_one_immutability_triggers_survive(
        self, connection: sqlite3.Connection, seeded_article
    ) -> None:
        """Migration 005 must not have weakened the append-only guarantees."""
        triggers = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        assert "trg_draft_versions_no_update" in triggers
        assert "trg_draft_versions_no_delete" in triggers

    def test_earlier_migrations_were_not_altered(self) -> None:
        for path in sorted(db.MIGRATIONS_DIR.glob("00[1-4]_*.sql")):
            text = path.read_text(encoding="utf-8").lower()
            assert "post_format" not in text
            assert "writer_notes_json" not in text


class TestMigration006:
    """Phase-7 publications: additive, append-only, and idempotent by construction."""

    def test_upgrade_from_a_phase_5_database(self, tmp_path: Path) -> None:
        staged = tmp_path / "upto_005"
        staged.mkdir()
        for name in sorted(p.name for p in db.MIGRATIONS_DIR.glob("00[1-5]_*.sql")):
            (staged / name).write_text(
                (db.MIGRATIONS_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
            )

        conn = db.connect(tmp_path / "upgrade6.sqlite3")
        db.migrate(conn, staged)
        assert db.schema_version(conn) == 5

        applied = db.migrate(conn)
        assert [m.version for m in applied] == [6, 7, 8]

    def test_history_is_untouched(self) -> None:
        """001-005 are never edited; 006 adds a table and nothing else."""
        text = (db.MIGRATIONS_DIR / "006_publications.sql").read_text(encoding="utf-8").lower()
        assert "create table publications" in text
        for forbidden in ("drop table", "alter table drafts", "alter table draft_versions",
                          "alter table review_decisions", "drop trigger", "drop index"):
            assert forbidden not in text

    def test_the_publications_table_exists(self, connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(publications)")}
        assert {
            "id", "draft_id", "draft_version_id", "review_decision_id", "content_hash",
            "channel", "status", "message_id", "chat_id", "attempt_no", "failure_reason",
            "published_at", "created_at",
        } <= columns

    def test_an_unknown_status_is_refused(self, connection: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO publications (id, draft_id, draft_version_id, "
                "review_decision_id, content_hash, channel, status, created_at) "
                "VALUES ('p', 'd', 'v', 'r', 'h', '@c', 'MAYBE', 'now')"
            )

    def test_a_success_without_a_message_id_is_refused(
        self, connection: sqlite3.Connection
    ) -> None:
        """The database will not record a success it has no evidence for."""
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO publications (id, draft_id, draft_version_id, "
                "review_decision_id, content_hash, channel, status, created_at) "
                "VALUES ('p', 'd', 'v', 'r', 'h', '@c', 'SUCCEEDED', 'now')"
            )

    def test_append_only_triggers_are_installed(self, connection: sqlite3.Connection) -> None:
        """Enforcement with real rows lives in the repository tests; this is the schema."""
        triggers = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'publications'"
            )
        }
        assert triggers == {"trg_publications_no_update", "trg_publications_no_delete"}

    def test_one_success_per_version_and_destination_is_unique(
        self, connection: sqlite3.Connection
    ) -> None:
        index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_publications_one_success_per_destination'"
        ).fetchone()["sql"]
        assert "UNIQUE" in index
        assert "draft_version_id" in index and "channel" in index
        assert "SUCCEEDED" in index
