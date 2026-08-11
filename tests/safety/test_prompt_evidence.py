"""A prompt post must rest on a demonstration somebody published. Never skip these.

Phase 7.5 let prompts be editorial-original. In practice that meant inventing something
plausible and presenting it as advice — and a prompt that reads well is indistinguishable,
to a reader, from a prompt that was shown to work.

The correction has to be structural. A style-guide paragraph would be followed by whoever
read it and ignored by the next session that did not. So these tests ask one question in
several ways: **can content without a demonstration reach a channel?**

The answer must be no, including when a human approves it. Approval means "this reads
well and I want it out", which is a different claim from "somebody demonstrated this".
"""

from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    ContentType,
    DraftStatus,
    EvidenceStatus,
    PostFormat,
    PromptRepresentation,
    PromptTopic,
    SourceTier,
)
from ai_news_editor.domain.models import (
    ContentItem,
    ExplainerBody,
    PromptBody,
    PromptEvidence,
)
from ai_news_editor.publishing.eligibility import (
    NotPublishableError,
    assert_publishable,
    publication_problem,
)
from ai_news_editor.publishing.gate import approve_draft, authorization_for_approved_draft
from ai_news_editor.storage.repositories import ContentItemRepository, DraftRepository
from ai_news_editor.writing.schema import STYLE_VERSION

pytestmark = pytest.mark.safety

SOURCE_URL = "https://openai.com/index/example-workflow"

POST_BODY = (
    "Автор перевірив, як ШІ працює з довгим документом. Нижче — промпт, який він "
    "використав, і що з цього вийшло. Джерело наприкінці, щоб можна було подивитися "
    "самому.\n\n"
    f"🔗 {SOURCE_URL}"
)


def evidence(**overrides: object) -> PromptEvidence:
    data: dict[str, object] = {
        "source_url": SOURCE_URL,
        "source_title": "Example tested workflow",
        "source_tier": SourceTier.OFFICIAL_PRODUCT,
        "tested_by": "OpenAI",
        "tool_used": "ChatGPT",
        "what_was_tested": "підсумок довгого PDF із посиланнями на сторінки",
        "observed_result": "модель повернула структурований підсумок",
    }
    data.update(overrides)
    return PromptEvidence(**data)  # type: ignore[arg-type]


def prompt_item(
    connection: sqlite3.Connection,
    *,
    status: EvidenceStatus,
    with_evidence: bool = True,
) -> ContentItem:
    return ContentItemRepository(connection).add(
        ContentItem(
            content_type=ContentType.PROMPT,
            audience=AudienceTier.NEWCOMER,
            title=f"prompt-{uuid4().hex[:6]}",
            topic=PromptTopic.WORK,
            body=PromptBody(
                what_you_can_do="швидко зрозуміти, про що довгий документ",
                prompt_text="Ось документ. Зроби короткий підсумок головних пунктів.",
                customization_tips=("попросіть цитати замість переказу",),
            ),
            evidence=evidence() if with_evidence else None,
            evidence_status=status,
            created_by="claude-code",
        )
    )


def prompt_draft(
    connection: sqlite3.Connection, *, status: EvidenceStatus, with_evidence: bool = True
):  # type: ignore[no-untyped-def]
    item = prompt_item(connection, status=status, with_evidence=with_evidence)
    drafts = DraftRepository(connection)
    draft, version = drafts.create(
        content_item_id=item.id,
        content_type=ContentType.PROMPT,
        title="✨ Промпт: підсумок довгого документа",
        body=POST_BODY,
        category=Category.AI_FOR_WORK,
        audience=AudienceTier.NEWCOMER,
        source_attribution=f"🔗 Джерело: Example tested workflow\n{SOURCE_URL}",
        source_url=SOURCE_URL,
        post_format=PostFormat.QUICK,
        created_by="claude-code:content_v2",
    )
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    return drafts.get(draft.id), version


class TestEvidenceIsRequired:
    def test_a_source_backed_prompt_needs_its_evidence(self) -> None:
        with pytest.raises(ValidationError, match="without the evidence"):
            ContentItem(
                content_type=ContentType.PROMPT,
                audience=AudienceTier.NEWCOMER,
                title="t",
                topic=PromptTopic.WORK,
                body=PromptBody(
                    what_you_can_do="x",
                    prompt_text="Ось документ. Зроби короткий підсумок головних пунктів.",
                    customization_tips=("a",),
                ),
                evidence_status=EvidenceStatus.VERIFIED_SOURCE_BACKED,
                created_by="claude-code",
            )

    def test_a_prompt_needs_an_evidence_status_at_all(self) -> None:
        """A blank status would be a third, unlabelled category. There are three."""
        with pytest.raises(ValidationError, match="needs an evidence status"):
            ContentItem(
                content_type=ContentType.PROMPT,
                audience=AudienceTier.NEWCOMER,
                title="t",
                topic=PromptTopic.WORK,
                body=PromptBody(
                    what_you_can_do="x",
                    prompt_text="Ось документ. Зроби короткий підсумок головних пунктів.",
                    customization_tips=("a",),
                ),
                created_by="claude-code",
            )

    @pytest.mark.parametrize(
        "missing",
        ["source_title", "tested_by", "tool_used", "what_was_tested", "observed_result"],
    )
    def test_every_evidence_field_is_required(self, missing: str) -> None:
        with pytest.raises(ValidationError):
            evidence(**{missing: ""})

    def test_a_source_url_that_is_not_a_link_is_refused(self) -> None:
        """Python cannot check a page exists. It can refuse something that never could."""
        with pytest.raises(ValidationError, match="http"):
            evidence(source_url="somebody on reddit said so")

    def test_the_model_version_is_optional_and_never_inferred(self) -> None:
        """A source that says "ChatGPT" gets "ChatGPT". Guessing is inventing evidence."""
        assert evidence().model_version is None
        assert evidence(model_version="GPT-5.6 Luna").model_version == "GPT-5.6 Luna"

    def test_limitations_may_be_genuinely_empty(self) -> None:
        """An honest absence, not a field to fill with plausible caveats."""
        assert evidence().limitations == ()

    def test_an_explainer_carries_no_evidence(self) -> None:
        """Explainers are editorial-original by design and say so."""
        with pytest.raises(ValidationError, match="not a tested-prompt demonstration"):
            ContentItem(
                content_type=ContentType.EXPLAINER,
                audience=AudienceTier.NEWCOMER,
                title="t",
                body=ExplainerBody(
                    concept="Промпт",
                    simple_explanation="x",
                    real_life_example="y",
                    why_it_matters="z",
                ),
                evidence=evidence(),
                created_by="claude-code",
            )

    def test_an_explainer_carries_no_evidence_status_either(self) -> None:
        with pytest.raises(ValidationError, match="PROMPT concept"):
            ContentItem(
                content_type=ContentType.EXPLAINER,
                audience=AudienceTier.NEWCOMER,
                title="t",
                body=ExplainerBody(
                    concept="Промпт",
                    simple_explanation="x",
                    real_life_example="y",
                    why_it_matters="z",
                ),
                evidence_status=EvidenceStatus.LEGACY_UNVERIFIED,
                created_by="claude-code",
            )


class TestPublicationEligibility:
    def test_a_source_backed_prompt_is_publishable(
        self, connection: sqlite3.Connection
    ) -> None:
        draft, _ = prompt_draft(connection, status=EvidenceStatus.VERIFIED_SOURCE_BACKED)
        assert publication_problem(connection, draft) is None

    def test_a_legacy_prompt_is_not_publishable(
        self, connection: sqlite3.Connection
    ) -> None:
        draft, _ = prompt_draft(
            connection, status=EvidenceStatus.LEGACY_UNVERIFIED, with_evidence=False
        )
        problem = publication_problem(connection, draft)
        assert problem is not None
        assert "demonstration" in problem

    def test_the_refusal_says_what_to_do_instead(
        self, connection: sqlite3.Connection
    ) -> None:
        """And what not to do: inventing a source for it is the tempting wrong move."""
        draft, _ = prompt_draft(
            connection, status=EvidenceStatus.LEGACY_UNVERIFIED, with_evidence=False
        )
        problem = publication_problem(connection, draft)
        assert problem is not None
        assert "inventing a source" in problem

    def test_a_prompt_whose_source_showed_nothing_is_not_publishable(
        self, connection: sqlite3.Connection
    ) -> None:
        draft, _ = prompt_draft(connection, status=EvidenceStatus.INSUFFICIENT_EVIDENCE)
        assert publication_problem(connection, draft) is not None

    def test_assert_publishable_raises_with_the_reason(
        self, connection: sqlite3.Connection
    ) -> None:
        draft, _ = prompt_draft(
            connection, status=EvidenceStatus.LEGACY_UNVERIFIED, with_evidence=False
        )
        with pytest.raises(NotPublishableError, match="demonstration"):
            assert_publishable(connection, draft)


class TestApprovalCannotOverrideEvidence:
    """The rule that matters most: a human saying yes is not evidence."""

    def test_a_legacy_prompt_cannot_be_approved(
        self, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        draft, _ = prompt_draft(
            connection, status=EvidenceStatus.LEGACY_UNVERIFIED, with_evidence=False
        )
        with pytest.raises(NotPublishableError):
            approve_draft(connection, draft.id)

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW

    def test_a_refused_approval_records_no_decision(
        self, connection: sqlite3.Connection
    ) -> None:
        from ai_news_editor.review.service import review_history

        draft, _ = prompt_draft(
            connection, status=EvidenceStatus.LEGACY_UNVERIFIED, with_evidence=False
        )
        with pytest.raises(NotPublishableError):
            approve_draft(connection, draft.id)

        assert review_history(connection, draft.id) == []

    def test_a_prompt_approved_before_the_rule_still_cannot_publish(
        self, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        """An approval recorded under the old rules must not carry content past the new one."""
        from ai_news_editor.publishing.gate import verify_publication

        draft, _ = prompt_draft(connection, status=EvidenceStatus.VERIFIED_SOURCE_BACKED)
        authorization = approve_draft(connection, draft.id)

        # The source is re-checked and turns out to demonstrate nothing.
        connection.execute(
            "UPDATE content_items SET evidence_status = ? WHERE id = ?",
            (EvidenceStatus.INSUFFICIENT_EVIDENCE.value, str(draft.content_item_id)),
        )

        with pytest.raises(NotPublishableError):
            verify_publication(connection, authorization)

    def test_a_source_backed_prompt_approves_normally(
        self, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        draft, version = prompt_draft(
            connection, status=EvidenceStatus.VERIFIED_SOURCE_BACKED
        )
        authorization = approve_draft(connection, draft.id)

        assert drafts.get(draft.id).status is DraftStatus.APPROVED
        assert authorization.authorizes(version)
        assert authorization_for_approved_draft(connection, draft.id) is not None


class TestOtherContentTypesAreUnaffected:
    def test_news_is_not_subject_to_the_prompt_rule(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        from tests.conftest import DRAFT_CONTENT

        draft, _ = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        assert publication_problem(connection, drafts.get(draft.id)) is None

    def test_news_still_approves_and_publishes(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        from tests.conftest import DRAFT_CONTENT

        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        authorization = approve_draft(connection, draft.id)

        assert authorization.authorizes(version)
        assert drafts.get(draft.id).status is DraftStatus.APPROVED

    def test_an_explainer_is_not_subject_to_the_prompt_rule(
        self, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        item = ContentItemRepository(connection).add(
            ContentItem(
                content_type=ContentType.EXPLAINER,
                audience=AudienceTier.NEWCOMER,
                title="Що таке промпт",
                body=ExplainerBody(
                    concept="Промпт",
                    simple_explanation="Промпт — це те, що ви пишете ШІ.",
                    real_life_example="Як записка колезі.",
                    why_it_matters="Від формулювання залежить відповідь.",
                ),
                created_by="claude-code",
            )
        )
        draft, _ = drafts.create(
            content_item_id=item.id,
            content_type=ContentType.EXPLAINER,
            title="🧠 Що таке промпт",
            body=POST_BODY,
            category=Category.EXPLAINED_SIMPLY,
            audience=AudienceTier.NEWCOMER,
            source_attribution="Матеріал каналу",
            source_url=None,
            created_by="claude-code:content_v2",
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)

        assert publication_problem(connection, drafts.get(draft.id)) is None
        approve_draft(connection, draft.id)
        assert drafts.get(draft.id).status is DraftStatus.APPROVED


class TestRepresentationIsHonest:
    def test_the_default_is_adapted_not_verbatim(self) -> None:
        """Claiming a quotation we did not check is the wrong default to have."""
        body = PromptBody(
            what_you_can_do="x",
            prompt_text="Ось документ. Зроби короткий підсумок головних пунктів.",
            customization_tips=("a",),
        )
        assert body.representation is PromptRepresentation.ADAPTED

    def test_a_reconstruction_can_be_labelled_as_one(self) -> None:
        body = PromptBody(
            what_you_can_do="x",
            prompt_text="Ось документ. Зроби короткий підсумок головних пунктів.",
            customization_tips=("a",),
            representation=PromptRepresentation.WORKFLOW_RECONSTRUCTION,
        )
        assert body.representation is PromptRepresentation.WORKFLOW_RECONSTRUCTION

    def test_the_representation_survives_storage(
        self, connection: sqlite3.Connection
    ) -> None:
        item = prompt_item(connection, status=EvidenceStatus.VERIFIED_SOURCE_BACKED)
        stored = ContentItemRepository(connection).get(item.id)
        assert isinstance(stored.body, PromptBody)
        assert stored.body.representation is item.body.representation  # type: ignore[union-attr]

    def test_the_evidence_survives_storage(self, connection: sqlite3.Connection) -> None:
        item = prompt_item(connection, status=EvidenceStatus.VERIFIED_SOURCE_BACKED)
        stored = ContentItemRepository(connection).get(item.id)

        assert stored.evidence is not None
        assert stored.evidence.source_url == SOURCE_URL
        assert stored.evidence.tested_by == "OpenAI"
        assert stored.evidence_status is EvidenceStatus.VERIFIED_SOURCE_BACKED


class TestSchemaRefusals:
    """The batch contract, at the edges."""

    def _batch(self, **item_overrides: object) -> dict[str, object]:
        item: dict[str, object] = {
            "content_type": "PROMPT",
            "title": "t",
            "audience": "NEWCOMER",
            "topic": "WORK",
            "what_you_can_do": "x",
            "prompt_text": "Ось документ. Зроби короткий підсумок головних пунктів.",
            "customization_tips": ["a"],
            "representation": "ADAPTED",
            "evidence": {
                "source_url": SOURCE_URL,
                "source_title": "Example",
                "source_tier": "OFFICIAL_PRODUCT",
                "tested_by": "OpenAI",
                "tool_used": "ChatGPT",
                "what_was_tested": "x",
                "observed_result": "y",
            },
            "post": {
                "headline": "✨ Заголовок",
                "body": (
                    "Ось документ. Зроби короткий підсумок головних пунктів. "
                    + "Достатньо довгий текст, щоб пройти мінімальну довжину допису. " * 2
                    + SOURCE_URL
                ),
                "category": "AI_FOR_WORK",
                "post_format": "STANDARD",
            },
        }
        item.update(item_overrides)
        return {
            "schema_version": "1",
            "style_version": STYLE_VERSION,
            "batch_id": "b",
            "items": [item],
        }

    def test_a_well_formed_prompt_validates(self) -> None:
        from ai_news_editor.content.schema import ContentBatch

        batch = ContentBatch.model_validate(self._batch())
        assert batch.items[0].evidence.tested_by == "OpenAI"  # type: ignore[union-attr]

    def test_a_prompt_without_evidence_is_refused(self) -> None:
        from ai_news_editor.content.schema import ContentBatch

        payload = self._batch()
        del payload["items"][0]["evidence"]  # type: ignore[index]
        with pytest.raises(ValidationError):
            ContentBatch.model_validate(payload)

    def test_a_source_url_that_is_not_a_link_is_refused(self) -> None:
        from ai_news_editor.content.schema import ContentBatch

        payload = self._batch()
        payload["items"][0]["evidence"]["source_url"] = "somebody on reddit"  # type: ignore[index]
        with pytest.raises(ValidationError, match="http"):
            ContentBatch.model_validate(payload)

    def test_a_post_that_does_not_link_its_source_is_refused(self) -> None:
        """A report without a link is indistinguishable from an invention."""
        from ai_news_editor.content.schema import ContentBatch

        payload = self._batch()
        body = payload["items"][0]["post"]["body"]  # type: ignore[index]
        payload["items"][0]["post"]["body"] = body.replace(SOURCE_URL, "")  # type: ignore[index]
        with pytest.raises(ValidationError, match="source link"):
            ContentBatch.model_validate(payload)

    def test_an_unknown_source_tier_is_refused(self) -> None:
        from ai_news_editor.content.schema import ContentBatch

        payload = self._batch()
        payload["items"][0]["evidence"]["source_tier"] = "A_FRIEND_SAID"  # type: ignore[index]
        with pytest.raises(ValidationError):
            ContentBatch.model_validate(payload)

    def test_an_unknown_representation_is_refused(self) -> None:
        from ai_news_editor.content.schema import ContentBatch

        with pytest.raises(ValidationError):
            ContentBatch.model_validate(self._batch(representation="TOTALLY_ORIGINAL"))


class TestReviewCardShowsEvidence:
    """The reviewer judges the source, so the source has to be on the card."""

    def test_the_terminal_card_shows_who_tested_it(
        self, connection: sqlite3.Connection
    ) -> None:
        from ai_news_editor.bot import render
        from ai_news_editor.review.service import review_queue

        prompt_draft(connection, status=EvidenceStatus.VERIFIED_SOURCE_BACKED)
        item = review_queue(connection)[0]
        card = render.review_card(item, position=1, total=1)

        assert "Перевіряв: OpenAI" in card
        assert "Інструмент: ChatGPT" in card
        assert SOURCE_URL in card
        assert "VERIFIED_SOURCE_BACKED" in card

    def test_the_card_shows_requirements_and_limitations_when_present(
        self, connection: sqlite3.Connection
    ) -> None:
        """A NEWCOMER told to upload a file needs to know their plan may not allow it."""
        from ai_news_editor.bot import render
        from ai_news_editor.review.service import review_queue

        item = ContentItemRepository(connection).add(
            ContentItem(
                content_type=ContentType.PROMPT,
                audience=AudienceTier.NEWCOMER,
                title="prompt-with-caveats",
                topic=PromptTopic.WORK,
                body=PromptBody(
                    what_you_can_do="x",
                    prompt_text="Ось документ. Зроби короткий підсумок головних пунктів.",
                    customization_tips=("a",),
                ),
                evidence=evidence(
                    model_version="GPT-5.6 Luna",
                    requires=("завантаження файлу",),
                    limitations=("у безкоштовному плані ліміти",),
                ),
                evidence_status=EvidenceStatus.VERIFIED_SOURCE_BACKED,
                created_by="claude-code",
            )
        )
        drafts = DraftRepository(connection)
        draft, _version = drafts.create(
            content_item_id=item.id,
            content_type=ContentType.PROMPT,
            title="✨ Заголовок",
            body=POST_BODY,
            category=Category.AI_FOR_WORK,
            audience=AudienceTier.NEWCOMER,
            source_attribution=f"🔗 Джерело: Example\n{SOURCE_URL}",
            source_url=SOURCE_URL,
            created_by="claude-code:content_v2",
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)

        card = render.review_card(review_queue(connection)[0], position=1, total=1)
        assert "Потрібно: завантаження файлу" in card
        assert "Обмеження: у безкоштовному плані ліміти" in card
        assert "(GPT-5.6 Luna)" in card
