"""rendering.caption — Step 5 sections 19-20; redesigned in Step 6C for the
single-post invariant (no more short-caption-plus-followup split)."""

from __future__ import annotations

from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence
from ai_news_editor.publishing.message import telegram_length
from ai_news_editor.publishing.plan import MAX_CAPTION_CHARS
from ai_news_editor.rendering.caption import length_bucket, plan_caption
from ai_news_editor.rendering.content import BodyBlock, EditorialContent
from ai_news_editor.rendering.render import render_editorial_post, render_short_summary


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
    def test_a_short_post_is_not_shortened(self) -> None:
        plan = plan_caption(_content())
        assert plan.shortened is False

    def test_the_caption_equals_the_full_rendered_post(self) -> None:
        content = _content()
        plan = plan_caption(content)
        full = render_editorial_post(content)
        assert plan.caption == full.full_text

    def test_the_caption_always_fits_a_single_telegram_message(self) -> None:
        plan = plan_caption(_content())
        assert telegram_length(plan.caption) <= MAX_CAPTION_CHARS


class TestLongPostIsShortenedNotSplit:
    def _long_content(self) -> EditorialContent:
        """Long enough that the full post doesn't fit a single caption, but short
        enough that the short-summary escalation step (headline + one highlight +
        source) does — exercises the "shortened but not hard-truncated" path."""
        long_text = "Дуже детальний абзац з важливими фактами про подію. " * 10
        return _content(
            body=(
                BodyBlock(purpose="what_happened", text=long_text),
                BodyBlock(purpose="why_it_matters", text=long_text),
            )
        )

    def test_a_long_post_is_shortened(self) -> None:
        content = self._long_content()
        plan = plan_caption(content)
        assert plan.shortened is True

    def test_the_shortened_caption_still_fits_a_single_message(self) -> None:
        content = self._long_content()
        plan = plan_caption(content)
        assert telegram_length(plan.caption) <= MAX_CAPTION_CHARS

    def test_the_shortened_caption_equals_the_deterministic_short_summary(self) -> None:
        content = self._long_content()
        plan = plan_caption(content)
        assert plan.caption == render_short_summary(content)

    def test_the_shortened_caption_is_meaningfully_shorter_than_the_full_post(self) -> None:
        content = self._long_content()
        plan = plan_caption(content)
        full = render_editorial_post(content)
        assert len(plan.caption) < len(full.full_text)

    def test_the_shortened_caption_still_carries_headline_and_source(self) -> None:
        content = self._long_content()
        plan = plan_caption(content)
        assert "Google випустила нову функцію" in plan.caption
        assert "[Джерело: Google]" in plan.caption

    def test_no_followup_attribute_exists_any_more(self) -> None:
        """The single-post invariant means there is nothing left to carry a
        follow-up message — CaptionPlan simply has no such field any more."""
        plan = plan_caption(self._long_content())
        assert not hasattr(plan, "followup")
        assert not hasattr(plan, "needs_followup")


class TestHardTruncationLastResort:
    def test_an_extreme_body_still_produces_a_single_fitting_caption(self) -> None:
        """Even a pathological input (a first body block far longer than a caption
        could ever hold) must never break the single-message guarantee — headline and
        source still survive, only the highlight is clipped."""
        huge_text = "Дуже детальний абзац з важливими фактами про подію. " * 60
        content = _content(body=(BodyBlock(purpose="what_happened", text=huge_text),))
        plan = plan_caption(content)
        assert telegram_length(plan.caption) <= MAX_CAPTION_CHARS
        assert plan.shortened is True
        assert "Google випустила нову функцію" in plan.caption
        assert "[Джерело: Google]" in plan.caption
        assert "caption hard-truncated to fit a single message" in plan.warnings


class TestLengthBucket:
    def test_short_bucket(self) -> None:
        assert length_bucket(100) == "short"

    def test_medium_bucket(self) -> None:
        assert length_bucket(500) == "medium"

    def test_long_bucket(self) -> None:
        assert length_bucket(900) == "long"

    def test_a_short_post_is_bucketed(self) -> None:
        plan = plan_caption(_content())
        assert plan.length_bucket in {"short", "medium", "long"}


class TestDeterminism:
    def test_the_same_content_always_produces_the_same_caption_plan(self) -> None:
        content = _content()
        first = plan_caption(content)
        second = plan_caption(content)
        assert first.caption == second.caption
        assert first.shortened == second.shortened

    def test_the_shortened_caption_is_derived_from_the_same_record_not_a_second_summary(
        self,
    ) -> None:
        """The shortened caption's highlight is the content's own first body block —
        never an independently-generated one-liner that could drift from the full
        post."""
        content = self._long_content_for_determinism()
        plan = plan_caption(content)
        assert "Дуже детальний абзац" in plan.caption

    def _long_content_for_determinism(self) -> EditorialContent:
        long_text = "Дуже детальний абзац з важливими фактами про подію. " * 30
        return _content(body=(BodyBlock(purpose="what_happened", text=long_text),))
