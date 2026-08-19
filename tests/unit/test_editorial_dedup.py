"""editorial.dedup — Step 3 section 19 (pure logic; see
tests/integration/test_editorial_dedup_repository.py for the real-repository path).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from ai_news_editor.domain.enums import EditorialCategory
from ai_news_editor.editorial.dedup import (
    EditorialCategoryDriftError,
    check_category_consistency,
    established_category,
)

NEWS = EditorialCategory.NEWS
AI_TOOL = EditorialCategory.AI_TOOL


class TestEstablishedCategory:
    def test_an_empty_history_has_no_established_category(self) -> None:
        assert established_category([]) is None

    def test_a_history_of_only_none_has_no_established_category(self) -> None:
        assert established_category([None, None]) is None

    def test_a_single_category_is_established(self) -> None:
        assert established_category([NEWS]) == NEWS

    def test_none_entries_are_ignored_alongside_a_real_category(self) -> None:
        assert established_category([None, NEWS, None]) == NEWS

    def test_repeating_the_same_category_is_still_consistent(self) -> None:
        assert established_category([NEWS, NEWS, NEWS]) == NEWS

    def test_two_different_categories_in_history_raise(self) -> None:
        with pytest.raises(EditorialCategoryDriftError, match="already disagrees"):
            established_category([NEWS, AI_TOOL])


class TestCheckCategoryConsistency:
    def test_a_first_ever_classification_never_conflicts(self) -> None:
        check_category_consistency(uuid4(), NEWS, [])

    def test_reclassifying_as_the_same_category_is_allowed(self) -> None:
        check_category_consistency(uuid4(), NEWS, [NEWS])

    def test_a_different_category_is_rejected(self) -> None:
        """The exact case section 19 names: NEWS today, AI_TOOL tomorrow."""
        article_id = uuid4()
        with pytest.raises(EditorialCategoryDriftError, match="already classified as NEWS"):
            check_category_consistency(article_id, AI_TOOL, [NEWS])

    def test_a_none_only_history_never_conflicts(self) -> None:
        check_category_consistency(uuid4(), AI_TOOL, [None, None])
