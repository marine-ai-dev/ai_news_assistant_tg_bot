"""Deterministic processing orchestrator: raw items become editorial candidates.

    unprocessed RawItems → normalize → Article → duplicate detection → prefilter

Resumable and idempotent. Only raw items that have not yet produced an article are
considered, so running the command repeatedly converges rather than accumulating.

Nothing in this module calls an LLM, computes an embedding, or makes a judgement about
whether a story is interesting. Every decision is a named rule with a recorded reason.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ai_news_editor.domain.clock import now_utc, to_iso
from ai_news_editor.domain.enums import ArticleStatus, DuplicateReason, PrefilterReason
from ai_news_editor.domain.models import Article, CommunitySignal, RawItem
from ai_news_editor.editorial.prefilter import screen
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.pipeline.dedupe import NEAR_DUPLICATE_WINDOW, find_duplicate
from ai_news_editor.pipeline.normalize import NormalizationRejected, normalize
from ai_news_editor.pipeline.urls import try_canonicalize_url
from ai_news_editor.sources.hn_algolia import signal_fields
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    CommunitySignalRepository,
    RawItemRepository,
    SourceRepository,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessingReport:
    """Counts from one processing run."""

    considered: int = 0
    normalized: int = 0
    exact_duplicates: int = 0
    near_duplicates: int = 0
    possible_cross_source: int = 0
    screened_out: int = 0
    ready: int = 0
    rejected: int = 0
    signals_recorded: int = 0
    signals_attached: int = 0
    rejections: tuple[str, ...] = field(default_factory=tuple)
    screening_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def duplicates(self) -> int:
        return self.exact_duplicates + self.near_duplicates


def process(
    connection: sqlite3.Connection,
    *,
    limit: int | None = None,
    source_ids: list[str] | None = None,
) -> ProcessingReport:
    """Normalize, deduplicate and screen every unprocessed raw item."""
    articles_repo = ArticleRepository(connection)
    raw_repo = RawItemRepository(connection)
    sources_repo = SourceRepository(connection)
    signals_repo = CommunitySignalRepository(connection)

    sources = {source.id: source for source in sources_repo.list_all()}
    #: Community sources never become articles. Their records are attention metadata,
    #: and deriving an editorial candidate from one would make chatter look like news.
    signal_source_ids = {sid for sid, source in sources.items() if source.signal_only}

    already = articles_repo.normalized_raw_item_ids()
    pending = raw_repo.list_unprocessed(
        exclude_ids=already, source_ids=source_ids, limit=limit
    )

    logger.info("processing started", extra={"pending": len(pending)})

    normalized = exact = near = possible = screened = ready = rejected = 0
    signals_recorded = signals_attached = 0
    rejections: list[str] = []
    screening_reasons: dict[str, int] = {}

    for item in pending:
        if item.source_id in signal_source_ids:
            recorded, attached = _record_signal(item, signals_repo, articles_repo)
            signals_recorded += recorded
            signals_attached += attached
            continue

        outcome = normalize(item)
        if isinstance(outcome, NormalizationRejected):
            rejected += 1
            rejections.append(f"{item.source_id}: {outcome.reason}")
            logger.warning(
                "raw item could not be normalized",
                extra={"source_id": item.source_id, "reason": outcome.reason},
            )
            continue

        article = articles_repo.add(outcome)
        articles_repo.set_status(article.id, ArticleStatus.NORMALIZED)
        normalized += 1

        match = _detect_duplicate(article, articles_repo)
        if match is not None and not match.cross_source:
            articles_repo.mark_duplicate_of(article.id, match.duplicate_of_id, match.reason)
            if match.reason is DuplicateReason.NEAR_DUPLICATE_SIMHASH:
                near += 1
            else:
                exact += 1
            screening_reasons[PrefilterReason.DUPLICATE.value] = (
                screening_reasons.get(PrefilterReason.DUPLICATE.value, 0) + 1
            )
            logger.info(
                "duplicate detected",
                extra={
                    "article_id": str(article.id),
                    "duplicate_of": str(match.duplicate_of_id),
                    "reason": match.reason.value,
                },
            )
            continue

        if match is not None and match.cross_source:
            # Recorded, deliberately not acted on: secondary reporting of an official
            # announcement is worth keeping for corroboration later.
            articles_repo.mark_possible_duplicate(article.id, match.duplicate_of_id)
            possible += 1
            logger.info(
                "possible cross-source duplicate",
                extra={
                    "article_id": str(article.id),
                    "resembles": str(match.duplicate_of_id),
                    "reason": match.reason.value,
                },
            )

        verdict = screen(articles_repo.get(article.id))
        if verdict.screened_out and verdict.reason is not None:
            articles_repo.screen_out(article.id, verdict.reason)
            screened += 1
            screening_reasons[verdict.reason.value] = (
                screening_reasons.get(verdict.reason.value, 0) + 1
            )
            logger.info(
                "article screened out",
                extra={
                    "article_id": str(article.id),
                    "rule": verdict.rule_id,
                    "reason": verdict.reason.value,
                },
            )
        else:
            ready += 1

    attached = _attach_pending_signals(signals_repo, articles_repo)
    signals_attached += attached

    report = ProcessingReport(
        considered=len(pending),
        normalized=normalized,
        exact_duplicates=exact,
        near_duplicates=near,
        possible_cross_source=possible,
        screened_out=screened,
        ready=ready,
        rejected=rejected,
        signals_recorded=signals_recorded,
        signals_attached=signals_attached,
        rejections=tuple(rejections),
        screening_reasons=screening_reasons,
    )
    logger.info(
        "processing finished",
        extra={
            "normalized": normalized,
            "duplicates": report.duplicates,
            "screened_out": screened,
            "ready": ready,
            "signals": signals_recorded,
        },
    )
    return report


def _detect_duplicate(article: Article, articles_repo: ArticleRepository):  # type: ignore[no-untyped-def]
    window_start = to_iso(now_utc() - NEAR_DUPLICATE_WINDOW)
    candidates = articles_repo.find_duplicate_candidates(article, window_start=window_start)
    return find_duplicate(article, candidates)


def _record_signal(
    item: RawItem,
    signals_repo: CommunitySignalRepository,
    articles_repo: ArticleRepository,
) -> tuple[int, int]:
    """Turn a community raw item into a signal, attaching it if we hold the article."""
    canonical = try_canonicalize_url(item.url_original)
    article = articles_repo.find_by_canonical_url(canonical) if canonical else None
    extra = signal_fields(item.payload_raw)

    signal = CommunitySignal(
        source_id=item.source_id,
        external_id=item.external_id or str(item.id),
        article_id=article.id if article else None,
        canonical_url=canonical,
        title=item.title_original,
        points=extra.get("points"),
        num_comments=extra.get("num_comments"),
        author=item.author,
        posted_at=item.published_at,
        discussion_url=extra.get("discussion_url"),
    )
    recorded = signals_repo.add_if_absent(signal)
    return (1 if recorded else 0, 1 if recorded and article else 0)


def _attach_pending_signals(
    signals_repo: CommunitySignalRepository, articles_repo: ArticleRepository
) -> int:
    """Link signals seen before their article was collected.

    Attention often precedes our copy of a story, so unmatched signals are retried on
    every run rather than discarded at first sight.
    """
    attached = 0
    for signal in signals_repo.list_unattached():
        if not signal.canonical_url:
            continue
        article = articles_repo.find_by_canonical_url(signal.canonical_url)
        if article is not None:
            signals_repo.attach_to_article(signal.id, article.id)
            attached += 1
    return attached


def pipeline_stats(connection: sqlite3.Connection) -> dict[str, int]:
    """Funnel counts for the status command."""
    raw_repo = RawItemRepository(connection)
    articles_repo = ArticleRepository(connection)
    signals_repo = CommunitySignalRepository(connection)

    by_status = articles_repo.count_by_status()
    total_raw = raw_repo.count()
    processed = sum(by_status.values())
    return {
        "raw_items": total_raw,
        "unprocessed": max(0, total_raw - processed - signals_repo.count()),
        "articles": processed,
        "normalized": by_status.get(ArticleStatus.NORMALIZED.value, 0),
        "duplicates": by_status.get(ArticleStatus.DUPLICATE.value, 0),
        "screened_out": by_status.get(ArticleStatus.SCREENED_OUT.value, 0),
        "awaiting_evaluation": by_status.get(ArticleStatus.NORMALIZED.value, 0),
        "community_signals": signals_repo.count(),
        "signals_attached": signals_repo.count_attached(),
    }
