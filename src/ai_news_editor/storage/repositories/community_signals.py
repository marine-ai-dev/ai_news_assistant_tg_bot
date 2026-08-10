"""Persistence for community attention signals."""

from __future__ import annotations

import sqlite3
from uuid import UUID

from ai_news_editor.domain.clock import to_iso
from ai_news_editor.domain.models import CommunitySignal


def _to_domain(row: sqlite3.Row) -> CommunitySignal:
    return CommunitySignal.model_validate(dict(row))


class CommunitySignalRepository:
    """Reads and writes ``community_signals``.

    Kept in its own table and its own repository so a signal can never be mistaken for
    a source: nothing here writes to ``articles`` beyond attaching a reference, and no
    signal text ever becomes article content.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def add_if_absent(self, signal: CommunitySignal) -> bool:
        """Insert unless this discussion was already recorded. Returns whether it was new."""
        cursor = self._conn.execute(
            """
            INSERT INTO community_signals (id, source_id, external_id, article_id, canonical_url,
                                           title, points, num_comments, author, posted_at,
                                           discussion_url, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_id, external_id) DO NOTHING
            """,
            (
                str(signal.id),
                signal.source_id,
                signal.external_id,
                str(signal.article_id) if signal.article_id else None,
                signal.canonical_url,
                signal.title,
                signal.points,
                signal.num_comments,
                signal.author,
                to_iso(signal.posted_at) if signal.posted_at else None,
                signal.discussion_url,
                to_iso(signal.observed_at),
            ),
        )
        return cursor.rowcount == 1

    def attach_to_article(self, signal_id: UUID, article_id: UUID) -> None:
        """Link a previously unmatched signal to an article discovered later."""
        self._conn.execute(
            "UPDATE community_signals SET article_id = ? WHERE id = ?",
            (str(article_id), str(signal_id)),
        )

    def list_unattached(self, *, limit: int = 500) -> list[CommunitySignal]:
        """Signals with no article yet — a discussion we hold no story for."""
        rows = self._conn.execute(
            "SELECT * FROM community_signals WHERE article_id IS NULL "
            "AND canonical_url IS NOT NULL ORDER BY observed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def list_for_article(self, article_id: UUID) -> list[CommunitySignal]:
        rows = self._conn.execute(
            "SELECT * FROM community_signals WHERE article_id = ? ORDER BY observed_at",
            (str(article_id),),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) AS n FROM community_signals").fetchone()["n"]
        )

    def count_attached(self) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) AS n FROM community_signals WHERE article_id IS NOT NULL"
            ).fetchone()["n"]
        )
