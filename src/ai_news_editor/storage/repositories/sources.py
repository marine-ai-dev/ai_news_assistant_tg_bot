"""Persistence for configured sources."""

from __future__ import annotations

import json
import sqlite3

from ai_news_editor.domain.clock import now_utc, to_iso
from ai_news_editor.domain.errors import EntityNotFoundError
from ai_news_editor.domain.models import Source


def _to_domain(row: sqlite3.Row) -> Source:
    data = dict(row)
    data["config"] = json.loads(data.pop("config_json"))
    return Source.model_validate(data)


class SourceRepository:
    """Reads and writes ``sources``."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def upsert(self, source: Source) -> Source:
        """Insert a source, or update it in place if the slug already exists.

        Sources are declared in configuration, so re-running a sync must converge
        rather than fail.
        """
        updated = source.model_copy(update={"updated_at": now_utc()})
        self._conn.execute(
            """
            INSERT INTO sources (id, name, kind, url, trust_tier, signal_only, enabled,
                                 language, publisher, poll_interval_minutes, config_json,
                                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name,
                kind = excluded.kind,
                url = excluded.url,
                trust_tier = excluded.trust_tier,
                signal_only = excluded.signal_only,
                enabled = excluded.enabled,
                language = excluded.language,
                publisher = excluded.publisher,
                poll_interval_minutes = excluded.poll_interval_minutes,
                config_json = excluded.config_json,
                updated_at = excluded.updated_at
            """,
            (
                updated.id,
                updated.name,
                updated.kind.value,
                updated.url,
                updated.trust_tier.value,
                int(updated.signal_only),
                int(updated.enabled),
                updated.language,
                updated.publisher,
                updated.poll_interval_minutes,
                json.dumps(updated.config, ensure_ascii=False, sort_keys=True),
                to_iso(updated.created_at),
                to_iso(updated.updated_at),
            ),
        )
        return self.get(updated.id)

    def get(self, source_id: str) -> Source:
        row = self._conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise EntityNotFoundError(f"source {source_id!r} not found")
        return _to_domain(row)

    def find(self, source_id: str) -> Source | None:
        row = self._conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return _to_domain(row) if row else None

    def list_all(self, *, enabled_only: bool = False) -> list[Source]:
        sql = "SELECT * FROM sources"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return [_to_domain(row) for row in self._conn.execute(sql).fetchall()]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"])
