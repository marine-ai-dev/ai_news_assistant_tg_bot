"""Persistence for the human review audit trail — append-only."""

from __future__ import annotations

import sqlite3
from uuid import UUID

from ai_news_editor.domain.clock import to_iso
from ai_news_editor.domain.enums import ReviewAction
from ai_news_editor.domain.errors import EntityNotFoundError
from ai_news_editor.domain.models import ReviewDecision


def _to_domain(row: sqlite3.Row) -> ReviewDecision:
    return ReviewDecision.model_validate(dict(row))


class ReviewDecisionRepository:
    """Reads and appends ``review_decisions``.

    Every human action is recorded against the exact draft version and content hash
    that was on screen. Nothing here updates or deletes; the database enforces it.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def add(self, decision: ReviewDecision) -> ReviewDecision:
        self._conn.execute(
            """
            INSERT INTO review_decisions (id, draft_id, draft_version_id, content_hash,
                                          action, actor, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(decision.id),
                str(decision.draft_id),
                str(decision.draft_version_id),
                decision.content_hash,
                decision.action.value,
                decision.actor,
                decision.note,
                to_iso(decision.created_at),
            ),
        )
        return decision

    def get(self, decision_id: UUID) -> ReviewDecision:
        row = self._conn.execute(
            "SELECT * FROM review_decisions WHERE id = ?", (str(decision_id),)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"review decision {decision_id} not found")
        return _to_domain(row)

    def list_for_draft(self, draft_id: UUID) -> list[ReviewDecision]:
        rows = self._conn.execute(
            "SELECT * FROM review_decisions WHERE draft_id = ? ORDER BY created_at, id",
            (str(draft_id),),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def latest_approval(self, draft_id: UUID, version_id: UUID) -> ReviewDecision | None:
        """Most recent APPROVE recorded for one exact version, if any.

        Scoped to a version id on purpose: an approval of an earlier version must never
        be discoverable as an approval of the current one.
        """
        row = self._conn.execute(
            """
            SELECT * FROM review_decisions
            WHERE draft_id = ? AND draft_version_id = ? AND action = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (str(draft_id), str(version_id), ReviewAction.APPROVE.value),
        ).fetchone()
        return _to_domain(row) if row else None

    def count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) AS n FROM review_decisions").fetchone()["n"]
        )
