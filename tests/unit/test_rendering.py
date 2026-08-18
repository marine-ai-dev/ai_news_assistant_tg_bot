"""rendering.render — Step 5 sections 5-17, 38-43: one renderer per EditorialCategory."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_news_editor.domain.enums import (
    EditorialCategory,
    EditorialEvidence,
    FreeDealKind,
    PromptOrigin,
)
from ai_news_editor.editorial.safety import ResearchClaimFraming
from ai_news_editor.rendering.content import BodyBlock, DigestItem, EditorialContent
from ai_news_editor.rendering.render import RenderError, render_editorial_post
from ai_news_editor.rendering.style import CATEGORY_EMOJI, category_emoji


def _content(**overrides: object) -> EditorialContent:
    data: dict[str, object] = {
        "category": EditorialCategory.NEWS,
        "evidence": EditorialEvidence.PRIMARY_SOURCE,
        "headline": "Google випустила нову функцію для Gemini",
        "body": (
            BodyBlock(purpose="what_happened", text="Google представила нову функцію."),
            BodyBlock(purpose="why_it_matters", text="Це спрощує роботу з документами."),
        ),
        "source_label": "Google",
        "source_url": "https://blog.google/example",
    }
    data.update(overrides)
    return EditorialContent(**data)  # type: ignore[arg-type]


class TestCategoryEmojiIsCentralized:
    @pytest.mark.parametrize("category", list(EditorialCategory))
    def test_every_category_has_exactly_one_canonical_emoji(
        self, category: EditorialCategory
    ) -> None:
        emoji = category_emoji(category)
        assert emoji
        assert emoji == CATEGORY_EMOJI[category]

    @pytest.mark.parametrize("category", list(EditorialCategory))
    def test_the_rendered_headline_starts_with_that_categorys_emoji(
        self, category: EditorialCategory
    ) -> None:
        content = _content(
            category=category,
            evidence=EditorialEvidence.PRIMARY_SOURCE
            if category is not EditorialCategory.AI_LIFEHACK
            else EditorialEvidence.USER_REPORTED,
            free_deal_kind=FreeDealKind.FREE if category is EditorialCategory.FREE_DEAL else None,
            prompt_origin=PromptOrigin.SOURCE_VERBATIM
            if category is EditorialCategory.PROMPT_WORKFLOW
            else None,
            prompt_text="крок 1: ..." if category is EditorialCategory.PROMPT_WORKFLOW else None,
            research_framing=ResearchClaimFraming.PAPER_RESULT
            if category is EditorialCategory.RESEARCH
            else None,
            digest_items=(DigestItem(headline="Щось", summary="Коротко."),)
            if category is EditorialCategory.WEEKLY_DIGEST
            else (),
        )
        rendered = render_editorial_post(content)
        assert rendered.full_text.startswith(f"*{CATEGORY_EMOJI[category]} ")


class TestNewsRenderer:
    def test_headline_is_bold_with_emoji(self) -> None:
        rendered = render_editorial_post(_content())
        assert rendered.full_text.startswith("*🚀 ")

    def test_body_blocks_are_separated_by_a_blank_line(self) -> None:
        rendered = render_editorial_post(_content())
        assert "\n\n" in rendered.full_text

    def test_source_is_a_markdown_hyperlink_not_a_raw_url(self) -> None:
        rendered = render_editorial_post(_content())
        assert "[Джерело: Google](https://blog.google/example)" in rendered.full_text
        # The bare URL must never appear on its own outside the link syntax.
        assert "\nhttps://blog.google/example" not in rendered.full_text

    def test_special_characters_are_escaped_not_left_as_literal_markup(self) -> None:
        content = _content(headline="Google (нарешті!) додала функцію.")
        rendered = render_editorial_post(content)
        assert "\\(нарешті\\!\\)" in rendered.full_text
        assert "функцію\\." in rendered.full_text

    def test_no_html_tags_leak_through(self) -> None:
        content = _content(
            body=(BodyBlock(purpose="what_happened", text="<b>bold</b> спроба інʼєкції"),)
        )
        rendered = render_editorial_post(content)
        assert "<b>" not in rendered.full_text


class TestAiToolRenderer:
    def test_renders_with_the_tool_emoji(self) -> None:
        content = _content(
            category=EditorialCategory.AI_TOOL,
            body=(
                BodyBlock(purpose="what_it_is", text="Інструмент для нотаток."),
                BodyBlock(purpose="who_its_for", text="Для команд."),
            ),
        )
        rendered = render_editorial_post(content)
        assert rendered.full_text.startswith("*🛠 ")


class TestExplainerRenderer:
    def test_renders_with_the_explainer_emoji(self) -> None:
        content = _content(
            category=EditorialCategory.EXPLAINER,
            body=(BodyBlock(purpose="what_it_is", text="MCP — це протокол."),),
        )
        rendered = render_editorial_post(content)
        assert rendered.full_text.startswith("*🧠 ")


class TestFreeDealRenderer:
    @pytest.mark.parametrize(
        ("kind", "label_fragment"),
        [
            (FreeDealKind.FREE, "Безкоштовно"),
            (FreeDealKind.FREE_TIER, "тариф"),
            (FreeDealKind.FREE_TRIAL, "пробний"),
            (FreeDealKind.OPEN_SOURCE, "код"),
            (FreeDealKind.PROMOTION, "акція"),
            (FreeDealKind.DISCOUNT, "Знижка"),
        ],
    )
    def test_each_kind_gets_its_own_distinct_label(
        self, kind: FreeDealKind, label_fragment: str
    ) -> None:
        content = _content(category=EditorialCategory.FREE_DEAL, free_deal_kind=kind)
        rendered = render_editorial_post(content)
        assert label_fragment in rendered.full_text

    def test_a_free_trial_is_never_rendered_as_free_forever(self) -> None:
        content = _content(
            category=EditorialCategory.FREE_DEAL, free_deal_kind=FreeDealKind.FREE_TRIAL
        )
        rendered = render_editorial_post(content)
        assert "назавжди" not in rendered.full_text.lower()
        assert "пробний" in rendered.full_text

    def test_free_tier_does_not_claim_everything_is_free(self) -> None:
        content = _content(
            category=EditorialCategory.FREE_DEAL, free_deal_kind=FreeDealKind.FREE_TIER
        )
        rendered = render_editorial_post(content)
        assert "тариф" in rendered.full_text

    def test_open_source_is_not_rendered_as_a_free_hosted_service(self) -> None:
        content = _content(
            category=EditorialCategory.FREE_DEAL, free_deal_kind=FreeDealKind.OPEN_SOURCE
        )
        rendered = render_editorial_post(content)
        assert "код" in rendered.full_text
        assert "хостинг" not in rendered.full_text.lower()


class TestLifehackRenderer:
    def test_carries_the_lifehack_emoji_and_a_caveat(self) -> None:
        content = _content(
            category=EditorialCategory.AI_LIFEHACK,
            evidence=EditorialEvidence.USER_REPORTED,
            body=(
                BodyBlock(purpose="anecdote", text="Один користувач Hacker News поділився."),
                BodyBlock(
                    purpose="anecdote_result", text="За його словами, це зекономило 2 години."
                ),
            ),
        )
        rendered = render_editorial_post(content)
        assert rendered.full_text.startswith("*💡 ")
        assert "конкретного користувача" in rendered.full_text

    def test_never_states_the_ai_capability_as_a_flat_fact(self) -> None:
        """The exact rule this category exists to enforce: 'user reported X saved 2
        hours' must never render as 'AI saves 2 hours' stated as fact."""
        content = _content(
            category=EditorialCategory.AI_LIFEHACK,
            evidence=EditorialEvidence.USER_REPORTED,
            body=(
                BodyBlock(
                    purpose="anecdote_result",
                    text="За словами автора, це зекономило 2 години.",
                ),
            ),
        )
        rendered = render_editorial_post(content)
        assert "За словами автора" in rendered.full_text
        assert "AI saves 2 hours" not in rendered.full_text

    def test_wrong_evidence_tier_is_rejected(self) -> None:
        content = _content(
            category=EditorialCategory.AI_LIFEHACK, evidence=EditorialEvidence.PRIMARY_SOURCE
        )
        with pytest.raises(RenderError, match="USER_REPORTED"):
            render_editorial_post(content)


class TestPromptWorkflowRenderer:
    def _content_with_origin(self, origin: PromptOrigin) -> EditorialContent:
        return _content(
            category=EditorialCategory.PROMPT_WORKFLOW,
            body=(BodyBlock(purpose="task", text="Підсумувати довгий документ."),),
            prompt_origin=origin,
            prompt_text="Ти — редактор. Стисни текст до 5 речень.",
        )

    def test_verbatim_prompt_is_labeled_original_and_quoted(self) -> None:
        rendered = render_editorial_post(self._content_with_origin(PromptOrigin.SOURCE_VERBATIM))
        assert "Оригінальний промпт" in rendered.full_text
        assert ">Ти" in rendered.full_text

    def test_adapted_prompt_is_visibly_labeled_as_adapted_not_quoted(self) -> None:
        rendered = render_editorial_post(self._content_with_origin(PromptOrigin.SOURCE_ADAPTED))
        assert "Адаптована версія" in rendered.full_text
        assert ">Ти" not in rendered.full_text

    def test_workflow_derived_is_never_called_the_original_prompt(self) -> None:
        rendered = render_editorial_post(self._content_with_origin(PromptOrigin.WORKFLOW_DERIVED))
        assert "Ідея workflow" in rendered.full_text
        assert "Оригінальний промпт" not in rendered.full_text
        assert ">Ти" not in rendered.full_text


class TestResearchRenderer:
    def test_paper_result_is_labeled_as_such(self) -> None:
        content = _content(
            category=EditorialCategory.RESEARCH,
            evidence=EditorialEvidence.RESEARCH_PAPER,
            body=(BodyBlock(purpose="what_was_found", text="Модель показала кращу точність."),),
            research_framing=ResearchClaimFraming.PAPER_RESULT,
        )
        rendered = render_editorial_post(content)
        assert "За даними дослідження" in rendered.full_text

    def test_company_claim_keeps_its_attribution(self) -> None:
        content = _content(
            category=EditorialCategory.RESEARCH,
            evidence=EditorialEvidence.REPUTABLE_SECONDARY,
            body=(BodyBlock(purpose="what_was_found", text="Компанія повідомляє про приріст."),),
            research_framing=ResearchClaimFraming.COMPANY_CLAIM,
        )
        rendered = render_editorial_post(content)
        assert "За заявою компанії" in rendered.full_text
        assert "доведено" not in rendered.full_text.lower()

    def test_independent_verification_framing_requires_it_to_be_true(self) -> None:
        content = _content(
            category=EditorialCategory.RESEARCH,
            research_framing=ResearchClaimFraming.INDEPENDENT_VERIFICATION,
            research_independently_verified=False,
        )
        with pytest.raises(RenderError, match="independent verification"):
            render_editorial_post(content)

    def test_independent_verification_renders_when_actually_verified(self) -> None:
        content = _content(
            category=EditorialCategory.RESEARCH,
            research_framing=ResearchClaimFraming.INDEPENDENT_VERIFICATION,
            research_independently_verified=True,
        )
        rendered = render_editorial_post(content)
        assert "Незалежно підтверджено" in rendered.full_text


class TestWeeklyDigestRenderer:
    def test_renders_numbered_items(self) -> None:
        content = _content(
            category=EditorialCategory.WEEKLY_DIGEST,
            headline="AI-тиждень: 3 речі, які варто знати",
            digest_items=(
                DigestItem(headline="Перше", summary="Коротко про перше."),
                DigestItem(
                    headline="Друге",
                    summary="Коротко про друге.",
                    source_label="OpenAI",
                    source_url="https://openai.com/example",
                ),
                DigestItem(headline="Третє", summary="Коротко про третє."),
            ),
        )
        rendered = render_editorial_post(content)
        assert "1️⃣" in rendered.full_text
        assert "2️⃣" in rendered.full_text
        assert "3️⃣" in rendered.full_text
        assert "[OpenAI]" in rendered.full_text

    def test_requires_at_least_one_item(self) -> None:
        with pytest.raises(ValidationError, match="at least one digest item"):
            _content(category=EditorialCategory.WEEKLY_DIGEST, digest_items=())


class TestDetailBlock:
    def test_detail_bullets_render_when_present(self) -> None:
        content = _content(detail_bullets=("Пункт один", "Пункт два"))
        rendered = render_editorial_post(content)
        assert "🔆 Детальніше" in rendered.full_text
        assert "• Пункт один" in rendered.full_text

    def test_no_detail_header_when_no_bullets(self) -> None:
        rendered = render_editorial_post(_content())
        assert "Детальніше" not in rendered.full_text


class TestSizeWarnings:
    def test_an_ordinary_category_far_over_target_is_flagged(self) -> None:
        long_text = "Дуже довге речення про подію. " * 60
        content = _content(body=(BodyBlock(purpose="what_happened", text=long_text),))
        rendered = render_editorial_post(content)
        assert any("over the" in w for w in rendered.warnings)

    def test_a_wide_category_is_exempt_at_the_same_length(self) -> None:
        long_text = "Дуже довге речення про дослідження. " * 20
        content = _content(
            category=EditorialCategory.RESEARCH,
            evidence=EditorialEvidence.RESEARCH_PAPER,
            body=(BodyBlock(purpose="what_was_found", text=long_text),),
            research_framing=ResearchClaimFraming.PAPER_RESULT,
        )
        rendered = render_editorial_post(content)
        assert rendered.warnings == ()


class TestHypeDetection:
    def test_a_forbidden_hype_phrase_is_flagged(self) -> None:
        content = _content(headline="Це революційний прорив у AI")
        rendered = render_editorial_post(content)
        assert any("hype" in w for w in rendered.warnings)

    def test_ordinary_text_has_no_hype_warning(self) -> None:
        rendered = render_editorial_post(_content())
        assert not any("hype" in w for w in rendered.warnings)
