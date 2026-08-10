"""Persistence for raw collected items — append-only provenance."""

from __future__ import annotations

import sqlite3
from uuid import UUID

from ai_news_editor.domain.clock import to_iso
from ai_news_editor.domain.errors import EntityNotFoundError
from ai_news_editor.domain.models import RawItem


def _to_domain(row: sqlite3.Row) -> RawItem:
    return RawItem.model_validate(dict(row))


class RawItemRepository:
    """Reads and appends ``raw_items``.

    There is deliberately no update or delete method: the table records what a source
    actually returned, and the database enforces that with triggers.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def add(self, item: RawItem) -> RawItem:
        self._conn.execute(
            """
            INSERT INTO raw_items (id, source_id, external_id, title_original, url_original,
                                   author, published_at, fetched_at, summary_raw, content_raw,
                                   payload_raw, content_type, fetch_run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(item.id),
                item.source_id,
                item.external_id,
                item.title_original,
                item.url_original,
                item.author,
                to_iso(item.published_at) if item.published_at else None,
                to_iso(item.fetched_at),
                item.summary_raw,
                item.content_raw,
                item.payload_raw,
                item.content_type,
                item.fetch_run_id,
            ),
        )
        return item

    def add_if_absent(self, item: RawItem) -> bool:
        """Insert unless this source already delivered that entry. Returns whether it was new.

        Ingestion-level idempotency only: identity is ``(source_id, external_id)``,
        enforced by a unique index, so re-reading the same feed cannot accumulate
        duplicates. This says nothing about two *different* sources covering the same
        story — that is editorial deduplication, and it belongs to a later phase.

        ``INSERT ... ON CONFLICT DO NOTHING`` rather than check-then-insert: one
        statement, so there is no window between the check and the write. The conflict
        target repeats the index's ``WHERE`` clause because the index is partial —
        SQLite will not match a partial index otherwise.

        An item with no ``external_id`` is always inserted, since there is nothing to
        deduplicate on. Adapters are expected to supply one, deriving it deterministically
        when the source does not.
        """
        cursor = self._conn.execute(
            """
            INSERT INTO raw_items (id, source_id, external_id, title_original, url_original,
                                   author, published_at, fetched_at, summary_raw, content_raw,
                                   payload_raw, content_type, fetch_run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_id, external_id) WHERE external_id IS NOT NULL DO NOTHING
            """,
            (
                str(item.id),
                item.source_id,
                item.external_id,
                item.title_original,
                item.url_original,
                item.author,
                to_iso(item.published_at) if item.published_at else None,
                to_iso(item.fetched_at),
                item.summary_raw,
                item.content_raw,
                item.payload_raw,
                item.content_type,
                item.fetch_run_id,
            ),
        )
        return cursor.rowcount == 1

    def get(self, item_id: UUID) -> RawItem:
        row = self._conn.execute(
            "SELECT * FROM raw_items WHERE id = ?", (str(item_id),)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"raw item {item_id} not found")
        return _to_domain(row)

    def exists_external_id(self, source_id: str, external_id: str) -> bool:
        """Whether this source already delivered an item with that stable id."""
        row = self._conn.execute(
            "SELECT 1 FROM raw_items WHERE source_id = ? AND external_id = ? LIMIT 1",
            (source_id, external_id),
        ).fetchone()
        return row is not None

    def list_by_source(self, source_id: str, *, limit: int = 100) -> list[RawItem]:
        rows = self._conn.execute(
            "SELECT * FROM raw_items WHERE source_id = ? ORDER BY fetched_at DESC LIMIT ?",
            (source_id, limit),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM raw_items").fetchone()["n"])

    def list_unprocessed(
        self,
        *,
        exclude_ids: set[UUID],
        source_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[RawItem]:
        """Raw items that have not yet produced an article, oldest first.

        Oldest-first matters for duplicate detection: the earliest version of a story
        becomes the article that later copies are matched against, which keeps the
        canonical choice stable across runs.
        """
        sql = "SELECT * FROM raw_items"
        params: list[object] = []
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            sql += f" WHERE source_id IN ({placeholders})"
            params.extend(source_ids)
        sql += " ORDER BY COALESCE(published_at, fetched_at), id"

        rows = self._conn.execute(sql, tuple(params)).fetchall()
        items: list[RawItem] = []
        for row in rows:
            item = _to_domain(row)
            if item.id in exclude_ids:
                continue
            items.append(item)
            if limit is not None and len(items) >= limit:
                break
        return items
