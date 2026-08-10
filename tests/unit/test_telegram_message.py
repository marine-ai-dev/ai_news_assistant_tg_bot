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
from ai_news_editor.writing.format import render_version

BODY = (
    "Компанія оновила застосунок: тепер він уміє більше, ніж раніше. Це помітно тим, "
    "хто користується ним щодня — зникає один зайвий крок у роботі, і не треба щоразу "
    "перемикатися між вкладками."
)


def version(title: str = "🆕 Застосунок отримав нову функцію", body: str = BODY) -> DraftVersion:
    from uuid import uuid4

    return DraftVersion(
        draft_id=uuid4(),
        version_no=1,
        title=title,
        body=body,
        category=Category.PRODUCT_UPDATE,
        audience=AudienceTier.GENERAL,
        source_attribution="🔗 Джерело: Alpha Co\nhttps://alpha.invalid/story",
        source_url="https://alpha.invalid/story",
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
        """No parse mode means nothing can be misparsed. The normal case."""
        message = build_message(version())
        assert message.parse_mode is None
        assert message.payload_text == message.approved_text
        assert "parse_mode" not in message.to_payload("@c")

    def test_a_post_with_markup_uses_html(self) -> None:
        message = build_message(version(body=f"<b>Увага.</b> {BODY}"))
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

    def test_what_a_reader_sees_equals_what_was_approved_plain(self) -> None:
        subject = version()
        assert displayed_text(build_message(subject)) == render_version(subject)

    def test_what_a_reader_sees_equals_what_was_approved_with_markup(self) -> None:
        subject = version(body=f"<b>Увага.</b> Q&A. {BODY}")
        assert displayed_text(build_message(subject)) == render_version(subject)

    def test_the_headline_body_and_source_line_are_all_present(self) -> None:
        subject = version()
        text = build_message(subject).approved_text
        assert subject.title in text
        assert subject.body in text
        assert "https://alpha.invalid/story" in text
        assert "Alpha Co" in text

    def test_nothing_is_reordered_or_reworded(self) -> None:
        subject = version()
        text = build_message(subject).approved_text
        assert text.index(subject.title) < text.index(subject.body) < text.index("🔗 Джерело")

    def test_the_emoji_in_the_headline_is_not_changed(self) -> None:
        subject = version(title="🤖 Заголовок з емодзі")
        assert build_message(subject).approved_text.startswith("🤖 Заголовок з емодзі")


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
