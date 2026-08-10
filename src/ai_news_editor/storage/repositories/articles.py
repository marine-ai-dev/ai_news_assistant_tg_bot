"""Persistence for articles — normalized editorial candidates."""

from __future__ import annotations

import sqlite3
from uuid import UUID

from ai_news_editor.domain.clock import now_utc, to_iso
from ai_news_editor.domain.enums import ArticleStatus
from ai_news_editor.domain.errors import EntityNotFoundError
from ai_news_editor.domain.models import Article
from ai_news_editor.domain.transitions import assert_article_transition


def _to_domain(row: sqlite3.Row) -> Article:
    return Article.model_validate(dict(row))


class ArticleRepository:
    """Reads and writes ``articles``.

    Status changes go exclusively through :meth:`set_status`, which validates against
    the lifecycle table. No other method writes the ``status`` column.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def add(self, article: Article) -> Article:
        self._conn.execute(
            """
            INSERT INTO articles (id, raw_item_id, source_id, title, canonical_url, clean_text,
                                  language, published_at, content_hash, duplicate_of_id, status,
                                  filtered_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(article.id),
                str(article.raw_item_id),
                article.source_id,
                article.title,
                article.canonical_url,
                article.clean_text,
                article.language,
                to_iso(article.published_at) if article.published_at else None,
                article.content_hash,
                str(article.duplicate_of_id) if article.duplicate_of_id else None,
                article.status.value,
                article.filtered_by,
                to_iso(article.created_at),
                to_iso(article.updated_at),
            ),
        )
        return article

    def get(self, article_id: UUID) -> Article:
        row = self._conn.execute(
            "SELECT * FROM articles WHERE id = ?", (str(article_id),)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"article {article_id} not found")
        return _to_domain(row)

    def find_by_raw_item(self, raw_item_id: UUID) -> Article | None:
        row = self._conn.execute(
            "SELECT * FROM articles WHERE raw_item_id = ?", (str(raw_item_id),)
        ).fetchone()
        return _to_domain(row) if row else None

    def set_status(
        self,
        article_id: UUID,
        target: ArticleStatus,
        *,
        filtered_by: str | None = None,
    ) -> Article:
        """Move an article to ``target``, refusing transitions the lifecycle forbids."""
        article = self.get(article_id)
        assert_article_transition(article.status, target)

        self._conn.execute(
            "UPDATE articles SET status = ?, filtered_by = COALESCE(?, filtered_by), "
            "updated_at = ? WHERE id = ?",
            (target.value, filtered_by, to_iso(now_utc()), str(article_id)),
        )
        return self.get(article_id)

    def mark_duplicate(self, article_id: UUID, duplicate_of_id: UUID) -> Article:
        """Record that an article duplicates another, and move it to DUPLICATE."""
        if article_id == duplicate_of_id:
            raise ValueError("an article cannot be a duplicate of itself")
        article = self.get(article_id)
        assert_article_transition(article.status, ArticleStatus.DUPLICATE)

        self._conn.execute(
            "UPDATE articles SET duplicate_of_id = ?, status = ?, updated_at = ? WHERE id = ?",
            (
                str(duplicate_of_id),
                ArticleStatus.DUPLICATE.value,
                to_iso(now_utc()),
                str(article_id),
            ),
        )
        return self.get(article_id)

    def list_by_status(self, status: ArticleStatus, *, limit: int = 100) -> list[Article]:
        rows = self._conn.execute(
            "SELECT * FROM articles WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status.value, limit),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM articles GROUP BY status ORDER BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}
