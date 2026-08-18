"""Building the Telegram payload from an approved version.

The property under test throughout: whatever encoding happens on the way out, the text
a reader sees is the text a human approved.
"""

from __future__ import annotations

import pytest

from ai_news_editor.domain.enums import AudienceTier, Category
from ai_news_editor.domain.models import DraftVersion
from ai_news_editor.publishing.message import (
    MAX_MESSAGE_CHARS,
    MessageTooLongError,
    build_message,
    displayed_text,
    escape_html,
    telegram_length,
    unescape_html,
    uses_markup,
)
from ai_news_editor.writing.format import (
    _MARKDOWN_V2_SPECIAL,
    escape_markdown_v2,
    render_version,
    unescape_markdown_v2,
)

BODY = (
    "Компанія оновила застосунок: тепер він уміє більше, ніж раніше. Це помітно тим, "
    "хто користується ним щодня — зникає один зайвий крок у роботі, і не треба щоразу "
    "перемикатися між вкладками."
)


def version(
    title: str = "🆕 Застосунок отримав нову функцію",
    body: str = BODY,
    source_url: str | None = "https://alpha.invalid/story",
    source_attribution: str = "🔗 Джерело: Alpha Co\nhttps://alpha.invalid/story",
) -> DraftVersion:
    from uuid import uuid4

    return DraftVersion(
        draft_id=uuid4(),
        version_no=1,
        title=title,
        body=body,
        category=Category.PRODUCT_UPDATE,
        audience=AudienceTier.GENERAL,
        source_attribution=source_attribution,
        source_url=source_url,
        created_by="test",
    )


class TestLength:
    def test_length_is_counted_in_utf16_code_units(self) -> None:
        """Telegram counts an astral emoji as two. Python's len says one."""
        assert telegram_length("🆕") == 2
        assert len("🆕") == 1

    def test_a_plain_ascii_string_counts_the_same_either_way(self) -> None:
        assert telegram_length("hello") == len("hello") == 5

    def test_cyrillic_counts_as_one_each(self) -> None:
        assert telegram_length("Привіт") == 6

    def test_the_limit_matches_the_documented_bot_api_limit(self) -> None:
        """Bot API 10.2: sendMessage text is 1-4096 characters."""
        assert MAX_MESSAGE_CHARS == 4096

    def test_a_post_over_the_limit_is_refused_not_truncated(self) -> None:
        with pytest.raises(MessageTooLongError, match="not truncated or split"):
            build_message(version(body="слово " * 900))

    def test_the_refusal_names_the_measured_length(self) -> None:
        with pytest.raises(MessageTooLongError) as excinfo:
            build_message(version(body="я" * 5000))
        assert "4096" in str(excinfo.value)


class TestMarkupDetection:
    def test_plain_text_is_recognised_as_plain(self) -> None:
        assert uses_markup("звичайний текст") is False

    def test_a_permitted_tag_is_recognised(self) -> None:
        assert uses_markup("трохи <b>жирного</b>") is True

    def test_an_angle_bracket_that_is_not_a_tag_is_not_markup(self) -> None:
        assert uses_markup("5 < 6 та 7 > 2") is False

    def test_an_unknown_tag_does_not_turn_on_html_mode(self) -> None:
        """A stray <div> must not silently make the post HTML — it would be escaped."""
        assert uses_markup("<div>щось</div>") is False


class TestEscaping:
    @pytest.mark.parametrize(
        ("raw", "escaped"),
        [
            ("Q&A", "Q&amp;A"),
            ("5 < 6", "5 &lt; 6"),
            ("7 > 2", "7 &gt; 2"),
            ("AT&T & Co", "AT&amp;T &amp; Co"),
        ],
    )
    def test_bare_characters_are_escaped(self, raw: str, escaped: str) -> None:
        assert escape_html(raw) == escaped

    def test_permitted_tags_survive_unescaped(self) -> None:
        assert escape_html("<b>жирний</b>") == "<b>жирний</b>"

    def test_a_link_keeps_its_attributes(self) -> None:
        text = '<a href="https://a.invalid">тут</a>'
        assert escape_html(text) == text

    def test_escaping_round_trips(self) -> None:
        text = 'Q&A <b>жирний</b> 5 < 6 & <a href="https://a.invalid?x=1&y=2">лінк</a>'
        assert unescape_html(escape_html(text)) == text

    def test_emoji_and_ukrainian_are_untouched(self) -> None:
        text = "🤯 Це — «неочікувано»: комп'ютер відповів"
        assert escape_html(text) == text


class TestPayload:
    def test_a_plain_post_is_sent_without_a_parse_mode(self) -> None:
        """No parse mode means nothing can be misparsed.

        A NEWS post always carries a source and is therefore always rendered with the
        channel's rich HTML style (see TestNewsStyle below) — the case that stays plain
        is editorial-original content, which has no source to attribute and no markup
        of its own.
        """
        message = build_message(
            version(source_url=None, source_attribution="Матеріал каналу")
        )
        assert message.parse_mode is None
        assert message.payload_text == message.approved_text
        assert "parse_mode" not in message.to_payload("@c")

    def test_a_post_with_markup_uses_html(self) -> None:
        """Legacy HTML mode survives for editorial-original content, where a writer's
        own deliberate <b>/<a> is still theirs to use — NEWS posts (with a source) are
        always MarkdownV2 instead; see TestNewsStylePayload."""
        message = build_message(
            version(
                source_url=None, source_attribution="Матеріал каналу",
                body=f"<b>Увага.</b> {BODY}",
            )
        )
        assert message.parse_mode == "HTML"
        assert message.to_payload("@c")["parse_mode"] == "HTML"

    def test_the_payload_carries_the_destination_and_text(self) -> None:
        message = build_message(version())
        payload = message.to_payload("@my_channel")
        assert payload["chat_id"] == "@my_channel"
        assert payload["text"] == message.payload_text

    def test_link_previews_are_disabled_with_the_current_api_field(self) -> None:
        """Bot API 10.2 configures previews through link_preview_options."""
        payload = build_message(version()).to_payload("@c")
        assert payload["link_preview_options"] == {"is_disabled": True}
        assert "disable_web_page_preview" not in payload


class TestContentExactness:
    """The one that matters: Telegram must show what was approved."""

    def test_the_payload_is_the_approved_rendering(self) -> None:
        subject = version()
        message = build_message(subject)
        assert message.approved_text == render_version(subject)

    def test_what_a_reader_sees_recovers_the_unescaped_approved_content(self) -> None:
        """For a NEWS post, render_version's own output is already MarkdownV2-escaped
        (see writing.format.render_post) — displayed_text's job is the other direction:
        undo that escaping, the way a reader's Telegram client would. The two meet
        exactly at unescape_markdown_v2(render_version(subject))."""
        subject = version()
        assert displayed_text(build_message(subject)) == unescape_markdown_v2(
            render_version(subject)
        )

    def test_what_a_reader_sees_equals_what_was_approved_with_markup(self) -> None:
        subject = version(
            source_url=None, source_attribution="Матеріал каналу",
            body=f"<b>Увага.</b> Q&A. {BODY}",
        )
        assert displayed_text(build_message(subject)) == render_version(subject)

    def test_the_headline_body_and_source_line_are_all_present(self) -> None:
        subject = version()
        text = build_message(subject).approved_text
        assert subject.title in text
        assert escape_markdown_v2(subject.body) in text
        assert "https://alpha.invalid/story" in text
        assert "Alpha Co" in text

    def test_nothing_is_reordered_or_reworded(self) -> None:
        subject = version()
        text = build_message(subject).approved_text
        assert (
            text.index(subject.title)
            < text.index(escape_markdown_v2(subject.body))
            < text.index("Джерело")
        )

    def test_the_emoji_in_the_headline_is_not_changed(self) -> None:
        """The renderer prepends its own deterministic emoji, but a writer's own
        leading emoji in the title itself is still carried through unmodified."""
        subject = version(title="🤖 Заголовок з емодзі")
        assert subject.title in build_message(subject).approved_text


class TestUnknownTags:
    """A stray tag outside the permitted subset is escaped, not passed through.

    Phase 5 refuses to import a draft containing one, so this should be unreachable —
    but if it ever were, Telegram must receive an inert, visible ``&lt;div&gt;`` rather
    than markup nobody reviewed.
    """

    def test_an_unknown_tag_is_escaped(self) -> None:
        assert escape_html("<div>щось</div>") == "&lt;div&gt;щось&lt;/div&gt;"

    def test_an_unknown_tag_beside_a_permitted_one_is_still_escaped(self) -> None:
        assert escape_html("<b>так</b> <div>ні</div>") == "<b>так</b> &lt;div&gt;ні&lt;/div&gt;"

    def test_a_mixed_buffer_round_trips(self) -> None:
        text = "<b>так</b> <div>ні</div> & 5 < 6"
        assert unescape_html(escape_html(text)) == text

    def test_unescaping_ignores_tags_it_would_never_have_written(self) -> None:
        """escape_html never emits a raw <div>, so unescape treats one as plain text."""
        assert unescape_html("<div>&amp;</div>") == "<div>&</div>"


class TestNewsStylePayload:
    """The full pipeline for a NEWS post: render_version's MarkdownV2 text (already
    escaped by the renderer itself), then build_message — the exact path a real
    automation post takes on its way to Telegram. See publishing.plan / publishing.rich
    for why this is the payload that actually reaches sendMessage, not just what
    build_message computes in isolation — a real screenshot proved the two had drifted
    apart once already (build_message was right; plan.py never called it)."""

    def test_the_final_payload_is_markdownv2_with_a_bold_headline(self) -> None:
        message = build_message(version())
        assert message.parse_mode == "MarkdownV2"
        assert message.payload_text.startswith("*")
        assert "*\n\n" in message.payload_text
        # No HTML markup of any kind — the bug this replaces sent exactly this, raw.
        assert "<b>" not in message.payload_text
        assert "</b>" not in message.payload_text

    def test_paragraphs_stay_blank_line_separated_in_the_payload(self) -> None:
        body = "Перший абзац\n\nДругий абзац"
        message = build_message(version(body=body))
        assert "\n\n" in message.payload_text

    def test_the_source_link_survives_and_no_bare_url_line_appears(self) -> None:
        message = build_message(version())
        assert "[Джерело: Alpha Co](https://alpha.invalid/story)" in message.payload_text
        # The URL appears exactly once — inside the link target, never again as bare text.
        assert message.payload_text.count("https://alpha.invalid/story") == 1
        assert "<a href=" not in message.payload_text

    @pytest.mark.parametrize(
        "raw",
        ["Google (Gemini)", "AI-powered", "foo_bar", "test!", "[example]", "100% успіху."],
    )
    def test_headline_special_characters_are_escaped_and_readable(self, raw: str) -> None:
        message = build_message(version(title=f"{raw} — новина"))
        # The message is well-formed MarkdownV2: escaped, not stripped or rejected.
        assert message.parse_mode == "MarkdownV2"
        # The reader gets back exactly the raw text that was approved.
        assert raw in displayed_text(message)
        # And no unescaped special character from the raw title survives next to our
        # own markup — every one of MarkdownV2's specials in the raw text is escaped.
        for char in raw:
            if char in _MARKDOWN_V2_SPECIAL:
                assert f"\\{char}" in message.payload_text

    def test_body_special_characters_are_escaped(self) -> None:
        message = build_message(version(body="Ціна < 100 та рейтинг > 90%."))
        assert "90%\\." in message.payload_text
        # And the reader still sees exactly the raw approved text back out.
        assert "Ціна < 100 та рейтинг > 90%." in displayed_text(message)

    def test_a_source_url_with_a_closing_paren_is_a_safe_link_target(self) -> None:
        subject = version(
            source_url="https://x.invalid/a(1)",
            source_attribution="🔗 Джерело: X\nhttps://x.invalid/a(1)",
        )
        message = build_message(subject)
        assert "(https://x.invalid/a(1\\))" in message.payload_text

    def test_a_headline_with_parens_does_not_corrupt_the_source_link(self) -> None:
        """'<' and '>' are not MarkdownV2 syntax and need no escaping (Telegram shows
        them literally) — but '(' and ')' are, and a headline using them must not be
        able to prematurely close the link this renderer builds at the end."""
        message = build_message(version(title="<script>bad()</script> заголовок"))
        assert "bad()</script> заголовок" in displayed_text(message)
        assert "[Джерело: Alpha Co](https://alpha.invalid/story)" in message.payload_text
