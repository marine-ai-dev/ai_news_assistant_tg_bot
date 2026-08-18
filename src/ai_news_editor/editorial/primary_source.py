"""Primary-source preference — Step 3 (AI News Agent v2), section 6.

When two already-collected candidates report the same story — linked via
``Article.possible_duplicate_of_id``, the existing conservative cross-source match
recorded during normalization (see ``ArticleRepository`` and migration 003) — prefer
whichever one comes from the higher-trust source: Tier A (OFFICIAL) over an equivalent
Tier B (REPUTABLE_SECONDARY) report of the same story, Tier B over Tier C, and so on.

This is not a search engine and does no new fetching or matching of its own: it only
ever compares candidates already in the pool, using a same-story link the pipeline
already establishes for a different purpose (corroboration bookkeeping). Nothing here
excludes the lower-tier article either — same "reorder, never remove" discipline as
``editorial.diversity``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from uuid import UUID

from ai_news_editor.domain.enums import TrustTier
from ai_news_editor.domain.models import Article

#: Higher wins. Unrecognized/missing tiers rank below every real tier rather than
#: raising, since this module only compares — it never validates tier legitimacy
#: (that is ``sources.capability``'s job).
_TIER_RANK: dict[TrustTier, int] = {
    TrustTier.OFFICIAL: 3,
    TrustTier.REPUTABLE_SECONDARY: 2,
    TrustTier.COMMUNITY_SIGNAL: 1,
    TrustTier.UNVERIFIED: 0,
}


def story_groups(articles: Sequence[Article]) -> list[list[Article]]:
    """Cluster ``articles`` into same-story groups.

    Uses only the ``possible_duplicate_of_id`` link the normalization stage already
    records — a single conservative pairwise match, not a transitive similarity graph
    — via a small union-find so that A-links-to-B and C-links-to-B still end up in one
    group. An article with no link is its own singleton group.
    """
    by_id = {article.id: article for article in articles}
    parent: dict[UUID, UUID] = {article.id: article.id for article in articles}

    def find(node: UUID) -> UUID:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: UUID, right: UUID) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_left] = root_right

    for article in articles:
        target = article.possible_duplicate_of_id
        if target is not None and target in by_id:
            union(article.id, target)

    groups: dict[UUID, list[Article]] = {}
    for article in articles:
        groups.setdefault(find(article.id), []).append(article)
    return list(groups.values())


def pick_primary(
    group: Sequence[Article], trust_tier_of: Callable[[Article], TrustTier]
) -> Article:
    """The best-evidenced article within one same-story ``group``.

    Highest trust tier wins. Ties (including an empty/unknown tier lookup) keep the
    group's original relative order — ``max`` only replaces on a strictly greater key,
    so the first-seen article of the top tier is returned, never a re-shuffled pick.
    """
    return max(group, key=lambda article: _TIER_RANK.get(trust_tier_of(article), -1))


def prefer_primary_sources(
    articles: Sequence[Article], trust_tier_of: Callable[[Article], TrustTier]
) -> dict[UUID, Article]:
    """For every article, the preferred (highest-trust) version of its story.

    Every article id in ``articles`` is a key, including ones that already are the
    preferred pick — a caller can use this to decide "should I use this candidate's
    own reporting, or defer to a same-story Tier A candidate that already exists in
    the pool?" without needing to know cluster membership itself.
    """
    preferred: dict[UUID, Article] = {}
    for group in story_groups(articles):
        primary = pick_primary(group, trust_tier_of)
        for article in group:
            preferred[article.id] = primary
    return preferred
