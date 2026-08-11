"""The content model: types, audiences, origins and the structures behind each format."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_news_editor.content.jargon import scan, unexplained
from ai_news_editor.domain.enums import (
    AUDIENCE_ORDER,
    NON_TECHNICAL_AUDIENCES,
    AudienceTier,
    ContentOrigin,
    ContentType,
    EvidenceStatus,
    PromptTopic,
)
from ai_news_editor.domain.models import (
    ContentItem,
    ContentReference,
    Draft,
    ExplainerBody,
    PromptBody,
)

PROMPT_TEXT = (
    "Я надішлю список продуктів, які є вдома. Запропонуй три прості страви, які з них "
    "можна приготувати, і скажи, чого не вистачає."
)


def prompt_body(**overrides: object) -> PromptBody:
    data: dict[str, object] = {
        "what_you_can_do": "швидко вирішити, що приготувати",
        "prompt_text": PROMPT_TEXT,
        "customization_tips": ("вкажіть, скільки часу у вас є",),
    }
    data.update(overrides)
    return PromptBody(**data)  # type: ignore[arg-type]


def explainer_body(**overrides: object) -> ExplainerBody:
    data: dict[str, object] = {
        "concept": "Промпт",
        "simple_explanation": "Промпт — це те, що ви пишете ШІ.",
        "real_life_example": "Як записка колезі: чим точніше, тим кращий результат.",
        "why_it_matters": "Від формулювання залежить, наскільки корисною буде відповідь.",
    }
    data.update(overrides)
    return ExplainerBody(**data)  # type: ignore[arg-type]


class TestAudienceScale:
    def test_newcomer_exists_and_is_the_least_assuming_level(self) -> None:
        assert AUDIENCE_ORDER[0] is AudienceTier.NEWCOMER

    def test_the_existing_levels_are_unchanged(self) -> None:
        """Adding a level must not rename or renumber the ones already in use."""
        for name in ("BEGINNER", "GENERAL", "TECH_CURIOUS"):
            assert AudienceTier(name).value == name

    def test_the_scale_is_ordered_lowest_first(self) -> None:
        assert [a.value for a in AUDIENCE_ORDER] == [
            "NEWCOMER",
            "BEGINNER",
            "GENERAL",
            "TECH_CURIOUS",
        ]

    def test_the_non_technical_audiences_are_the_bottom_two(self) -> None:
        assert {AudienceTier.NEWCOMER, AudienceTier.BEGINNER} == NON_TECHNICAL_AUDIENCES

    def test_an_unknown_audience_is_refused(self) -> None:
        with pytest.raises(ValueError):
            AudienceTier("EXPERT")


class TestContentTypes:
    @pytest.mark.parametrize("name", ["NEWS", "PROMPT", "EXPLAINER"])
    def test_the_three_types_exist(self, name: str) -> None:
        assert ContentType(name).value == name

    def test_an_unknown_type_is_refused(self) -> None:
        with pytest.raises(ValueError):
            ContentType("HOW_TO")

    @pytest.mark.parametrize("name", ["TESTED_USE_CASE", "RESOURCE"])
    def test_the_phase_82_types_exist(self, name: str) -> None:
        assert ContentType(name).value == name

    def test_the_vocabulary_stays_small(self) -> None:
        """Five formats the channel actually publishes. Adding one is an editorial
        decision, not a convenience."""
        assert len(list(ContentType)) == 5


class TestPromptStructure:
    def test_a_complete_prompt_is_accepted(self) -> None:
        assert prompt_body().prompt_text == PROMPT_TEXT

    def test_an_empty_prompt_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            prompt_body(prompt_text="")

    def test_a_trivially_short_prompt_is_refused(self) -> None:
        """'Напиши щось' is not a prompt anyone can use."""
        with pytest.raises(ValidationError, match="too short"):
            prompt_body(prompt_text="Напиши щось")

    def test_customization_guidance_is_required(self) -> None:
        """Without it a prompt is an incantation rather than a tool."""
        with pytest.raises(ValidationError):
            prompt_body(customization_tips=())

    def test_compatibility_is_optional_and_defaults_to_unclaimed(self) -> None:
        """Silence beats claiming a prompt works with tools nobody tested."""
        assert prompt_body().works_with is None


class TestExplainerStructure:
    def test_a_complete_explainer_is_accepted(self) -> None:
        assert explainer_body().concept == "Промпт"

    @pytest.mark.parametrize(
        "missing", ["concept", "simple_explanation", "real_life_example", "why_it_matters"]
    )
    def test_every_required_part_is_required(self, missing: str) -> None:
        with pytest.raises(ValidationError):
            explainer_body(**{missing: ""})

    def test_a_closing_suggestion_is_optional(self) -> None:
        assert explainer_body().try_this is None
        assert explainer_body(try_this="Спробуйте попросити пояснити простіше").try_this


class TestContentItem:
    def test_a_prompt_item_needs_a_topic(self) -> None:
        with pytest.raises(ValidationError, match="needs a topic"):
            ContentItem(
                content_type=ContentType.PROMPT,
                audience=AudienceTier.NEWCOMER,
                title="t",
                body=prompt_body(),
                created_by="claude-code",
            )

    def test_an_explainer_is_described_by_its_concept_not_a_topic(self) -> None:
        with pytest.raises(ValidationError, match="concept"):
            ContentItem(
                content_type=ContentType.EXPLAINER,
                audience=AudienceTier.NEWCOMER,
                title="t",
                topic=PromptTopic.LEARNING,
                body=explainer_body(),
                created_by="claude-code",
            )

    def test_the_body_must_match_the_type(self) -> None:
        with pytest.raises(ValidationError, match="prompt body"):
            ContentItem(
                content_type=ContentType.PROMPT,
                audience=AudienceTier.NEWCOMER,
                title="t",
                topic=PromptTopic.FOOD,
                body=explainer_body(),
                created_by="claude-code",
            )

    def test_news_cannot_be_a_content_item(self) -> None:
        """News comes from an article. A content item for it would be a fake source."""
        with pytest.raises(ValidationError, match="sourced from an article"):
            ContentItem(
                content_type=ContentType.NEWS,
                audience=AudienceTier.GENERAL,
                title="t",
                body=prompt_body(),
                created_by="claude-code",
            )

    def test_the_origin_is_editorial_original_by_default(self) -> None:
        item = ContentItem(
            content_type=ContentType.EXPLAINER,
            audience=AudienceTier.NEWCOMER,
            title="Що таке промпт",
            body=explainer_body(),
            created_by="claude-code",
        )
        assert item.origin is ContentOrigin.EDITORIAL_ORIGINAL

    def test_references_are_optional_and_say_what_they_support(self) -> None:
        item = ContentItem(
            content_type=ContentType.EXPLAINER,
            audience=AudienceTier.NEWCOMER,
            title="Що таке промпт",
            body=explainer_body(),
            created_by="claude-code",
            references=(
                ContentReference(
                    label="OpenAI Help",
                    url="https://help.openai.com/x",
                    supports="що безкоштовний план має ліміти",
                ),
            ),
        )
        assert item.references[0].supports

    def test_the_subject_is_the_topic_for_a_prompt(self) -> None:
        item = ContentItem(
            content_type=ContentType.PROMPT,
            audience=AudienceTier.NEWCOMER,
            title="t",
            topic=PromptTopic.WORK,
            body=prompt_body(),
            evidence_status=EvidenceStatus.LEGACY_UNVERIFIED,
            created_by="claude-code",
        )
        assert item.subject == "WORK"

    def test_the_subject_is_the_concept_for_an_explainer(self) -> None:
        item = ContentItem(
            content_type=ContentType.EXPLAINER,
            audience=AudienceTier.NEWCOMER,
            title="t",
            body=explainer_body(),
            created_by="claude-code",
        )
        assert item.subject == "Промпт"


class TestDraftOrigin:
    """A draft has exactly one origin, and it matches its content type."""

    def test_a_news_draft_needs_an_article(self) -> None:
        with pytest.raises(ValidationError, match="needs the article"):
            Draft(content_type=ContentType.NEWS)

    def test_a_prompt_draft_needs_a_content_item(self) -> None:
        with pytest.raises(ValidationError, match="needs the content item"):
            Draft(content_type=ContentType.PROMPT)

    def test_a_prompt_draft_may_not_borrow_an_article(self) -> None:
        """The rule that stops original content from inventing provenance."""
        with pytest.raises(ValidationError, match="invent provenance"):
            Draft(
                content_type=ContentType.PROMPT,
                content_item_id=uuid4(),
                article_id=uuid4(),
            )

    def test_a_news_draft_may_not_carry_a_content_item(self) -> None:
        with pytest.raises(ValidationError, match="not a content item"):
            Draft(
                content_type=ContentType.NEWS,
                article_id=uuid4(),
                content_item_id=uuid4(),
            )

    def test_editorial_original_content_carries_no_evaluation(self) -> None:
        with pytest.raises(ValidationError, match="no article to evaluate"):
            Draft(
                content_type=ContentType.EXPLAINER,
                content_item_id=uuid4(),
                evaluation_id=uuid4(),
            )

    def test_a_draft_defaults_to_news(self) -> None:
        """So every row written before Phase 7.5 means what it always meant."""
        assert Draft(article_id=uuid4()).content_type is ContentType.NEWS

    def test_origin_is_derived_not_stored_twice(self) -> None:
        assert Draft(article_id=uuid4()).origin is ContentOrigin.SOURCED_ARTICLE
        assert (
            Draft(content_type=ContentType.PROMPT, content_item_id=uuid4()).origin
            is ContentOrigin.EDITORIAL_ORIGINAL
        )


class TestJargonWarnings:
    """A reading aid. It flags for attention; it never rejects."""

    def test_an_unexplained_product_name_is_flagged(self) -> None:
        assert [n.term for n in unexplained("Notion додав нову функцію.")] == ["Notion"]

    def test_an_explained_product_name_is_not_flagged(self) -> None:
        """The counter-example that a keyword blocklist would get wrong."""
        text = (
            "Notion — сервіс для нотаток і робочих документів — додав функцію, яка "
            "запускає завдання після зустрічі."
        )
        assert unexplained(text) == []

    def test_ukrainian_inflection_is_matched(self) -> None:
        assert [n.term for n in unexplained("Тригер для AI-агента.")] == ["AI-агент"]

    def test_plain_writing_raises_nothing(self) -> None:
        assert unexplained("ШІ тепер уміє більше, ніж раніше.") == []

    def test_a_term_defined_with_a_dash_counts_as_explained(self) -> None:
        assert unexplained("Промпт — це те, що ви пишете ШІ.") == []

    def test_a_term_defined_in_parentheses_counts_as_explained(self) -> None:
        assert unexplained("Використайте API (спосіб з'єднати дві програми).") == []

    def test_each_term_is_reported_once_not_per_occurrence(self) -> None:
        notes = scan("Slack, Slack і ще раз Slack.")
        assert [n.term for n in notes] == ["Slack"]

    def test_the_note_carries_an_excerpt_for_the_reviewer(self) -> None:
        note = unexplained("Notion додав нову функцію.")[0]
        assert "Notion" in note.excerpt
        assert "without an explanation" in note.message


class TestJargonNoteMessages:
    def test_an_explained_term_says_so(self) -> None:
        note = scan("Промпт — це те, що ви пишете ШІ.")[0]
        assert note.explained is True
        assert "looks explained" in note.message


class TestExplanationAnywhere:
    """A label in the headline explained in the first line is a good post, not a flag."""

    def test_a_term_explained_later_in_the_post_is_not_flagged(self) -> None:
        text = (
            "✨ Промпт: що приготувати з того, що вже є вдома\n"
            "Знайоме відчуття: холодильник не порожній, а ідей немає. Це якраз те, що "
            "можна перекласти на ШІ, і зайняти це має хвилину, не більше.\n"
            "Нижче — готовий промпт. Промпт — це просто текст, який ви пишете ШІ."
        )
        assert unexplained(text) == []

    def test_a_term_never_explained_is_still_flagged(self) -> None:
        text = (
            "✨ Промпт: що приготувати\n"
            "Скопіюйте цей промпт у чат. Далі впишіть свій список продуктів і чекайте "
            "на відповідь, вона зазвичай приходить за кілька секунд."
        )
        assert [n.term for n in unexplained(text)] == ["промпт"]
