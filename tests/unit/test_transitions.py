"""Lifecycle state machines.

The tests iterate over the full enum rather than a hand-written list of cases, so a
newly added status cannot slip through untested.
"""

from __future__ import annotations

import pytest

from ai_news_editor.domain.enums import ArticleStatus, DraftStatus
from ai_news_editor.domain.errors import IllegalStateTransition
from ai_news_editor.domain.transitions import (
    ARTICLE_TRANSITIONS,
    DRAFT_TRANSITIONS,
    NON_PUBLISHABLE_DRAFT_STATES,
    assert_article_transition,
    assert_draft_transition,
)


class TestCompleteness:
    def test_every_article_status_has_a_rule(self) -> None:
        assert set(ARTICLE_TRANSITIONS) == set(ArticleStatus)

    def test_every_draft_status_has_a_rule(self) -> None:
        assert set(DRAFT_TRANSITIONS) == set(DraftStatus)

    def test_no_rule_points_at_an_unknown_status(self) -> None:
        for targets in ARTICLE_TRANSITIONS.values():
            assert targets <= set(ArticleStatus)
        for targets in DRAFT_TRANSITIONS.values():
            assert targets <= set(DraftStatus)

    def test_no_status_transitions_to_itself(self) -> None:
        for status, targets in DRAFT_TRANSITIONS.items():
            assert status not in targets
        for status, targets in ARTICLE_TRANSITIONS.items():
            assert status not in targets


class TestArticleLifecycle:
    @pytest.mark.parametrize(
        "current,target",
        [
            (ArticleStatus.COLLECTED, ArticleStatus.NORMALIZED),
            (ArticleStatus.NORMALIZED, ArticleStatus.DUPLICATE),
            (ArticleStatus.NORMALIZED, ArticleStatus.SCREENED_OUT),
            (ArticleStatus.NORMALIZED, ArticleStatus.EVALUATED),
            (ArticleStatus.EVALUATED, ArticleStatus.SHORTLISTED),
            (ArticleStatus.SHORTLISTED, ArticleStatus.DRAFTED),
        ],
    )
    def test_allowed(self, current: ArticleStatus, target: ArticleStatus) -> None:
        assert_article_transition(current, target)

    @pytest.mark.parametrize(
        "current,target",
        [
            (ArticleStatus.COLLECTED, ArticleStatus.SHORTLISTED),
            (ArticleStatus.COLLECTED, ArticleStatus.DRAFTED),
            (ArticleStatus.DUPLICATE, ArticleStatus.EVALUATED),
            (ArticleStatus.SCREENED_OUT, ArticleStatus.SHORTLISTED),
            (ArticleStatus.DISCARDED, ArticleStatus.NORMALIZED),
        ],
    )
    def test_forbidden(self, current: ArticleStatus, target: ArticleStatus) -> None:
        with pytest.raises(IllegalStateTransition):
            assert_article_transition(current, target)

    @pytest.mark.parametrize(
        "terminal",
        [ArticleStatus.DUPLICATE, ArticleStatus.SCREENED_OUT, ArticleStatus.DISCARDED],
    )
    def test_terminal_states_have_no_exits(self, terminal: ArticleStatus) -> None:
        assert ARTICLE_TRANSITIONS[terminal] == frozenset()


class TestDraftLifecycle:
    @pytest.mark.parametrize(
        "current,target",
        [
            (DraftStatus.DRAFTED, DraftStatus.PENDING_REVIEW),
            (DraftStatus.PENDING_REVIEW, DraftStatus.APPROVED),
            (DraftStatus.PENDING_REVIEW, DraftStatus.REJECTED),
            (DraftStatus.PENDING_REVIEW, DraftStatus.NEEDS_REWRITE),
            (DraftStatus.NEEDS_REWRITE, DraftStatus.DRAFTED),
            (DraftStatus.APPROVED, DraftStatus.PUBLISHING),
            (DraftStatus.APPROVED, DraftStatus.PENDING_REVIEW),
            (DraftStatus.PUBLISHING, DraftStatus.PUBLISHED),
            (DraftStatus.PUBLISHING, DraftStatus.PUBLISH_FAILED),
            (DraftStatus.PUBLISH_FAILED, DraftStatus.APPROVED),
        ],
    )
    def test_allowed(self, current: DraftStatus, target: DraftStatus) -> None:
        assert_draft_transition(current, target)

    @pytest.mark.parametrize(
        "current",
        [
            DraftStatus.DRAFTED,
            DraftStatus.PENDING_REVIEW,
            DraftStatus.NEEDS_REWRITE,
            DraftStatus.REJECTED,
            DraftStatus.PUBLISHED,
            DraftStatus.PUBLISH_FAILED,
        ],
    )
    def test_only_approved_may_start_publishing(self, current: DraftStatus) -> None:
        """PUBLISHING is reachable from exactly one state: APPROVED."""
        with pytest.raises(IllegalStateTransition):
            assert_draft_transition(current, DraftStatus.PUBLISHING)

    @pytest.mark.parametrize("current", list(DraftStatus))
    def test_published_is_reachable_only_via_publishing(self, current: DraftStatus) -> None:
        if current is DraftStatus.PUBLISHING:
            assert_draft_transition(current, DraftStatus.PUBLISHED)
            return
        with pytest.raises(IllegalStateTransition):
            assert_draft_transition(current, DraftStatus.PUBLISHED)

    def test_rejected_is_terminal(self) -> None:
        assert DRAFT_TRANSITIONS[DraftStatus.REJECTED] == frozenset()

    def test_published_is_terminal(self) -> None:
        assert DRAFT_TRANSITIONS[DraftStatus.PUBLISHED] == frozenset()

    def test_failed_publish_returns_to_approved_never_to_review(self) -> None:
        """A failed send must not silently discard a real human approval."""
        allowed = DRAFT_TRANSITIONS[DraftStatus.PUBLISH_FAILED]
        assert DraftStatus.APPROVED in allowed
        assert DraftStatus.PENDING_REVIEW not in allowed
        assert DraftStatus.PUBLISHED not in allowed

    def test_non_publishable_set_covers_everything_but_approved(self) -> None:
        assert DraftStatus.APPROVED not in NON_PUBLISHABLE_DRAFT_STATES
        assert set(DraftStatus) - {DraftStatus.APPROVED} == NON_PUBLISHABLE_DRAFT_STATES


class TestErrorDetail:
    def test_error_names_the_entity_and_both_states(self) -> None:
        with pytest.raises(IllegalStateTransition) as info:
            assert_draft_transition(DraftStatus.REJECTED, DraftStatus.PUBLISHED)
        assert info.value.entity == "Draft"
        assert info.value.current == "REJECTED"
        assert info.value.target == "PUBLISHED"
