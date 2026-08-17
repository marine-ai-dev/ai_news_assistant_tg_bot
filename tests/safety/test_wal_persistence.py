"""Committed state must survive without its WAL sidecar. Never skip these.

The GitHub Actions automation workflow commits exactly one file back to git:
``automation_state/ai_news.sqlite3`` — never its ``-wal``/``-shm`` sidecars, which
``.gitignore`` excludes everywhere on purpose (see the workflow's own comments on why
committing a WAL sidecar is not an acceptable permanent design). That means the main
file alone has to be a complete, self-consistent snapshot at the moment it is copied for
git — not "complete once SQLite gets around to it."

These tests do not assume that is true. They prove it, against this project's own
``storage.db`` connection helper (the real pragmas: WAL journal mode, foreign keys,
autocommit) and a real repository write, by copying *only* the main file — while the
original connection is still open, simulating the worst case of an abrupt process exit
right after the checkpoint — and reading it back from nowhere else.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest

from ai_news_editor.domain.errors import CheckpointError
from ai_news_editor.storage import db
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    RawItemRepository,
    SourceRepository,
)
from tests.conftest import make_article, make_raw_item, make_source

pytestmark = pytest.mark.safety


def _write_automation_state(connection: sqlite3.Connection) -> UUID:
    """One representative write: a source, a raw item, an article — exactly the shape
    a live 'ai-news collect' pass leaves behind for the automation pipeline to read."""
    sources = SourceRepository(connection)
    raw_items = RawItemRepository(connection)
    articles = ArticleRepository(connection)

    source = sources.upsert(make_source())
    item = raw_items.add(make_raw_item(source.id))
    article = articles.add(make_article(item.id, source.id))
    return article.id


class TestWalCheckpointGuaranteesPersistence:
    def test_journal_mode_is_really_wal(self, tmp_path: Path) -> None:
        """Confirms this test is actually exercising the failure mode it claims to —
        a non-WAL database would trivially pass everything below for the wrong reason."""
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        connection.close()
        assert mode.lower() == "wal"

    def test_uncheckpointed_writes_exist_only_in_the_wal_sidecar(
        self, tmp_path: Path
    ) -> None:
        """Documents the actual risk, not just the fix: without an explicit checkpoint,
        a fresh connection to a copy of only the main file can genuinely miss committed
        rows — this is *why* db.checkpoint() exists, not an assumption."""
        db_path = tmp_path / "ai_news.sqlite3"
        connection = db.connect(db_path)
        db.migrate(connection)
        _write_automation_state(connection)

        wal_path = db_path.with_name(db_path.name + "-wal")
        assert wal_path.exists() and wal_path.stat().st_size > 0, (
            "the write above should still be sitting in the WAL, uncheckpointed — "
            "if this fails, SQLite itself already folded it, and the rest of this "
            "test class is not proving what it claims to"
        )

        copy_path = tmp_path / "copy_without_checkpoint.sqlite3"
        shutil.copy(db_path, copy_path)  # main file only — no -wal, no -shm
        connection.close()

        # The migrations themselves ran inside committed-but-uncheckpointed
        # transactions too, so an uncheckpointed copy can be missing the *schema*, not
        # just the row — an even sharper demonstration of the risk than a row count
        # would be. Either shape (table absent, or present but empty) proves the point;
        # what would disprove it is the row actually being there.
        reopened = sqlite3.connect(copy_path)
        reopened.row_factory = sqlite3.Row
        table = reopened.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='articles'"
        ).fetchone()
        if table is None:
            reopened.close()
            return
        rows = reopened.execute("SELECT COUNT(*) AS n FROM articles").fetchone()
        reopened.close()
        assert rows["n"] == 0, (
            "expected the uncheckpointed copy to be missing the write — if it is "
            "present, this SQLite build checkpoints on every commit regardless, which "
            "would make the explicit checkpoint a no-op rather than a real fix"
        )

    def test_checkpoint_makes_the_main_file_alone_a_complete_snapshot(
        self, tmp_path: Path
    ) -> None:
        """The actual guarantee this application depends on: call db.checkpoint(),
        then copy *only* the main file — even with the original connection still open,
        the worst case for an abruptly killed CI job — and every committed row is
        there."""
        db_path = tmp_path / "ai_news.sqlite3"
        connection = db.connect(db_path)
        db.migrate(connection)
        article_id = _write_automation_state(connection)

        db.checkpoint(connection)

        wal_path = db_path.with_name(db_path.name + "-wal")
        assert wal_path.exists()
        assert wal_path.stat().st_size == 0, "TRUNCATE checkpoint should empty the WAL"

        copy_path = tmp_path / "copy_after_checkpoint.sqlite3"
        shutil.copy(db_path, copy_path)  # main file only, connection still open

        reopened = sqlite3.connect(copy_path)
        reopened.row_factory = sqlite3.Row
        row = reopened.execute(
            "SELECT id FROM articles WHERE id = ?", (str(article_id),)
        ).fetchone()
        reopened.close()
        connection.close()

        assert row is not None
        assert row["id"] == str(article_id)

    def test_checkpoint_survives_a_normal_close_too(self, tmp_path: Path) -> None:
        """The steady-state path every CLI command actually takes: checkpoint, then
        close, then (in the workflow) git add -f only the main file."""
        db_path = tmp_path / "ai_news.sqlite3"
        connection = db.connect(db_path)
        db.migrate(connection)
        article_id = _write_automation_state(connection)
        db.checkpoint(connection)
        connection.close()

        for sidecar in ("-wal", "-shm"):
            path = db_path.with_name(db_path.name + sidecar)
            assert not path.exists() or path.stat().st_size == 0

        copy_path = tmp_path / "copy_after_close.sqlite3"
        shutil.copy(db_path, copy_path)
        reopened = sqlite3.connect(copy_path)
        row = reopened.execute(
            "SELECT id FROM articles WHERE id = ?", (str(article_id),)
        ).fetchone()
        reopened.close()
        assert row is not None

    def test_checkpoint_on_an_idle_connection_is_a_harmless_no_op(
        self, tmp_path: Path
    ) -> None:
        """dry-run writes nothing, but cli/auto.py calls checkpoint() unconditionally
        on every mode — this must never fail just because there was nothing to do."""
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        db.migrate(connection)
        db.checkpoint(connection)  # no write happened; must not raise
        db.checkpoint(connection)  # calling it twice must not raise either
        connection.close()

    def test_a_busy_checkpoint_raises_rather_than_silently_losing_data(
        self, tmp_path: Path
    ) -> None:
        """If a second connection is reading the WAL, SQLite cannot fully fold it —
        this must be loud (CheckpointError), never a silent partial checkpoint that
        then gets copied to git as if it were complete."""
        db_path = tmp_path / "ai_news.sqlite3"
        connection = db.connect(db_path)
        db.migrate(connection)
        _write_automation_state(connection)

        # A second connection with an open read transaction holds the WAL open under
        # it, which is exactly the condition PRAGMA wal_checkpoint reports as "busy".
        reader = db.connect(db_path)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM articles").fetchall()
        try:
            with pytest.raises(CheckpointError):
                db.checkpoint(connection)
        finally:
            reader.execute("ROLLBACK")
            reader.close()
            connection.close()
