"""Explicit lifecycle state machines for articles and drafts.

Every status change in the application goes through :func:`assert_article_transition`
or :func:`assert_draft_transition`. Nothing else is permitted to write a status, which
is what keeps impossible combinations (e.g. ``REJECTED -> PUBLISHING``) out of the
database by construction rather than by review discipline.
"""

from __future__ import annotations

from ai_news_editor.domain.enums import ArticleStatus, DraftStatus
from ai_news_editor.domain.errors import IllegalStateTransition

ARTICLE_TRANSITIONS: dict[ArticleStatus, frozenset[ArticleStatus]] = {
    ArticleStatus.COLLECTED: frozenset({ArticleStatus.NORMALIZED, ArticleStatus.DISCARDED}),
    ArticleStatus.NORMALIZED: frozenset(
        {
            ArticleStatus.DUPLICATE,
            ArticleStatus.SCREENED_OUT,
            ArticleStatus.EVALUATED,
            ArticleStatus.DISCARDED,
        }
    ),
    ArticleStatus.EVALUATED: frozenset({ArticleStatus.SHORTLISTED, ArticleStatus.DISCARDED}),
    ArticleStatus.SHORTLISTED: frozenset({ArticleStatus.DRAFTED, ArticleStatus.DISCARDED}),
    ArticleStatus.DRAFTED: frozenset({ArticleStatus.DISCARDED}),
    # Terminal states.
    ArticleStatus.DUPLICATE: frozenset(),
    ArticleStatus.SCREENED_OUT: frozenset(),
    ArticleStatus.DISCARDED: frozenset(),
}

DRAFT_TRANSITIONS: dict[DraftStatus, frozenset[DraftStatus]] = {
    DraftStatus.DRAFTED: frozenset({DraftStatus.PENDING_REVIEW, DraftStatus.REJECTED}),
    DraftStatus.PENDING_REVIEW: frozenset(
        {DraftStatus.APPROVED, DraftStatus.REJECTED, DraftStatus.NEEDS_REWRITE}
    ),
    DraftStatus.NEEDS_REWRITE: frozenset({DraftStatus.DRAFTED, DraftStatus.REJECTED}),
    # An approved draft that gets edited returns to review: the new content has not
    # been approved by anyone. This edge is the lifecycle half of the safety gate.
    DraftStatus.APPROVED: frozenset(
        {DraftStatus.PUBLISHING, DraftStatus.PENDING_REVIEW, DraftStatus.REJECTED}
    ),
    DraftStatus.PUBLISHING: frozenset({DraftStatus.PUBLISHED, DraftStatus.PUBLISH_FAILED}),
    # A failed send returns to APPROVED for retry. It must never fall back to
    # PENDING_REVIEW (that would silently drop a real approval) and must never skip
    # ahead to PUBLISHED.
    DraftStatus.PUBLISH_FAILED: frozenset({DraftStatus.APPROVED, DraftStatus.REJECTED}),
    # Terminal states.
    DraftStatus.PUBLISHED: frozenset(),
    DraftStatus.REJECTED: frozenset(),
}

#: Draft states from which content may never be sent to the channel. Kept as an
#: explicit set so the safety tests can assert over the whole enum rather than over a
#: hand-written list that could drift.
NON_PUBLISHABLE_DRAFT_STATES: frozenset[DraftStatus] = frozenset(
    set(DraftStatus) - {DraftStatus.APPROVED}
)


def article_can_transition(current: ArticleStatus, target: ArticleStatus) -> bool:
    """Return whether an article may move from ``current`` to ``target``."""
    return target in ARTICLE_TRANSITIONS[current]


def draft_can_transition(current: DraftStatus, target: DraftStatus) -> bool:
    """Return whether a draft may move from ``current`` to ``target``."""
    return target in DRAFT_TRANSITIONS[current]


def assert_article_transition(current: ArticleStatus, target: ArticleStatus) -> None:
    """Raise :class:`IllegalStateTransition` unless the article transition is allowed."""
    if not article_can_transition(current, target):
        raise IllegalStateTransition("Article", current.value, target.value)


def assert_draft_transition(current: DraftStatus, target: DraftStatus) -> None:
    """Raise :class:`IllegalStateTransition` unless the draft transition is allowed."""
    if not draft_can_transition(current, target):
        raise IllegalStateTransition("Draft", current.value, target.value)
