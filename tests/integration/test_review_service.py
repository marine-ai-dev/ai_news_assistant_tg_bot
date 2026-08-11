"""The review service — the layer a Telegram review bot will call instead of the CLI.

Approval lives in :mod:`publishing.gate` and is tested in ``tests/safety``. What is left
here is everything around it: what the queue offers, what an edit is allowed to be, and
what happens when a draft moves while somebody is looking at it.
"""

from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from ai_news_editor.domain.enums import AudienceTier, Category, DraftStatus, PostFormat
from ai_news_editor.domain.models import DraftVersion
from ai_news_editor.review.service import (
    ReviewError,
    apply_edit,
    length_note,
    reject_draft,
    request_rewrite,
    review_queue,
    status_counts,
    validate_edit,
)
from ai_news_editor.storage.repositories import DraftRepository
from tests.conftest import DRAFT_CONTENT

GOOD_BODY = (
    "Оновлений текст допису після редагування людиною. Він достатньо довгий, щоб "
    "пройти перевірку мінімальної довжини, і не містить жодної забороненої розмітки."
)


@pytest.fixture
def pending(seeded_article, drafts: DraftRepository):  # type: ignore[no-untyped-def]
    draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    return drafts.get(draft.id), version


class TestQueue:
    def test_a_draft_that_is_not_awaiting_review_is_not_offered(
        self, pending, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        """Asking for one draft by id must not bypass the status filter."""
        draft, _ = pending
        drafts.set_status(draft.id, DraftStatus.REJECTED)

        assert review_queue(connection, draft_id=draft.id) == []

    def test_a_category_filter_excludes_other_categories(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        _draft, version = pending
        assert review_queue(connection, category=version.category) != []

        other = next(c for c in Category if c is not version.category)
        assert review_queue(connection, category=other) == []

    def test_status_counts_reports_the_queue(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        assert status_counts(connection)[DraftStatus.PENDING_REVIEW.value] == 1

    def test_a_version_with_no_declared_format_has_no_length_note(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        item = review_queue(connection)[0]
        assert item.version.post_format is None
        assert length_note(item) is None

    def test_the_length_note_describes_the_format_target(
        self, pending, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        draft, _ = pending
        _updated, version = drafts.append_version(
            draft.id,
            **{**DRAFT_CONTENT, "post_format": PostFormat.STANDARD},  # type: ignore[arg-type]
        )
        assert version.post_format is PostFormat.STANDARD

        item = next(i for i in review_queue(connection) if i.draft.id == draft.id)
        assert length_note(item)


class TestEditValidation:
    """A human edit is held to the same rules as a written draft. Nothing is fixed up."""

    def test_an_empty_headline_is_refused(self) -> None:
        problem = validate_edit(
            headline="   ", body=GOOD_BODY, source_label="Alpha", source_url="https://a.invalid"
        )
        assert problem == "the headline is empty"

    def test_an_empty_body_is_refused(self) -> None:
        problem = validate_edit(
            headline="Заголовок", body="\n", source_label="Alpha", source_url="https://a.invalid"
        )
        assert problem == "the body is empty"

    @pytest.mark.parametrize("field", ["headline", "body"])
    def test_markup_outside_the_permitted_subset_is_refused(self, field: str) -> None:
        hostile = "<script>alert(1)</script>"
        problem = validate_edit(
            headline=hostile if field == "headline" else "Заголовок",
            body=GOOD_BODY if field == "headline" else f"{GOOD_BODY} {hostile}",
            source_label="Alpha",
            source_url="https://a.invalid",
        )
        assert problem is not None
        assert field in problem
        assert "script" in problem

    def test_a_post_over_the_hard_limit_is_refused(self) -> None:
        problem = validate_edit(
            headline="Заголовок",
            body="слово " * 900,
            source_label="Alpha",
            source_url="https://a.invalid",
        )
        assert problem is not None
        assert "3500" in problem or "long" in problem.lower()

    def test_a_post_under_the_hard_minimum_is_refused(self) -> None:
        problem = validate_edit(
            headline="Ок", body="Коротко.", source_label="Alpha", source_url="https://a.invalid"
        )
        assert problem is not None

    def test_a_missing_source_url_is_refused(self) -> None:
        """Every post carries a link. An edit cannot quietly drop it."""
        problem = validate_edit(
            headline="Заголовок", body=GOOD_BODY, source_label="Alpha", source_url=""
        )
        assert problem is not None

    def test_an_invalid_edit_is_refused_by_the_service(
        self, pending, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        draft, version = pending
        with pytest.raises(ReviewError):
            apply_edit(connection, draft.id, headline="Заголовок", body="Закоротко.")

        assert drafts.current_version(draft.id).id == version.id


class TestEditPreconditions:
    def test_editing_a_draft_with_no_version_is_refused(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        draft, _ = pending
        connection.execute(
            "UPDATE drafts SET current_version_id = NULL WHERE id = ?", (str(draft.id),)
        )
        with pytest.raises(ReviewError, match="no version"):
            apply_edit(connection, draft.id, headline="Заголовок", body=GOOD_BODY)

    def test_editing_a_superseded_version_is_refused(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        """The same staleness check approval uses, for the same reason."""
        draft, version_one = pending
        apply_edit(connection, draft.id, headline="🆕 Друга версія", body=GOOD_BODY)

        with pytest.raises(ReviewError, match="re-read"):
            apply_edit(
                connection,
                draft.id,
                headline="🆕 Третя версія",
                body=GOOD_BODY,
                expected_version_id=version_one.id,
            )

    def test_a_rewritten_draft_comes_back_to_the_queue(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        """NEEDS_REWRITE → edited by hand → awaiting review again, not lost."""
        draft, _ = pending
        request_rewrite(connection, draft.id, note="занадто сухо")
        assert review_queue(connection) == []

        updated, version = apply_edit(
            connection, draft.id, headline="🆕 Переписано", body=GOOD_BODY
        )
        assert updated.status is DraftStatus.PENDING_REVIEW
        assert version.version_no == 2
        assert [item.draft.id for item in review_queue(connection)] == [draft.id]


class TestDecisionPreconditions:
    def test_a_rejected_draft_cannot_be_rejected_again(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        draft, _ = pending
        reject_draft(connection, draft.id)

        with pytest.raises(ReviewError, match="REJECTED"):
            reject_draft(connection, draft.id)

    def test_a_draft_with_no_version_cannot_be_rejected(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        draft, _ = pending
        connection.execute(
            "UPDATE drafts SET current_version_id = NULL WHERE id = ?", (str(draft.id),)
        )
        with pytest.raises(ReviewError, match="no version"):
            reject_draft(connection, draft.id)


class TestSourceRecovery:
    """Drafts written before source_url had its own column still carry the link."""

    def _version(self, attribution: str, url: str | None) -> DraftVersion:
        return DraftVersion(
            draft_id=uuid4(),
            version_no=1,
            title="Заголовок",
            body=GOOD_BODY,
            category=Category.PRODUCT_UPDATE,
            audience=AudienceTier.GENERAL,
            source_attribution=attribution,
            source_url=url,
            created_by="test",
        )

    def test_the_column_wins_when_it_is_set(self) -> None:
        from ai_news_editor.writing.format import source_url_of

        version = self._version("🔗 Джерело: Alpha\nhttps://a.invalid/x", "https://a.invalid/y")
        assert source_url_of(version) == "https://a.invalid/y"

    def test_the_link_is_recovered_from_the_attribution_line(self) -> None:
        from ai_news_editor.writing.format import source_url_of

        version = self._version("🔗 Джерело: Alpha\nhttps://a.invalid/x", None)
        assert source_url_of(version) == "https://a.invalid/x"

    def test_an_attribution_with_no_link_recovers_nothing(self) -> None:
        from ai_news_editor.writing.format import source_url_of

        assert source_url_of(self._version("🔗 Джерело: Alpha", None)) == ""


class TestContentAwareReview:
    """The review screen has to describe editorial-original content honestly."""

    def _editorial_draft(self, connection: sqlite3.Connection):  # type: ignore[no-untyped-def]
        from ai_news_editor.domain.enums import ContentType, PromptTopic
        from ai_news_editor.domain.models import ContentItem, PromptBody
        from ai_news_editor.storage.repositories import ContentItemRepository

        item = ContentItemRepository(connection).add(
            ContentItem(
                content_type=ContentType.PROMPT,
                audience=AudienceTier.NEWCOMER,
                title="Що приготувати",
                topic=PromptTopic.FOOD,
                body=PromptBody(
                    what_you_can_do="вирішити, що готувати",
                    prompt_text="Я надішлю список продуктів. Запропонуй три страви з них.",
                    customization_tips=("вкажіть, скільки часу у вас є",),
                ),
                created_by="claude-code",
            )
        )
        drafts = DraftRepository(connection)
        draft, _version = drafts.create(
            content_item_id=item.id,
            content_type=ContentType.PROMPT,
            title="✨ Промпт: що приготувати",
            body=GOOD_BODY,
            category=Category.EVERYDAY_AI,
            audience=AudienceTier.NEWCOMER,
            source_attribution="Матеріал каналу",
            source_url=None,
            created_by="claude-code:content_v2",
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        return draft

    def test_the_queue_carries_the_content_item_and_no_article(
        self, connection: sqlite3.Connection
    ) -> None:
        draft = self._editorial_draft(connection)
        item = next(i for i in review_queue(connection) if i.draft.id == draft.id)

        assert item.article is None
        assert item.evaluation is None
        assert item.content_item is not None
        assert item.subject == "FOOD"

    def test_a_news_item_has_no_content_item_subject(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        draft, _ = pending
        item = next(i for i in review_queue(connection) if i.draft.id == draft.id)
        assert item.content_item is None
        assert item.subject is None

    def test_an_editorial_edit_does_not_require_a_source(
        self, connection: sqlite3.Connection
    ) -> None:
        """News keeps its link; original content has none to keep."""
        draft = self._editorial_draft(connection)
        _updated, version = apply_edit(
            connection, draft.id, headline="✨ Змінено", body=GOOD_BODY
        )
        assert version.version_no == 2

    def test_an_unparseable_source_url_is_reported_rather_than_raised(self) -> None:
        problem = validate_edit(
            headline="Заголовок",
            body=GOOD_BODY,
            source_label="Alpha",
            source_url="ftp://a.invalid/x",
        )
        assert problem is not None
        assert "http" in problem
