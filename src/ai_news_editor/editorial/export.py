"""Building an editorial batch: candidates going out for review.

Selection is deterministic and conservative. An article is eligible when it survived
processing (not a duplicate, not screened out) and has no *current* evaluation — one
whose fingerprint still matches the content a reviewer would be shown today.

The exported document is what the reviewer actually sees, so the fingerprint is
computed over exactly those fields. Nothing else is sent: no raw payloads, no full
20k-character bodies, no HTML.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from uuid import UUID, uuid4

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.content import compute_editorial_fingerprint
from ai_news_editor.domain.enums import ArticleStatus
from ai_news_editor.domain.errors import EntityNotFoundError
from ai_news_editor.domain.models import Article, Source
from ai_news_editor.editorial.schema import (
    EXCERPT_CHAR_LIMIT,
    EXCERPT_TRUNCATION_MARKER,
    BatchArticle,
    BatchSource,
    CommunityAttention,
    EditorialBatch,
)
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    CommunitySignalRepository,
    EvaluationRepository,
    SourceRepository,
)

logger = get_logger(__name__)


def build_excerpt(text: str | None) -> tuple[str | None, bool]:
    """Trim article text to the review excerpt. Returns the text and whether it was cut.

    Truncation is never silent: the marker is visible in the excerpt and the flag is
    carried on the batch item, so a reviewer can tell a short story from a shortened one.
    """
    if not text:
        return None, False
    if len(text) <= EXCERPT_CHAR_LIMIT:
        return text, False
    keep = EXCERPT_CHAR_LIMIT - len(EXCERPT_TRUNCATION_MARKER)
    return text[:keep].rstrip() + EXCERPT_TRUNCATION_MARKER, True


def fingerprint_for(article: Article, excerpt: str | None) -> str:
    """The editorial fingerprint of an article as it would be reviewed today."""
    return compute_editorial_fingerprint(
        title=article.title,
        canonical_url=article.canonical_url,
        excerpt=excerpt,
        published_at=article.published_at.isoformat() if article.published_at else None,
    )


def build_batch(
    connection: sqlite3.Connection,
    *,
    limit: int = 20,
    source_ids: list[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    force: bool = False,
    batch_id: str | None = None,
    diverse: bool = True,
) -> EditorialBatch:
    """Select eligible candidates and assemble a batch for review.

    Args:
        force: include articles that already have a current evaluation. Without it,
            re-exporting is a no-op for anything already judged.
        diverse: round-robin across sources instead of taking whatever is newest, so a
            single prolific feed cannot fill the whole batch.
    """
    articles_repo = ArticleRepository(connection)
    sources_repo = SourceRepository(connection)
    signals_repo = CommunitySignalRepository(connection)
    evaluations_repo = EvaluationRepository(connection)

    sources = {source.id: source for source in sources_repo.list_all()}
    candidates = articles_repo.list_by_status(ArticleStatus.NORMALIZED, limit=2000)

    if source_ids:
        candidates = [a for a in candidates if a.source_id in source_ids]
    if since is not None:
        candidates = [a for a in candidates if a.published_at and a.published_at >= since]
    if until is not None:
        candidates = [a for a in candidates if a.published_at and a.published_at <= until]

    # Newest first: recency is the strongest proxy for editorial value we have before
    # anything has judged the story.
    candidates.sort(
        key=lambda a: (a.published_at is None, a.published_at or a.created_at), reverse=True
    )

    evaluated = evaluations_repo.current_fingerprints()

    selected: list[BatchArticle] = []
    prepared: list[tuple[Article, str, str | None, bool]] = []
    for article in candidates:
        excerpt, truncated = build_excerpt(article.clean_text)
        fingerprint = fingerprint_for(article, excerpt)
        if not force and evaluated.get(article.id) == fingerprint:
            continue
        prepared.append((article, fingerprint, excerpt, truncated))

    ordered = _round_robin(prepared) if diverse else prepared

    for article, fingerprint, excerpt, truncated in ordered[:limit]:
        source = sources.get(article.source_id)
        selected.append(
            _to_batch_article(article, source, fingerprint, excerpt, truncated, signals_repo)
        )

    batch = EditorialBatch(
        batch_id=batch_id or f"batch-{now_utc():%Y%m%dT%H%M%S}-{uuid4().hex[:8]}",
        articles=selected,
    )
    logger.info(
        "editorial batch built",
        extra={
            "batch_id": batch.batch_id,
            "articles": len(selected),
            "eligible": len(prepared),
            "force": force,
        },
    )
    return batch


def _round_robin(
    prepared: list[tuple[Article, str, str | None, bool]],
) -> list[tuple[Article, str, str | None, bool]]:
    """Interleave candidates by source so one prolific feed cannot dominate a batch."""
    by_source: dict[str, list[tuple[Article, str, str | None, bool]]] = {}
    for entry in prepared:
        by_source.setdefault(entry[0].source_id, []).append(entry)

    ordered: list[tuple[Article, str, str | None, bool]] = []
    while any(by_source.values()):
        for source_id in sorted(by_source):
            queue = by_source[source_id]
            if queue:
                ordered.append(queue.pop(0))
    return ordered


def _to_batch_article(
    article: Article,
    source: Source | None,
    fingerprint: str,
    excerpt: str | None,
    truncated: bool,
    signals_repo: CommunitySignalRepository,
) -> BatchArticle:
    signals = signals_repo.list_for_article(article.id)
    community = None
    if signals:
        strongest = max(signals, key=lambda s: s.points or 0)
        community = CommunityAttention(
            hacker_news_points=strongest.points,
            hacker_news_comments=strongest.num_comments,
            hacker_news_url=strongest.discussion_url,
        )

    return BatchArticle(
        article_id=article.id,
        source=BatchSource(
            id=article.source_id,
            name=source.name if source else article.source_id,
            trust_tier=source.trust_tier if source else "UNVERIFIED",  # type: ignore[arg-type]
            editorial_role=" ".join(source.editorial_role.split())
            if source and source.editorial_role
            else None,
            signal_only=source.signal_only if source else False,
        ),
        title=article.title,
        canonical_url=article.canonical_url,
        published_at=article.published_at.isoformat() if article.published_at else None,
        excerpt=excerpt,
        excerpt_truncated=truncated,
        excerpt_chars=len(excerpt or ""),
        community=community,
        content_fingerprint=fingerprint,
    )


def stale_evaluations(connection: sqlite3.Connection) -> list[tuple[UUID, str]]:
    """Articles whose most recent evaluation no longer matches their current content."""
    articles_repo = ArticleRepository(connection)
    evaluations_repo = EvaluationRepository(connection)

    stale: list[tuple[UUID, str]] = []
    for article_id, fingerprint in evaluations_repo.latest_fingerprints().items():
        try:
            article = articles_repo.get(article_id)
        except EntityNotFoundError:
            # An evaluation whose article is gone is not a staleness question.
            logger.warning(
                "evaluation refers to a missing article",
                extra={"article_id": str(article_id)},
            )
            continue
        excerpt, _ = build_excerpt(article.clean_text)
        if fingerprint_for(article, excerpt) != fingerprint:
            stale.append((article_id, article.title))
    return stale
