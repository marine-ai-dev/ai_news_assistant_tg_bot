"""Post assembly, length policy and link safety."""

from __future__ import annotations

import pytest

from ai_news_editor.domain.enums import Category, PostFormat
from ai_news_editor.writing.format import (
    ALLOWED_TAGS,
    FORMAT_TARGETS,
    HARD_MAX_CHARS,
    HARD_MIN_CHARS,
    UnsafeLinkError,
    check_length,
    disallowed_tags,
    escape_markdown_v2,
    hard_limit_problem,
    has_any_markup,
    render_post,
    source_line,
    unescape_markdown_v2,
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
            body="Тіло допису",
            source_label="OpenAI",
            source_url="https://openai.com/x",
        )
        assert "🆕 Заголовок" in text
        assert "Тіло допису" in text
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


class TestNewsStyleRendering:
    """The channel's NEWS visual style: bold emoji headline, emoji-led paragraphs,
    hidden source hyperlink — applied whenever a post has a source to attribute."""

    def test_the_headline_is_bold_and_emoji_led(self) -> None:
        text = render_post(
            headline="Новина дня", body="Один абзац тексту",
            source_label="Google", source_url="https://x.invalid/a",
            category=Category.PRODUCT_UPDATE,
        )
        assert text.startswith("*🚀 Новина дня*")

    def test_an_unknown_category_falls_back_to_the_default_emoji(self) -> None:
        text = render_post(
            headline="Новина", body="Текст",
            source_label="X", source_url="https://x.invalid/a", category=None,
        )
        assert text.startswith("*🧩 Новина*")

    @pytest.mark.parametrize(
        "category,emoji",
        [
            (Category.USEFUL_TOOL, "🛠"),
            (Category.CREATIVE_AI, "🎨"),
            (Category.AI_FOR_LEARNING, "🧠"),
        ],
    )
    def test_headline_emoji_is_deterministic_per_category(
        self, category: Category, emoji: str
    ) -> None:
        text = render_post(
            headline="Х", body="Текст", source_label="X",
            source_url="https://x.invalid/a", category=category,
        )
        assert text.startswith(f"*{emoji} Х*")

    def test_paragraphs_are_split_only_on_existing_blank_lines(self) -> None:
        body = "Перший абзац\n\nДругий абзац\n\nТретій абзац"
        text = render_post(
            headline="Н", body=body, source_label="X", source_url="https://x.invalid/a",
        )
        assert "✨ Перший абзац" in text
        assert "🛠 Другий абзац" in text
        assert "🔍 Третій абзац" in text
        # Blank-line-separated: every paragraph line is its own block.
        assert "\n\n✨ Перший абзац\n\n🛠 Другий абзац\n\n🔍 Третій абзац" in text

    def test_a_single_paragraph_body_is_not_force_split(self) -> None:
        text = render_post(
            headline="Н", body="Одне суцільне речення без порожніх рядків",
            source_label="X", source_url="https://x.invalid/a",
        )
        assert "✨ Одне суцільне речення без порожніх рядків" in text
        assert text.count("🛠") == 0

    def test_paragraph_emoji_rotates_and_wraps(self) -> None:
        body = "\n\n".join(f"Абзац {i}" for i in range(1, 8))  # 7 paragraphs, 6 emoji
        text = render_post(
            headline="Н", body=body, source_label="X", source_url="https://x.invalid/a",
        )
        assert "✨ Абзац 1" in text
        assert "✨ Абзац 7" in text  # wraps back to the first emoji

    def test_the_source_is_a_hidden_hyperlink_not_a_bare_url(self) -> None:
        text = render_post(
            headline="Н", body="Т", source_label="Google",
            source_url="https://blog.google/x",
        )
        assert "🔗 [Джерело: Google](https://blog.google/x)" in text
        # No standalone occurrence of the raw URL outside the link target.
        assert text.count("https://blog.google/x") == 1

    def test_editorial_content_without_a_source_keeps_the_plain_style(self) -> None:
        text = render_post(headline="Заголовок", body="Тіло.")
        assert text == "Заголовок\n\nТіло."
        assert "<b>" not in text
        assert "Джерело" not in text


class TestMarkdownV2Escaping:
    """escape_markdown_v2 / unescape_markdown_v2 — the pair that stands between
    Gemini-or-writer text and a message Telegram either refuses to parse or renders as
    something nobody wrote."""

    @pytest.mark.parametrize("char", list("_*[]()~`>#+-=|{}.!\\"))
    def test_every_special_character_is_escaped(self, char: str) -> None:
        assert escape_markdown_v2(char) == f"\\{char}"

    def test_ordinary_characters_are_untouched(self) -> None:
        assert escape_markdown_v2("Привіт світ 123") == "Привіт світ 123"

    @pytest.mark.parametrize(
        "raw",
        [
            "Google (Gemini)",
            "AI-powered",
            "foo_bar",
            "test!",
            "[example]",
            "100% успіху.",
            "діапазон 1-10",
            "прайс: $5 | безкоштовно",
        ],
    )
    def test_escaping_round_trips_exactly(self, raw: str) -> None:
        assert unescape_markdown_v2(escape_markdown_v2(raw)) == raw

    @pytest.mark.parametrize(
        "raw",
        ["Google (Gemini)", "AI-powered", "foo_bar", "test!", "[example]", "100% успіху."],
    )
    def test_a_rendered_post_stays_well_formed_with_dangerous_headline_text(
        self, raw: str
    ) -> None:
        text = render_post(
            headline=f"{raw} — новина", body="Т", source_label="X",
            source_url="https://x.invalid/a",
        )
        # Every escaped special character from the raw headline appears with its
        # backslash — nothing was silently stripped to make the message parseable.
        for char in raw:
            if char in "_*[]()~`>#+-=|{}.!\\":
                assert f"\\{char}" in text
        # And the source link this renderer always appends is still intact and last.
        assert text.rstrip().endswith("[Джерело: X](https://x.invalid/a)")

    def test_url_escaping_only_touches_backslash_and_close_paren(self) -> None:
        text = render_post(
            headline="Н", body="Т", source_label="X",
            source_url="https://x.invalid/a(b)c",
        )
        assert "(https://x.invalid/a(b\\)c)" in text


class TestAnyMarkupDetection:
    def test_plain_text_has_no_markup(self) -> None:
        assert has_any_markup("Просто текст без тегів.") is False

    @pytest.mark.parametrize(
        "text",
        ["<b>жирний</b>", "звичайний <i>курсив</i>", '<a href="https://x.invalid">лінк</a>'],
    )
    def test_even_permitted_tags_count_as_markup(self, text: str) -> None:
        """Stricter than disallowed_tags on purpose — see has_any_markup's docstring:
        automation may not use any markup, not just the tags humans may not use."""
        assert has_any_markup(text) is True

    def test_a_bare_comparison_is_not_mistaken_for_a_tag(self) -> None:
        assert has_any_markup("n < 5 та m > 3") is False


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
