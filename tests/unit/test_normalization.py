"""Text normalization, fingerprinting, and RawItem → Article derivation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_news_editor.domain.enums import ArticleStatus
from ai_news_editor.pipeline.fingerprint import (
    DEFAULT_HAMMING_THRESHOLD,
    MIN_TOKENS_FOR_SIMHASH,
    content_fingerprint,
    features,
    hamming_distance,
    is_near_duplicate,
    simhash,
    title_fingerprint,
)
from ai_news_editor.pipeline.normalize import NormalizationRejected, normalize
from ai_news_editor.pipeline.text import (
    MAX_TEXT_CHARS,
    TRUNCATION_MARKER,
    clean_text,
    clean_title,
    html_to_text,
    normalize_unicode,
    normalize_whitespace,
    tokenize,
    truncate,
)
from ai_news_editor.storage.codecs import simhash_from_storage, simhash_to_storage
from tests.conftest import make_raw_item


class TestHtmlToText:
    def test_tags_are_stripped(self) -> None:
        assert "Hello world" in html_to_text("<p>Hello <b>world</b></p>")

    def test_entities_are_decoded(self) -> None:
        assert "AT&T" in html_to_text("<p>AT&amp;T</p>")
        assert "<new>" in html_to_text("<p>&lt;new&gt;</p>")

    def test_scripts_and_styles_are_discarded(self) -> None:
        """Markup plumbing must never end up stored as article text."""
        text = html_to_text(
            "<div><script>var secret = 1;</script><style>.a{color:red}</style>"
            "<p>Real content</p></div>"
        )
        assert "Real content" in text
        assert "secret" not in text
        assert "color:red" not in text

    def test_paragraph_boundaries_survive(self) -> None:
        text = normalize_whitespace(html_to_text("<p>First para</p><p>Second para</p>"))
        assert "First para" in text
        assert "Second para" in text
        assert "\n" in text

    def test_non_content_containers_are_dropped(self) -> None:
        text = html_to_text("<nav>Home About</nav><p>Story text</p>")
        assert "Story text" in text
        assert "Home About" not in text

    def test_empty_html_is_empty(self) -> None:
        assert html_to_text("") == ""


class TestWhitespaceAndUnicode:
    def test_runs_of_spaces_collapse(self) -> None:
        assert normalize_whitespace("a     b\t\tc") == "a b c"

    def test_excess_blank_lines_collapse_to_one(self) -> None:
        assert normalize_whitespace("a\n\n\n\n\nb") == "a\n\nb"

    def test_crlf_is_normalised(self) -> None:
        assert normalize_whitespace("a\r\nb") == "a\nb"

    def test_nfc_normalization_makes_equivalents_equal(self) -> None:
        composed = "é"
        decomposed = "é"
        assert normalize_unicode(composed) == normalize_unicode(decomposed)

    def test_zero_width_characters_are_removed(self) -> None:
        assert normalize_unicode("ai​news") == "ainews"

    def test_ukrainian_text_is_preserved(self) -> None:
        text = "Штучний інтелект — це цікаво"
        assert normalize_unicode(text) == text

    def test_emoji_are_preserved(self) -> None:
        assert "🤖" in normalize_whitespace("robots 🤖  everywhere")


class TestCleanTextAndTitle:
    def test_absent_stays_absent(self) -> None:
        assert clean_text(None) is None
        assert clean_text("   ") is None
        assert clean_title(None) is None

    def test_html_input_is_flattened(self) -> None:
        assert clean_text("<p>Hello &amp; welcome</p>") == "Hello & welcome"

    def test_title_newlines_are_flattened(self) -> None:
        assert clean_title("<h1>Two\nlines</h1>") == "Two lines"

    def test_long_text_is_truncated_visibly(self) -> None:
        """Truncation is a defined boundary behaviour, never a silent one."""
        result = clean_text("x" * (MAX_TEXT_CHARS + 500))
        assert result is not None
        assert len(result) <= MAX_TEXT_CHARS
        assert result.endswith(TRUNCATION_MARKER)

    def test_short_text_is_untouched(self) -> None:
        assert truncate("short", 100) == "short"


class TestTokenize:
    def test_lowercases_and_splits_on_punctuation(self) -> None:
        assert tokenize("Hello, World!") == ["hello", "world"]

    def test_handles_cyrillic(self) -> None:
        assert tokenize("Штучний інтелект") == ["штучний", "інтелект"]

    def test_empty_input(self) -> None:
        assert tokenize("") == []


class TestFingerprints:
    def test_content_fingerprint_is_stable(self) -> None:
        assert content_fingerprint("T", "B") == content_fingerprint("T", "B")

    def test_punctuation_and_case_do_not_change_the_fingerprint(self) -> None:
        assert content_fingerprint("Hello World", "A b") == content_fingerprint(
            "hello, world!", "a  B"
        )

    def test_different_content_differs(self) -> None:
        assert content_fingerprint("A", "x") != content_fingerprint("B", "x")

    def test_title_fingerprint_absent_for_empty_title(self) -> None:
        assert title_fingerprint("") is None
        assert title_fingerprint(None) is None

    def test_title_fingerprint_ignores_punctuation(self) -> None:
        assert title_fingerprint("Claude Opus 5!") == title_fingerprint("claude opus 5")


class TestSimhash:
    LONG = (
        "OpenAI launches a new ChatGPT feature that lets ordinary users build custom "
        "agents without writing any code at all"
    )

    def test_is_deterministic(self) -> None:
        assert simhash(self.LONG) == simhash(self.LONG)

    def test_short_text_is_refused(self) -> None:
        """Short texts collide easily, so they are never near-duplicate matched."""
        assert simhash("only a few words here") is None

    def test_threshold_for_length(self) -> None:
        tokens = " ".join(f"word{i}" for i in range(MIN_TOKENS_FOR_SIMHASH))
        assert simhash(tokens) is not None

    def test_features_include_words_and_pairs(self) -> None:
        assert features(["a", "b", "c"]) == ["a", "b", "c", "a b", "b c"]

    def test_identical_text_has_distance_zero(self) -> None:
        assert hamming_distance(simhash(self.LONG), simhash(self.LONG)) == 0  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "variant",
        [
            LONG + "!",
            LONG.replace("that", "which"),
            LONG.replace(",", "").upper(),
            "  ".join(LONG.split()),
        ],
    )
    def test_trivial_variations_stay_near(self, variant: str) -> None:
        distance = hamming_distance(simhash(self.LONG), simhash(variant))  # type: ignore[arg-type]
        assert distance <= DEFAULT_HAMMING_THRESHOLD

    @pytest.mark.parametrize(
        "other",
        [
            "Researchers describe a novel CUDA kernel optimisation for sparse matrix "
            "multiplication on modern GPUs",
            "Anthropic launches a new Claude feature that lets developers deploy custom "
            "tools through an API endpoint",
            "Midjourney releases version 8 with much better photorealistic human faces "
            "and correctly rendered hands",
        ],
    )
    def test_different_stories_stay_far_apart(self, other: str) -> None:
        """The safeguard that matters: a false merge silently deletes a story."""
        distance = hamming_distance(simhash(self.LONG), simhash(other))  # type: ignore[arg-type]
        assert distance > DEFAULT_HAMMING_THRESHOLD

    def test_word_order_matters(self) -> None:
        left = simhash("the dog bites the man in a story about animals and city life today")
        right = simhash("the man bites the dog in a story about animals and city life today")
        assert hamming_distance(left, right) > 0  # type: ignore[arg-type]

    def test_is_near_duplicate_helper(self) -> None:
        value = simhash(self.LONG)
        assert is_near_duplicate(value, value)  # type: ignore[arg-type]


class TestSimhashStorageCodec:
    """A 64-bit unsigned simhash has to survive SQLite's signed INTEGER."""

    @pytest.mark.parametrize("value", [0, 1, (1 << 62), (1 << 63), (1 << 64) - 1, 0xFFFF_0000_FFFF])
    def test_round_trip(self, value: int) -> None:
        assert simhash_from_storage(simhash_to_storage(value)) == value

    def test_high_bit_values_fit_in_signed_range(self) -> None:
        stored = simhash_to_storage((1 << 64) - 1)
        assert stored is not None
        assert -(1 << 63) <= stored < (1 << 63)

    def test_none_passes_through(self) -> None:
        assert simhash_to_storage(None) is None
        assert simhash_from_storage(None) is None

    def test_distance_is_unaffected_by_the_round_trip(self) -> None:
        a, b = (1 << 64) - 1, (1 << 64) - 3
        assert hamming_distance(
            simhash_from_storage(simhash_to_storage(a)),  # type: ignore[arg-type]
            simhash_from_storage(simhash_to_storage(b)),  # type: ignore[arg-type]
        ) == hamming_distance(a, b)


class TestNormalizeRawItem:
    def test_derives_an_article(self) -> None:
        item = make_raw_item(
            title_original="  <b>Claude Opus 5</b> is here  ",
            url_original="https://example.invalid/news/opus?utm_source=rss",
            summary_raw="<p>A summary &amp; more.</p>",
        )
        result = normalize(item)
        assert not isinstance(result, NormalizationRejected)
        assert result.title == "Claude Opus 5 is here"
        assert result.canonical_url == "https://example.invalid/news/opus"
        assert result.clean_text == "A summary & more."
        assert result.status is ArticleStatus.COLLECTED
        assert result.normalized_at is not None

    def test_raw_item_is_not_mutated(self) -> None:
        """RawItem is provenance; normalization derives, never rewrites."""
        item = make_raw_item(title_original="  Messy   title  ")
        normalize(item)
        assert item.title_original == "  Messy   title  "

    def test_links_back_to_its_raw_item(self) -> None:
        item = make_raw_item()
        result = normalize(item)
        assert not isinstance(result, NormalizationRejected)
        assert result.raw_item_id == item.id
        assert result.source_id == item.source_id

    def test_content_is_preferred_over_summary(self) -> None:
        item = make_raw_item(content_raw="<p>Full body</p>", summary_raw="Short summary")
        result = normalize(item)
        assert not isinstance(result, NormalizationRejected)
        assert result.clean_text == "Full body"

    def test_falls_back_to_summary(self) -> None:
        item = make_raw_item(content_raw=None, summary_raw="Only a summary")
        result = normalize(item)
        assert not isinstance(result, NormalizationRejected)
        assert result.clean_text == "Only a summary"

    def test_missing_publication_time_is_not_invented(self) -> None:
        result = normalize(make_raw_item(published_at=None))
        assert not isinstance(result, NormalizationRejected)
        assert result.published_at is None

    def test_publication_time_is_carried_through_as_utc(self) -> None:
        moment = datetime(2026, 8, 3, 10, 30, tzinfo=UTC)
        result = normalize(make_raw_item(published_at=moment))
        assert not isinstance(result, NormalizationRejected)
        assert result.published_at == moment

    def test_offset_timestamps_convert_to_utc(self) -> None:
        kyiv = datetime(2026, 8, 3, 13, 30, tzinfo=UTC) + timedelta(0)
        result = normalize(make_raw_item(published_at=kyiv))
        assert not isinstance(result, NormalizationRejected)
        assert result.published_at.tzinfo is UTC  # type: ignore[union-attr]

    def test_ukrainian_content_survives(self) -> None:
        item = make_raw_item(
            title_original="Новина про штучний інтелект 🤖",
            summary_raw="Опис українською мовою з емодзі.",
        )
        result = normalize(item)
        assert not isinstance(result, NormalizationRejected)
        assert result.title == "Новина про штучний інтелект 🤖"

    def test_item_without_a_title_is_rejected(self) -> None:
        result = normalize(make_raw_item(title_original=None))
        assert isinstance(result, NormalizationRejected)
        assert "title" in result.reason

    def test_item_with_an_unusable_url_is_rejected(self) -> None:
        result = normalize(make_raw_item(url_original="ftp://example.invalid/x"))
        assert isinstance(result, NormalizationRejected)
        assert "URL" in result.reason

    def test_fingerprints_are_populated(self) -> None:
        result = normalize(
            make_raw_item(
                title_original="A reasonably long headline about an AI product update today",
                summary_raw="With enough words in the body for a stable fingerprint to exist.",
            )
        )
        assert not isinstance(result, NormalizationRejected)
        assert result.content_hash
        assert result.title_fingerprint
        assert result.simhash is not None

    def test_normalization_is_deterministic(self) -> None:
        item = make_raw_item(title_original="Same input", summary_raw="Same body text")
        first, second = normalize(item), normalize(item)
        assert not isinstance(first, NormalizationRejected)
        assert not isinstance(second, NormalizationRejected)
        assert first.content_hash == second.content_hash
        assert first.simhash == second.simhash
        assert first.canonical_url == second.canonical_url
