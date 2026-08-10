"""Post assembly, length policy and link safety."""

from __future__ import annotations

import pytest

from ai_news_editor.domain.enums import PostFormat
from ai_news_editor.writing.format import (
    ALLOWED_TAGS,
    FORMAT_TARGETS,
    HARD_MAX_CHARS,
    HARD_MIN_CHARS,
    UnsafeLinkError,
    check_length,
    disallowed_tags,
    hard_limit_problem,
    render_post,
    source_line,
    validate_url,
)


class TestLinkSafety:
    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "file:///etc/passwd",
            "ftp://example.invalid/x",
        ],
    )
    def test_unsafe_schemes_are_refused(self, url: str) -> None:
        with pytest.raises(UnsafeLinkError):
            validate_url(url)

    @pytest.mark.parametrize("url", ["https://openai.com/index/x", "http://example.invalid/news"])
    def test_http_links_are_allowed(self, url: str) -> None:
        assert validate_url(url) == url

    def test_empty_url_is_refused(self) -> None:
        with pytest.raises(UnsafeLinkError, match="empty"):
            validate_url("   ")

    def test_url_without_a_host_is_refused(self) -> None:
        with pytest.raises(UnsafeLinkError, match="no host"):
            validate_url("https:///path")


class TestMarkupSubset:
    def test_permitted_tags_pass(self) -> None:
        assert disallowed_tags("<b>жирний</b> та <i>курсив</i>") == set()

    @pytest.mark.parametrize("markup", ["<script>x</script>", "<img src=x>", "<div>y</div>"])
    def test_other_tags_are_reported(self, markup: str) -> None:
        assert disallowed_tags(markup)

    def test_the_subset_stays_small(self) -> None:
        """A wide markup surface is a publishing bug waiting to happen."""
        assert len(ALLOWED_TAGS) <= 8


class TestPostAssembly:
    def test_source_line_carries_label_and_url(self) -> None:
        line = source_line("OpenAI", "https://openai.com/index/x")
        assert "OpenAI" in line
        assert "https://openai.com/index/x" in line
        assert line.startswith("🔗")

    def test_source_line_refuses_an_unsafe_url(self) -> None:
        with pytest.raises(UnsafeLinkError):
            source_line("Evil", "javascript:alert(1)")

    def test_rendered_post_contains_every_part(self) -> None:
        text = render_post(
            headline="🆕 Заголовок",
            body="Тіло допису.",
            source_label="OpenAI",
            source_url="https://openai.com/x",
        )
        assert "🆕 Заголовок" in text
        assert "Тіло допису." in text
        assert "https://openai.com/x" in text

    def test_rendering_is_deterministic(self) -> None:
        kwargs = {
            "headline": "H",
            "body": "B",
            "source_label": "S",
            "source_url": "https://x.invalid/a",
        }
        assert render_post(**kwargs) == render_post(**kwargs)  # type: ignore[arg-type]

    def test_ukrainian_typography_survives(self) -> None:
        text = render_post(
            headline="🤯 Це — справді неочікувано",
            body="Комп'ютер «подумав» і відповів. Ось що з цього вийшло: 100% успіху.",
            source_label="Джерело",
            source_url="https://x.invalid/новини",
        )
        for fragment in ("—", "«", "»", "'", "🤯", "новини"):
            assert fragment in text


class TestLengthPolicy:
    @pytest.mark.parametrize("post_format", list(PostFormat))
    def test_every_format_has_a_target(self, post_format: PostFormat) -> None:
        low, high = FORMAT_TARGETS[post_format]
        assert 0 < low < high

    def test_targets_do_not_overlap_backwards(self) -> None:
        quick = FORMAT_TARGETS[PostFormat.QUICK]
        standard = FORMAT_TARGETS[PostFormat.STANDARD]
        deep = FORMAT_TARGETS[PostFormat.DEEP_DIVE]
        assert quick[1] <= standard[0]
        assert standard[1] <= deep[0]

    def test_text_within_target_has_no_note(self) -> None:
        low, _ = FORMAT_TARGETS[PostFormat.STANDARD]
        check = check_length("x" * (low + 50), PostFormat.STANDARD)
        assert check.within_target
        assert check.note is None

    def test_short_text_is_noted_not_rejected(self) -> None:
        check = check_length("x" * 200, PostFormat.STANDARD)
        assert not check.within_target
        assert check.note and "short" in check.note

    def test_long_text_is_noted_not_rejected(self) -> None:
        check = check_length("x" * 2000, PostFormat.STANDARD)
        assert not check.within_target
        assert check.note and "long" in check.note

    def test_a_deep_dive_may_be_long_without_complaint(self) -> None:
        assert check_length("x" * 2000, PostFormat.DEEP_DIVE).within_target


class TestHardLimits:
    def test_a_reasonable_post_passes(self) -> None:
        assert hard_limit_problem("x" * 900) is None

    def test_an_oversized_post_is_refused(self) -> None:
        problem = hard_limit_problem("x" * (HARD_MAX_CHARS + 1))
        assert problem is not None
        assert "nothing is cropped automatically" in problem

    def test_a_tiny_post_is_refused(self) -> None:
        problem = hard_limit_problem("x" * (HARD_MIN_CHARS - 1))
        assert problem is not None
        assert "below the minimum" in problem

    def test_the_hard_cap_leaves_room_under_telegram_limit(self) -> None:
        """Telegram allows 4096; stopping short leaves headroom for the source line."""
        assert HARD_MAX_CHARS < 4096
