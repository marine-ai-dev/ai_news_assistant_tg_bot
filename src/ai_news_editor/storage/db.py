"""SQLite connection management and a deterministic migration runner.

Migrations are plain ``.sql`` files named ``NNN_description.sql``, applied in numeric
order, each inside a transaction, each recorded in ``schema_migrations`` with a
checksum. Re-running is a no-op. Editing an already-applied migration is an error
rather than a silent divergence between what the file says and what the database is.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ai_news_editor.domain.clock import now_utc, to_iso
from ai_news_editor.domain.errors import MigrationError
from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class Migration:
    """One migration file."""

    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """A migration recorded as applied in the database."""

    version: int
    name: str
    checksum: str
    applied_at: str


def connect(database_path: Path, *, create_parents: bool = True) -> sqlite3.Connection:
    """Open a connection with the pragmas this application depends on.

    Foreign keys are off by default in SQLite and must be enabled per connection, so
    every connection goes through here rather than through ``sqlite3.connect``.
    """
    if create_parents:
        database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside one transaction, rolling back on any exception."""
    connection.execute("BEGIN")
    try:
        yield connection
    except Exception:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return every migration file in ascending version order.

    Raises:
        MigrationError: a filename is malformed or two files claim the same version.
    """
    migrations: list[Migration] = []
    seen: dict[int, Path] = {}

    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise MigrationError(
                f"migration filename {path.name!r} must look like '001_snake_case.sql'"
            )
        version = int(match.group(1))
        if version in seen:
            raise MigrationError(
                f"duplicate migration version {version:03d}: {seen[version].name} and {path.name}"
            )
        seen[version] = path
        migrations.append(Migration(version=version, name=match.group(2), path=path))

    return sorted(migrations, key=lambda m: m.version)


def split_statements(sql: str) -> list[str]:
    """Split a migration script into individually executable statements.

    ``executescript`` cannot be used because it issues an implicit COMMIT, which would
    defeat the surrounding transaction. Naive splitting on ``;`` would break the
    ``BEGIN ... END`` body of a trigger, so statement boundaries come from
    :func:`sqlite3.complete_statement`, which understands them.
    """
    statements: list[str] = []
    buffer = ""

    for line in sql.splitlines(keepends=True):
        buffer += line
        if buffer.strip() and sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""

    leftover = "\n".join(
        line for line in buffer.splitlines() if line.strip() and not line.strip().startswith("--")
    )
    if leftover:
        raise MigrationError(f"migration ends with an incomplete statement: {leftover[:80]!r}")

    return statements


def _ensure_migrations_table(connection: sqlite3.Connection) -> None:
    connection.execute(_SCHEMA_MIGRATIONS_DDL)


def applied_migrations(connection: sqlite3.Connection) -> list[AppliedMigration]:
    """Return migrations already recorded in the database, oldest first."""
    _ensure_migrations_table(connection)
    rows = connection.execute(
        "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [AppliedMigration(**dict(row)) for row in rows]


def migrate(connection: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Bring the database to the latest schema. Returns the migrations applied now.

    Safe to call repeatedly: an up-to-date database returns an empty list.

    Raises:
        MigrationError: an applied migration's file changed, or applying one failed.
    """
    available = discover_migrations(directory)
    already = {m.version: m for m in applied_migrations(connection)}

    for migration in available:
        recorded = already.get(migration.version)
        if recorded and recorded.checksum != migration.checksum:
            raise MigrationError(
                f"migration {migration.version:03d}_{migration.name} changed after it was "
                f"applied; create a new migration instead of editing history"
            )

    pending = [m for m in available if m.version not in already]
    for migration in pending:
        logger.info(
            "applying migration",
            extra={"version": migration.version, "migration": migration.name},
        )
        try:
            with transaction(connection) as conn:
                for statement in split_statements(migration.sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (migration.version, migration.name, migration.checksum, to_iso(now_utc())),
                )
        except sqlite3.Error as exc:
            raise MigrationError(
                f"migration {migration.version:03d}_{migration.name} failed: {exc}"
            ) from exc

    return pending


def schema_version(connection: sqlite3.Connection) -> int:
    """Highest applied migration version, or 0 for an empty database."""
    applied = applied_migrations(connection)
    return applied[-1].version if applied else 0


def pending_migrations(
    connection: sqlite3.Connection, directory: Path = MIGRATIONS_DIR
) -> list[Migration]:
    """Migrations present on disk but not yet applied."""
    applied = {m.version for m in applied_migrations(connection)}
    return [m for m in discover_migrations(directory) if m.version not in applied]
