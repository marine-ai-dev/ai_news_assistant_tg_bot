"""Persistence for publication attempts — append-only.

This repository answers one question that has to be answered before every send: has
this exact version already reached this destination? The unique partial index in
migration 006 is what makes the answer trustworthy under a race; this module is how the
rest of the application asks.
"""

from __future__ import annotations

import sqlite3
from uuid import UUID

from ai_news_editor.domain.clock import to_iso
from ai_news_editor.domain.enums import PublicationStatus
from ai_news_editor.domain.errors import EntityNotFoundError, PublicationAlreadyExistsError
from ai_news_editor.domain.models import Publication
from ai_news_editor.observability.redaction import redact


def _to_domain(row: sqlite3.Row) -> Publication:
    return Publication.model_validate(dict(row))


class PublicationRepository:
    """Reads and appends ``publications``."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def add(self, publication: Publication) -> Publication:
        """Record one attempt.

        Raises:
            PublicationAlreadyExistsError: a successful publication of this exact
                version to this destination is already on record.
        """
        try:
            self._conn.execute(
                """
                INSERT INTO publications (id, draft_id, draft_version_id,
                                          review_decision_id, content_hash, channel,
                                          status, message_id, chat_id, attempt_no,
                                          failure_reason, published_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(publication.id),
                    str(publication.draft_id),
                    str(publication.draft_version_id),
                    str(publication.review_decision_id),
                    publication.content_hash,
                    publication.channel,
                    publication.status.value,
                    publication.message_id,
                    publication.chat_id,
                    publication.attempt_no,
                    # A failure reason can quote an upstream message. Scrub it on the
                    # way in: the database is one of the places a token must never be.
                    redact(publication.failure_reason) if publication.failure_reason else None,
                    to_iso(publication.published_at) if publication.published_at else None,
                    to_iso(publication.created_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            # SQLite names the columns, not the index, in a partial-index violation.
            message = str(exc)
            if "UNIQUE" in message and "draft_version_id" in message and "channel" in message:
                raise PublicationAlreadyExistsError(
                    f"draft version {publication.draft_version_id} was already published "
                    f"to {publication.channel}"
                ) from exc
            raise
        return publication

    def get(self, publication_id: UUID) -> Publication:
        row = self._conn.execute(
            "SELECT * FROM publications WHERE id = ?", (str(publication_id),)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"publication {publication_id} not found")
        return _to_domain(row)

    def successful_for_version(self, draft_version_id: UUID, channel: str) -> Publication | None:
        """The recorded success for this exact version and destination, if any.

        The idempotency check. Scoped to the version rather than the draft, because a
        different version is different content that carries its own approval.
        """
        row = self._conn.execute(
            """
            SELECT * FROM publications
            WHERE draft_version_id = ? AND channel = ? AND status = ?
            LIMIT 1
            """,
            (str(draft_version_id), channel, PublicationStatus.SUCCEEDED.value),
        ).fetchone()
        return _to_domain(row) if row else None

    def unresolved_for_version(self, draft_version_id: UUID, channel: str) -> Publication | None:
        """An attempt whose outcome was never learned, if one is on record.

        A send that may already have produced a post blocks the next one. Resolving it
        is a human's job, not a retry loop's.
        """
        row = self._conn.execute(
            """
            SELECT * FROM publications
            WHERE draft_version_id = ? AND channel = ? AND status = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (str(draft_version_id), channel, PublicationStatus.UNCERTAIN.value),
        ).fetchone()
        return _to_domain(row) if row else None

    def next_attempt_no(self, draft_version_id: UUID, channel: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(attempt_no) AS n FROM publications "
            "WHERE draft_version_id = ? AND channel = ?",
            (str(draft_version_id), channel),
        ).fetchone()
        return int(row["n"] or 0) + 1

    def list_for_draft(self, draft_id: UUID) -> list[Publication]:
        rows = self._conn.execute(
            "SELECT * FROM publications WHERE draft_id = ? ORDER BY created_at, id",
            (str(draft_id),),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def list_recent(self, limit: int = 50) -> list[Publication]:
        rows = self._conn.execute(
            "SELECT * FROM publications ORDER BY created_at DESC, id LIMIT ?", (limit,)
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) AS n FROM publications").fetchone()["n"]
        )
