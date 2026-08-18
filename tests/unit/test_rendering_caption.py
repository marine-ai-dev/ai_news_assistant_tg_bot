"""rendering.caption — Step 5 sections 19-20."""

from __future__ import annotations

from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence
from ai_news_editor.rendering.caption import plan_caption
from ai_news_editor.rendering.content import BodyBlock, EditorialContent
from ai_news_editor.rendering.render import render_editorial_post


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


class TestShortPostFitsAsSingleCaption:
    def test_a_short_post_is_a_single_caption_with_no_followup(self) -> None:
        plan = plan_caption(_content())
        assert plan.mode == "single"
        assert plan.followup is None
        assert plan.needs_followup is False

    def test_the_single_caption_equals_the_full_rendered_post(self) -> None:
        content = _content()
        plan = plan_caption(content)
        full = render_editorial_post(content)
        assert plan.caption == full.full_text


class TestLongPostSplitsWithoutTruncatingFacts:
    def _long_content(self) -> EditorialContent:
        long_text = "Дуже детальний абзац з важливими фактами про подію. " * 30
        return _content(
            body=(
                BodyBlock(purpose="what_happened", text=long_text),
                BodyBlock(purpose="why_it_matters", text=long_text),
            )
        )

    def test_a_long_post_splits_into_short_caption_plus_followup(self) -> None:
        content = self._long_content()
        plan = plan_caption(content)
        assert plan.mode == "split"
        assert plan.needs_followup is True
        assert plan.followup is not None

    def test_the_followup_carries_the_full_unabridged_post(self) -> None:
        content = self._long_content()
        plan = plan_caption(content)
        full = render_editorial_post(content)
        assert plan.followup == full.full_text

    def test_the_short_caption_is_meaningfully_shorter_than_the_full_post(self) -> None:
        content = self._long_content()
        plan = plan_caption(content)
        full = render_editorial_post(content)
        assert len(plan.caption) < len(full.full_text)

    def test_the_short_caption_still_carries_headline_and_source(self) -> None:
        content = self._long_content()
        plan = plan_caption(content)
        assert "Google випустила нову функцію" in plan.caption
        assert "[Джерело: Google]" in plan.caption


class TestDeterminism:
    def test_the_same_content_always_produces_the_same_caption_plan(self) -> None:
        content = _content()
        first = plan_caption(content)
        second = plan_caption(content)
        assert first.caption == second.caption
        assert first.followup == second.followup

    def test_the_short_caption_is_derived_from_the_same_record_not_a_second_summary(
        self,
    ) -> None:
        """The short caption's highlight is the content's own first body block — never
        an independently-generated one-liner that could drift from the full post."""
        content = self._long_content_for_determinism()
        plan = plan_caption(content)
        assert "Дуже детальний абзац" in plan.caption

    def _long_content_for_determinism(self) -> EditorialContent:
        long_text = "Дуже детальний абзац з важливими фактами про подію. " * 30
        return _content(body=(BodyBlock(purpose="what_happened", text=long_text),))
