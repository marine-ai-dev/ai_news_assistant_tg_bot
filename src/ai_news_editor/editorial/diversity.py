"""Editorial diversity — Step 3 (AI News Agent v2).

A transparent, small scoring adjustment, not an opaque recommendation engine: a
candidate that repeats the channel's last few posts' content type or source family
gets a penalty, never an outright rejection. "Safety/relevance > diversity" — the
one strong, trustworthy candidate of the day still wins even if it repeats yesterday's
category, because nothing here ever removes a candidate from consideration, only
reorders it against its peers.

Deliberately separate from ``automation.pipeline``'s domain-cooldown (Step 2's fix for
OpenAI's 403s): that mechanism is HTTP-fetch reliability and *excludes* a domain
outright for the rest of one run. This module never imports it and never excludes
anything — different concern, different layer, kept apart on purpose.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from ai_news_editor.domain.enums import EditorialCategory

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RecentPost:
    """One already-published post, as far as diversity scoring cares."""

    editorial_category: EditorialCategory
    source_family: str | None


@dataclass(frozen=True, slots=True)
class DiversityWeights:
    """Every tunable number diversity scoring uses, in one place.

    Small on purpose — see the module docstring. A repetition penalty is a nudge, not
    a wall: nothing here can make a candidate's final score negative enough to be
    filtered out by this module itself, because this module never filters at all.
    """

    category_repetition_penalty: float = 8.0
    source_family_repetition_penalty: float = 6.0
    #: How many of the most recent posts are consulted. Only a *unanimous* run within
    #: this window counts as repetition — one different post in the window is enough
    #: to break it, which is what keeps "last 2 posts happened to both be NEWS" from
    #: penalizing a candidate as hard as "last 5 posts were all NEWS."
    lookback: int = 3


DEFAULT_WEIGHTS = DiversityWeights()


def diversity_adjustment(
    *,
    category: EditorialCategory,
    source_family: str | None,
    recent: Sequence[RecentPost],
    weights: DiversityWeights = DEFAULT_WEIGHTS,
) -> float:
    """A signed adjustment to add to a candidate's own editorial score.

    Zero unless the most recent ``weights.lookback`` posts unanimously share this
    candidate's category and/or source_family — a single differing post in that
    window is enough to clear the penalty, so this only fires on a genuine run, not
    "two of the last three happened to match."
    """
    window = list(recent)[-weights.lookback :] if weights.lookback > 0 else []
    if not window:
        return 0.0

    penalty = 0.0
    if all(post.editorial_category == category for post in window):
        penalty += weights.category_repetition_penalty
    if source_family is not None and all(post.source_family == source_family for post in window):
        penalty += weights.source_family_repetition_penalty
    return -penalty


@dataclass(frozen=True, slots=True)
class ScoredCandidate(Generic[T]):
    """One candidate, its editorial classification, and how diversity adjusted it."""

    candidate: T
    category: EditorialCategory
    source_family: str | None
    base_score: float
    adjustment: float

    @property
    def final_score(self) -> float:
        return self.base_score + self.adjustment


def rank(
    candidates: Sequence[tuple[T, EditorialCategory, str | None, float]],
    recent: Sequence[RecentPost],
    *,
    weights: DiversityWeights = DEFAULT_WEIGHTS,
) -> list[ScoredCandidate[T]]:
    """Score and order every candidate, highest final score first.

    ``candidates`` is ``(candidate, category, source_family, base_score)`` tuples —
    the base score is whatever editorial-relevance signal the caller already has
    (composite_score, a trust-tier preference, freshness, ...); this function only
    ever adds the diversity adjustment on top. Ties keep their original relative order
    (Python's sort is stable), so equal-quality candidates are not shuffled
    arbitrarily on top of the diversity preference.
    """
    scored = [
        ScoredCandidate(
            candidate=candidate,
            category=category,
            source_family=source_family,
            base_score=base_score,
            adjustment=diversity_adjustment(
                category=category, source_family=source_family, recent=recent, weights=weights
            ),
        )
        for candidate, category, source_family, base_score in candidates
    ]
    return sorted(scored, key=lambda s: s.final_score, reverse=True)
