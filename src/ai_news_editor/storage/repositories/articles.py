"""Persistence for articles — normalized editorial candidates."""

from __future__ import annotations

import sqlite3
from uuid import UUID

from ai_news_editor.domain.clock import now_utc, to_iso
from ai_news_editor.domain.enums import ArticleStatus, DuplicateReason, PrefilterReason, TrustTier
from ai_news_editor.domain.errors import EntityNotFoundError
from ai_news_editor.domain.models import Article, DuplicateCandidate
from ai_news_editor.domain.transitions import assert_article_transition
from ai_news_editor.storage.codecs import simhash_from_storage, simhash_to_storage


def _to_domain(row: sqlite3.Row) -> Article:
    data = dict(row)
    data["simhash"] = simhash_from_storage(data.get("simhash"))
    return Article.model_validate(data)


def _to_candidate(row: sqlite3.Row) -> DuplicateCandidate:
    return DuplicateCandidate(
        id=UUID(row["id"]),
        source_id=row["source_id"],
        canonical_url=row["canonical_url"],
        content_hash=row["content_hash"],
        title_fingerprint=row["title_fingerprint"],
        simhash=simhash_from_storage(row["simhash"]),
        trust_tier=TrustTier(row["trust_tier"]),
        published_at=row["published_at"],
        text_length=row["text_length"],
    )


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
                                  language, published_at, content_hash, title_fingerprint,
                                  simhash, duplicate_of_id, duplicate_reason,
                                  possible_duplicate_of_id, status, filtered_by,
                                  normalized_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                article.title_fingerprint,
                simhash_to_storage(article.simhash),
                str(article.duplicate_of_id) if article.duplicate_of_id else None,
                article.duplicate_reason.value if article.duplicate_reason else None,
                str(article.possible_duplicate_of_id) if article.possible_duplicate_of_id else None,
                article.status.value,
                article.filtered_by.value if article.filtered_by else None,
                to_iso(article.normalized_at) if article.normalized_at else None,
                to_iso(article.created_at),
                to_iso(article.updated_at),
            ),
        )
        return article

    def find_duplicate_candidates(
        self,
        article: Article,
        *,
        window_start: str | None = None,
        limit: int = 500,
    ) -> list[DuplicateCandidate]:
        """Fetch the bounded set of articles worth comparing against ``article``.

        Exact signals (canonical URL, content fingerprint, title fingerprint) are
        indexed point lookups. Near-duplicate candidates are the non-duplicate articles
        inside the recency window, capped by ``limit`` — a small indexed range scan
        rather than a sweep of the whole table.
        """
        rows: dict[str, sqlite3.Row] = {}

        def collect(sql: str, params: tuple[object, ...]) -> None:
            for row in self._conn.execute(sql, params).fetchall():
                rows[row["id"]] = row

        select = (
            "SELECT a.id, a.source_id, a.canonical_url, a.content_hash, a.title_fingerprint, "
            "a.simhash, a.published_at, LENGTH(COALESCE(a.clean_text, '')) AS text_length, "
            "s.trust_tier FROM articles a JOIN sources s ON s.id = a.source_id "
        )
        exclude = "a.id <> ? AND a.status <> 'DUPLICATE'"

        collect(
            select + f"WHERE {exclude} AND a.canonical_url = ? LIMIT ?",
            (str(article.id), article.canonical_url, limit),
        )
        if article.content_hash:
            collect(
                select + f"WHERE {exclude} AND a.content_hash = ? LIMIT ?",
                (str(article.id), article.content_hash, limit),
            )
        if article.title_fingerprint:
            collect(
                select + f"WHERE {exclude} AND a.title_fingerprint = ? LIMIT ?",
                (str(article.id), article.title_fingerprint, limit),
            )
        if article.simhash is not None:
            if window_start:
                collect(
                    select + f"WHERE {exclude} AND a.simhash IS NOT NULL AND a.created_at >= ? "
                    "ORDER BY a.created_at DESC LIMIT ?",
                    (str(article.id), window_start, limit),
                )
            else:
                collect(
                    select + f"WHERE {exclude} AND a.simhash IS NOT NULL "
                    "ORDER BY a.created_at DESC LIMIT ?",
                    (str(article.id), limit),
                )

        return [_to_candidate(row) for row in rows.values()]

    def mark_duplicate_of(
        self,
        article_id: UUID,
        duplicate_of_id: UUID,
        reason: DuplicateReason,
    ) -> Article:
        """Record a confirmed duplicate with the rule that decided it."""
        if article_id == duplicate_of_id:
            raise ValueError("an article cannot be a duplicate of itself")
        article = self.get(article_id)
        assert_article_transition(article.status, ArticleStatus.DUPLICATE)
        self._conn.execute(
            "UPDATE articles SET duplicate_of_id = ?, duplicate_reason = ?, status = ?, "
            "filtered_by = ?, updated_at = ? WHERE id = ?",
            (
                str(duplicate_of_id),
                reason.value,
                ArticleStatus.DUPLICATE.value,
                PrefilterReason.DUPLICATE.value,
                to_iso(now_utc()),
                str(article_id),
            ),
        )
        return self.get(article_id)

    def mark_possible_duplicate(self, article_id: UUID, other_id: UUID) -> Article:
        """Record a cross-source resemblance without changing status.

        Secondary reporting stays a live candidate: it may be exactly what corroborates
        a sensitive story later. This is a note for Phase 4, not a decision.
        """
        if article_id == other_id:
            raise ValueError("an article cannot be a possible duplicate of itself")
        self._conn.execute(
            "UPDATE articles SET possible_duplicate_of_id = ?, updated_at = ? WHERE id = ?",
            (str(other_id), to_iso(now_utc()), str(article_id)),
        )
        return self.get(article_id)

    def screen_out(self, article_id: UUID, reason: PrefilterReason) -> Article:
        """Move an article to SCREENED_OUT with a machine-readable reason."""
        article = self.get(article_id)
        assert_article_transition(article.status, ArticleStatus.SCREENED_OUT)
        self._conn.execute(
            "UPDATE articles SET status = ?, filtered_by = ?, updated_at = ? WHERE id = ?",
            (
                ArticleStatus.SCREENED_OUT.value,
                reason.value,
                to_iso(now_utc()),
                str(article_id),
            ),
        )
        return self.get(article_id)

    def normalized_raw_item_ids(self) -> set[UUID]:
        """Raw items that already produced an article, so processing is resumable."""
        rows = self._conn.execute("SELECT raw_item_id FROM articles").fetchall()
        return {UUID(row["raw_item_id"]) for row in rows}

    def find_by_canonical_url(self, canonical_url: str) -> Article | None:
        row = self._conn.execute(
            "SELECT * FROM articles WHERE canonical_url = ? ORDER BY created_at LIMIT 1",
            (canonical_url,),
        ).fetchone()
        return _to_domain(row) if row else None

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
