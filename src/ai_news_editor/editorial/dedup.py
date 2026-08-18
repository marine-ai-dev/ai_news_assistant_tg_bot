"""Editorial-category dedup consistency — Step 3 (AI News Agent v2), section 19.

Dedup itself is URL-based, at the ``Article`` layer — ``canonical_url``,
``content_hash``, and ``simhash`` (see ``ArticleRepository``) — and this module does
not touch, re-implement, or replace any of it. What it adds is one narrow check on top:
a single article must not be classified as NEWS today and AI_TOOL tomorrow. That check
operates only on the append-only ``Evaluation`` history the existing dedup'd article
already accumulates (``EvaluationRepository.history_for_article``), never on a second
matching mechanism of its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from ai_news_editor.domain.enums import EditorialCategory
from ai_news_editor.storage.repositories import EvaluationRepository


class EditorialCategoryDriftError(ValueError):
    """A story was already classified under a different editorial category."""


def established_category(history: Sequence[EditorialCategory | None]) -> EditorialCategory | None:
    """The one editorial category this article's evaluation history has settled on.

    ``history`` is every evaluation's ``editorial_category`` for one article, in any
    order. ``None`` entries (evaluations made before this classification existed, or
    left unclassified) never count as an established category — there is nothing to be
    consistent with yet, so they are ignored rather than treated as a value.

    Raises:
        EditorialCategoryDriftError: the history itself already disagrees with
            itself — two prior evaluations recorded two different categories for the
            same article. That is a bug from before this check existed, not something
            this function can resolve, so it is surfaced rather than silently picking
            one.
    """
    seen = {category for category in history if category is not None}
    if len(seen) > 1:
        conflicting = ", ".join(sorted(category.value for category in seen))
        raise EditorialCategoryDriftError(
            f"this article's evaluation history already disagrees with itself: "
            f"recorded as {conflicting}"
        )
    return next(iter(seen), None)


def check_category_consistency(
    article_id: UUID,
    proposed: EditorialCategory,
    history: Sequence[EditorialCategory | None],
) -> None:
    """Raises if ``proposed`` would reclassify an article away from its established category.

    A first-ever classification (``history`` empty or entirely ``None``) never
    conflicts — every category starts out unestablished.
    """
    settled = established_category(history)
    if settled is not None and settled != proposed:
        raise EditorialCategoryDriftError(
            f"article {article_id} was already classified as {settled.value}; "
            f"cannot reclassify it as {proposed.value}"
        )


def check_consistency_for_article(
    evaluations: EvaluationRepository, article_id: UUID, proposed: EditorialCategory
) -> None:
    """Convenience wrapper: reads real evaluation history, then checks consistency."""
    history = [
        evaluation.editorial_category for evaluation in evaluations.history_for_article(article_id)
    ]
    check_category_consistency(article_id, proposed, history)
