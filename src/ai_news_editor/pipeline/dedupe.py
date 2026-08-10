"""Editorial duplicate detection.

Distinct from Phase 2's ingestion idempotency, which answers "have I already stored this
feed entry?". This module answers a different question: **are these two entries the same
news story?**

Layers, cheapest and most certain first:

===== ===================================== =========================================
Layer  Signal                                Catches
===== ===================================== =========================================
L1a    identical canonical URL               the same page arriving from two feeds
L1b    identical content fingerprint         verbatim syndication
L1c    identical title from the same source   a feed re-emitting an entry
L2     simhash within Hamming distance 3     rewordings and feed-vs-page variants
===== ===================================== =========================================

Cross-source matches are recorded as *possible* duplicates and nothing more: secondary
reporting is kept because it is useful for corroboration later, not deleted because an
official source exists. Full story clustering is deliberately out of scope.

Every decision carries a :class:`DuplicateReason`, so "why is B a duplicate of A?"
always has an answer that names a rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ai_news_editor.domain.enums import DuplicateReason, TrustTier
from ai_news_editor.domain.models import Article, DuplicateCandidate
from ai_news_editor.pipeline.fingerprint import DEFAULT_HAMMING_THRESHOLD, hamming_distance

#: Only articles seen within this window are compared for near-duplication. Bounds the
#: work and reflects reality: two stories two months apart are not the same news.
NEAR_DUPLICATE_WINDOW = timedelta(days=14)

#: Canonical preference by trust tier. An official announcement outranks reporting about
#: it, and community chatter can never be canonical for anything.
_TIER_RANK: dict[TrustTier, int] = {
    TrustTier.OFFICIAL: 0,
    TrustTier.REPUTABLE_SECONDARY: 1,
    TrustTier.UNVERIFIED: 2,
    TrustTier.COMMUNITY_SIGNAL: 3,
}


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    """A detected duplicate relationship."""

    duplicate_of_id: object
    reason: DuplicateReason
    #: True when the match spans two different sources, which is recorded as a
    #: *possible* duplicate rather than acted on.
    cross_source: bool = False


def find_duplicate(
    article: Article,
    candidates: list[DuplicateCandidate],
    *,
    threshold: int = DEFAULT_HAMMING_THRESHOLD,
) -> DuplicateMatch | None:
    """Return the strongest duplicate relationship for ``article``, or ``None``.

    ``candidates`` are pre-filtered by the repository to a bounded set: same URL, same
    fingerprints, or a shared simhash band within the recency window.
    """
    for candidate in candidates:
        if candidate.id == article.id:
            continue
        if candidate.canonical_url == article.canonical_url:
            return DuplicateMatch(
                candidate.id,
                DuplicateReason.SAME_CANONICAL_URL,
                cross_source=candidate.source_id != article.source_id,
            )

    for candidate in candidates:
        if candidate.id == article.id:
            continue
        if (
            article.content_hash
            and candidate.content_hash
            and candidate.content_hash == article.content_hash
        ):
            return DuplicateMatch(
                candidate.id,
                DuplicateReason.SAME_CONTENT_FINGERPRINT,
                cross_source=candidate.source_id != article.source_id,
            )

    for candidate in candidates:
        if candidate.id == article.id or candidate.source_id != article.source_id:
            continue
        if (
            article.title_fingerprint
            and candidate.title_fingerprint
            and candidate.title_fingerprint == article.title_fingerprint
        ):
            return DuplicateMatch(candidate.id, DuplicateReason.SAME_TITLE_SAME_SOURCE)

    return _nearest(article, candidates, threshold)


def _within_window(article: Article, candidate: DuplicateCandidate) -> bool:
    """Whether two articles are close enough in time to be the same story.

    A near-duplicate is the same news appearing twice at roughly the same moment. Two
    texts that merely *read* alike but were published weeks apart are a recurring
    column, not a duplicate — vendors publish "the latest AI news from <month>" every
    month, and those posts differ by one word while being genuinely different stories.

    Applied only when both publication times are known. An unknown date is not evidence
    of anything, so the pair stays eligible and the candidate query's own recency bound
    still applies.
    """
    if article.published_at is None or candidate.published_at is None:
        return True
    return abs(article.published_at - candidate.published_at) <= NEAR_DUPLICATE_WINDOW


def _nearest(
    article: Article, candidates: list[DuplicateCandidate], threshold: int
) -> DuplicateMatch | None:
    """Closest simhash neighbour within the threshold, if any.

    ``simhash`` is ``None`` for texts too short to fingerprint reliably; those are never
    matched this way, because short unrelated texts collide easily and a false merge
    silently removes a story from the pool.
    """
    if article.simhash is None:
        return None

    best: tuple[int, DuplicateCandidate] | None = None
    for candidate in candidates:
        if candidate.id == article.id or candidate.simhash is None:
            continue
        if not _within_window(article, candidate):
            continue
        distance = hamming_distance(article.simhash, candidate.simhash)
        if distance <= threshold and (best is None or distance < best[0]):
            best = (distance, candidate)

    if best is None:
        return None
    return DuplicateMatch(
        best[1].id,
        DuplicateReason.NEAR_DUPLICATE_SIMHASH,
        cross_source=best[1].source_id != article.source_id,
    )


def prefer_canonical(left: DuplicateCandidate, right: DuplicateCandidate) -> DuplicateCandidate:
    """Deterministically choose which of two articles should be the canonical one.

    Preference order, applied until one wins:

    1. higher trust tier — an official announcement beats reporting about it
    2. earlier publication — the original beats the follow-up
    3. richer text — more content is more useful to the editor later
    4. smaller id — an arbitrary but stable tie-break, so the result never depends
       on processing order

    A community signal can never win: it is not a factual source, and rank 3 keeps it
    last even against an unverified article.
    """
    for key in (
        lambda c: _TIER_RANK[c.trust_tier],
        lambda c: (c.published_at is None, c.published_at),
        lambda c: -c.text_length,
        lambda c: str(c.id),
    ):
        try:
            left_key, right_key = key(left), key(right)
        except TypeError:  # pragma: no cover - mismatched datetime comparison guard
            continue
        if left_key != right_key:
            return left if left_key < right_key else right
    return left
