"""Persistence for drafts and their immutable versions.

The single most important behaviour in this module is :meth:`DraftRepository.append_version`:
appending content to an approved draft moves it back to ``PENDING_REVIEW``. Editing
approved text therefore invalidates the approval at the storage layer, not merely by
convention in whatever code happens to call it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from uuid import UUID

from ai_news_editor.domain.clock import now_utc, to_iso
from ai_news_editor.domain.enums import AudienceTier, Category, DraftStatus, PostFormat
from ai_news_editor.domain.errors import EntityNotFoundError, RepositoryError
from ai_news_editor.domain.models import Draft, DraftVersion
from ai_news_editor.domain.transitions import assert_draft_transition
from ai_news_editor.storage.db import transaction

#: Draft states in which new content may be written. Terminal and in-flight states are
#: excluded: rewriting a published or in-flight post is a correctness hazard.
_APPENDABLE: frozenset[DraftStatus] = frozenset(
    {
        DraftStatus.DRAFTED,
        DraftStatus.PENDING_REVIEW,
        DraftStatus.NEEDS_REWRITE,
        DraftStatus.APPROVED,
    }
)

#: Where a draft lands after new content is appended. An approved draft must be
#: re-reviewed; a rewrite request is satisfied and returns to the normal flow.
_STATUS_AFTER_APPEND: dict[DraftStatus, DraftStatus] = {
    DraftStatus.APPROVED: DraftStatus.PENDING_REVIEW,
    DraftStatus.NEEDS_REWRITE: DraftStatus.DRAFTED,
}


def _draft_to_domain(row: sqlite3.Row) -> Draft:
    return Draft.model_validate(dict(row))


def _version_to_domain(row: sqlite3.Row) -> DraftVersion:
    data = dict(row)
    data["hashtags"] = tuple(json.loads(data.pop("hashtags_json")))
    data["writer_notes"] = tuple(json.loads(data.pop("writer_notes_json")))
    # content_hash is computed from the content, never read back as an input field.
    data.pop("content_hash", None)
    return DraftVersion.model_validate(data)


class DraftRepository:
    """Reads and writes ``drafts`` and ``draft_versions``."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    # -- creation ---------------------------------------------------------------

    def create(
        self,
        *,
        article_id: UUID,
        title: str,
        body: str,
        category: Category,
        audience: AudienceTier,
        source_attribution: str,
        created_by: str,
        hashtags: Sequence[str] = (),
        evaluation_id: UUID | None = None,
        source_url: str | None = None,
        post_format: PostFormat | None = None,
        style_version: str | None = None,
        writer_notes: Sequence[str] = (),
    ) -> tuple[Draft, DraftVersion]:
        """Create a draft together with its first version, atomically."""
        draft = Draft(
            article_id=article_id, evaluation_id=evaluation_id, status=DraftStatus.DRAFTED
        )
        version = DraftVersion(
            draft_id=draft.id,
            version_no=1,
            title=title,
            body=body,
            hashtags=tuple(hashtags),
            category=category,
            audience=audience,
            source_attribution=source_attribution,
            source_url=source_url,
            post_format=post_format,
            style_version=style_version,
            writer_notes=tuple(writer_notes),
            created_by=created_by,
        )

        with transaction(self._conn) as conn:
            conn.execute(
                "INSERT INTO drafts (id, article_id, evaluation_id, status, "
                "current_version_id, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (
                    str(draft.id),
                    str(draft.article_id),
                    str(evaluation_id) if evaluation_id else None,
                    draft.status.value,
                    to_iso(draft.created_at),
                    to_iso(draft.updated_at),
                ),
            )
            self._insert_version(conn, version)
            conn.execute(
                "UPDATE drafts SET current_version_id = ?, updated_at = ? WHERE id = ?",
                (str(version.id), to_iso(now_utc()), str(draft.id)),
            )

        return self.get(draft.id), version

    def append_version(
        self,
        draft_id: UUID,
        *,
        title: str,
        body: str,
        category: Category,
        audience: AudienceTier,
        source_attribution: str,
        created_by: str,
        hashtags: Sequence[str] = (),
        source_url: str | None = None,
        post_format: PostFormat | None = None,
        style_version: str | None = None,
        writer_notes: Sequence[str] = (),
    ) -> tuple[Draft, DraftVersion]:
        """Append a new immutable version and point the draft at it.

        If the draft was ``APPROVED``, it returns to ``PENDING_REVIEW``: the new content
        is content no human has approved.
        """
        draft = self.get(draft_id)
        if draft.status not in _APPENDABLE:
            raise RepositoryError(
                f"cannot append a version to draft {draft_id} in state {draft.status.value}"
            )

        next_no = self._next_version_no(draft_id)
        version = DraftVersion(
            draft_id=draft_id,
            version_no=next_no,
            title=title,
            body=body,
            hashtags=tuple(hashtags),
            category=category,
            audience=audience,
            source_attribution=source_attribution,
            source_url=source_url,
            post_format=post_format,
            style_version=style_version,
            writer_notes=tuple(writer_notes),
            created_by=created_by,
        )

        target_status = _STATUS_AFTER_APPEND.get(draft.status, draft.status)
        if target_status is not draft.status:
            assert_draft_transition(draft.status, target_status)

        with transaction(self._conn) as conn:
            self._insert_version(conn, version)
            conn.execute(
                "UPDATE drafts SET current_version_id = ?, status = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    str(version.id),
                    target_status.value,
                    to_iso(now_utc()),
                    str(draft_id),
                ),
            )

        return self.get(draft_id), version

    @staticmethod
    def _insert_version(conn: sqlite3.Connection, version: DraftVersion) -> None:
        conn.execute(
            """
            INSERT INTO draft_versions (id, draft_id, version_no, title, body, hashtags_json,
                                        category, audience, source_attribution, source_url,
                                        post_format, style_version, writer_notes_json,
                                        content_hash, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(version.id),
                str(version.draft_id),
                version.version_no,
                version.title,
                version.body,
                json.dumps(list(version.hashtags), ensure_ascii=False),
                version.category.value,
                version.audience.value,
                version.source_attribution,
                version.source_url,
                version.post_format.value if version.post_format else None,
                version.style_version,
                json.dumps(list(version.writer_notes), ensure_ascii=False),
                version.content_hash,
                version.created_by,
                to_iso(version.created_at),
            ),
        )

    def _next_version_no(self, draft_id: UUID) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) AS n FROM draft_versions WHERE draft_id = ?",
            (str(draft_id),),
        ).fetchone()
        return int(row["n"]) + 1

    # -- reads ------------------------------------------------------------------

    def get(self, draft_id: UUID) -> Draft:
        row = self._conn.execute("SELECT * FROM drafts WHERE id = ?", (str(draft_id),)).fetchone()
        if row is None:
            raise EntityNotFoundError(f"draft {draft_id} not found")
        return _draft_to_domain(row)

    def get_version(self, version_id: UUID) -> DraftVersion:
        row = self._conn.execute(
            "SELECT * FROM draft_versions WHERE id = ?", (str(version_id),)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"draft version {version_id} not found")
        return _version_to_domain(row)

    def current_version(self, draft_id: UUID) -> DraftVersion:
        """Return the version a draft currently points at."""
        draft = self.get(draft_id)
        if draft.current_version_id is None:
            raise EntityNotFoundError(f"draft {draft_id} has no versions")
        return self.get_version(draft.current_version_id)

    def list_versions(self, draft_id: UUID) -> list[DraftVersion]:
        rows = self._conn.execute(
            "SELECT * FROM draft_versions WHERE draft_id = ? ORDER BY version_no",
            (str(draft_id),),
        ).fetchall()
        return [_version_to_domain(row) for row in rows]

    def list_by_status(self, status: DraftStatus, *, limit: int = 100) -> list[Draft]:
        rows = self._conn.execute(
            "SELECT * FROM drafts WHERE status = ? ORDER BY created_at LIMIT ?",
            (status.value, limit),
        ).fetchall()
        return [_draft_to_domain(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM drafts GROUP BY status ORDER BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    # -- lifecycle --------------------------------------------------------------

    def set_status(self, draft_id: UUID, target: DraftStatus) -> Draft:
        """Move a draft to ``target``, refusing transitions the lifecycle forbids."""
        draft = self.get(draft_id)
        assert_draft_transition(draft.status, target)
        self._conn.execute(
            "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ?",
            (target.value, to_iso(now_utc()), str(draft_id)),
        )
        return self.get(draft_id)

    def claim_for_publishing(self, draft_id: UUID) -> bool:
        """Atomically move ``APPROVED -> PUBLISHING``; return whether this caller won.

        A compare-and-swap rather than a read-then-write: two concurrent publishers
        cannot both proceed, which is the concurrency half of publish-exactly-once.
        The publisher that consumes it arrives in Phase 7.
        """
        cursor = self._conn.execute(
            "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (
                DraftStatus.PUBLISHING.value,
                to_iso(now_utc()),
                str(draft_id),
                DraftStatus.APPROVED.value,
            ),
        )
        return cursor.rowcount == 1

    def find_by_article(self, article_id: UUID) -> Draft | None:
        """The draft for an article, if one exists. One draft per article in Phase 5."""
        row = self._conn.execute(
            "SELECT * FROM drafts WHERE article_id = ? ORDER BY created_at LIMIT 1",
            (str(article_id),),
        ).fetchone()
        return _draft_to_domain(row) if row else None

    def article_ids_with_drafts(self) -> set[UUID]:
        """Articles that already have a draft, so writing does not duplicate them."""
        rows = self._conn.execute("SELECT DISTINCT article_id FROM drafts").fetchall()
        return {UUID(row["article_id"]) for row in rows}

    def list_all(self, *, limit: int = 100) -> list[Draft]:
        rows = self._conn.execute(
            "SELECT * FROM drafts ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_draft_to_domain(row) for row in rows]
