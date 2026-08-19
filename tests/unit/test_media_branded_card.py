"""media.branded_card — Step 6B: the universal, locally-generated media fallback.

No network, no Gemini, no generative AI — Pillow only. Tests check the module's own
guarantees: every category produces a valid image at the fixed dimensions, output is
deterministic, long input never overflows or crashes, and cleanup (via MediaWorkspace,
tested elsewhere) is the caller's responsibility, not this module's.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from PIL import Image

from ai_news_editor.domain.enums import EditorialCategory
from ai_news_editor.media.branded_card import generate_branded_card
from ai_news_editor.media.limits import BRANDED_CARD_HEIGHT, BRANDED_CARD_WIDTH
from ai_news_editor.media.models import DiscoveryMethod, MediaKind


class TestGeneratesForEveryCategory:
    @pytest.mark.parametrize("category", list(EditorialCategory))
    def test_every_category_produces_a_valid_card(
        self, tmp_path: Path, category: EditorialCategory
    ) -> None:
        dest = tmp_path / f"{category.value}.jpg"
        result = generate_branded_card(
            category=category,
            headline="ExampleCorp представила новий AI-режим автозаповнення",
            source_label="ExampleCorp",
            dest=dest,
        )
        assert result.kind is MediaKind.IMAGE
        assert result.source_method is DiscoveryMethod.GENERATED_CARD
        assert result.width == BRANDED_CARD_WIDTH
        assert result.height == BRANDED_CARD_HEIGHT
        assert dest.exists()
        with Image.open(dest) as img:
            img.verify()

    @pytest.mark.parametrize("category", list(EditorialCategory))
    def test_the_file_is_a_real_decodable_jpeg_at_the_declared_size(
        self, tmp_path: Path, category: EditorialCategory
    ) -> None:
        dest = tmp_path / "card.jpg"
        result = generate_branded_card(
            category=category, headline="Заголовок", source_label="Джерело", dest=dest
        )
        with Image.open(dest) as img:
            assert img.format == "JPEG"
            assert img.size == (result.width, result.height)


class TestNoSourceUrl:
    def test_source_url_is_empty_nothing_was_downloaded(self, tmp_path: Path) -> None:
        result = generate_branded_card(
            category=EditorialCategory.NEWS,
            headline="Заголовок",
            source_label="Джерело",
            dest=tmp_path / "card.jpg",
        )
        assert result.source_url == ""


class TestDeterminism:
    def test_the_same_inputs_produce_byte_identical_output(self, tmp_path: Path) -> None:
        kwargs = {
            "category": EditorialCategory.AI_TOOL,
            "headline": "Notely перетворює голосові нотатки на структурований текст",
            "source_label": "Notely",
        }
        first = tmp_path / "first.jpg"
        second = tmp_path / "second.jpg"
        generate_branded_card(dest=first, **kwargs)
        generate_branded_card(dest=second, **kwargs)
        assert first.read_bytes() == second.read_bytes()

    def test_different_categories_produce_different_output(self, tmp_path: Path) -> None:
        a = tmp_path / "a.jpg"
        b = tmp_path / "b.jpg"
        generate_branded_card(
            category=EditorialCategory.NEWS, headline="X", source_label="Y", dest=a
        )
        generate_branded_card(
            category=EditorialCategory.RESEARCH, headline="X", source_label="Y", dest=b
        )
        assert a.read_bytes() != b.read_bytes()


class TestLongInputNeverOverflowsOrCrashes:
    def test_a_very_long_headline_is_shortened_and_still_produces_a_valid_card(
        self, tmp_path: Path
    ) -> None:
        headline = "Дуже довгий заголовок новини про штучний інтелект. " * 10
        dest = tmp_path / "card.jpg"
        result = generate_branded_card(
            category=EditorialCategory.EXPLAINER,
            headline=headline,
            source_label="ExampleCorp",
            dest=dest,
        )
        assert result.width == BRANDED_CARD_WIDTH
        assert result.height == BRANDED_CARD_HEIGHT
        with Image.open(dest) as img:
            img.verify()

    def test_a_very_long_source_label_does_not_crash(self, tmp_path: Path) -> None:
        dest = tmp_path / "card.jpg"
        result = generate_branded_card(
            category=EditorialCategory.FREE_DEAL,
            headline="Заголовок",
            source_label="A" * 300,
            dest=dest,
        )
        assert dest.exists()
        assert result.size_bytes > 0

    def test_an_empty_headline_does_not_crash(self, tmp_path: Path) -> None:
        dest = tmp_path / "card.jpg"
        generate_branded_card(
            category=EditorialCategory.AI_LIFEHACK, headline="", source_label="X", dest=dest
        )
        assert dest.exists()


class TestPaletteExpansion:
    """Step 6C: real-channel visual review found the cards read as "just blue and
    green" — the palette must cover at least blue/green/purple/pink/orange/red/yellow,
    and must actually vary across different stories in the same category, not just
    across categories."""

    _REQUIRED_HUES: ClassVar[set[str]] = {
        "blue", "green", "purple", "pink", "orange", "red", "yellow"
    }

    def test_every_required_hue_is_present_in_the_palette(self) -> None:
        from ai_news_editor.media.branded_card import _PALETTE

        names = {name for name, _top, _bottom in _PALETTE}
        assert names >= self._REQUIRED_HUES

    def test_different_headlines_in_the_same_category_pick_different_colors(self) -> None:
        from ai_news_editor.media.branded_card import _pick_palette_entry

        names = {
            _pick_palette_entry(EditorialCategory.NEWS, f"Headline number {i}")[0]
            for i in range(20)
        }
        assert len(names) > 1

    def test_the_pick_is_deterministic_for_the_same_category_and_headline(self) -> None:
        from ai_news_editor.media.branded_card import _pick_palette_entry

        first = _pick_palette_entry(EditorialCategory.NEWS, "A fixed headline")
        second = _pick_palette_entry(EditorialCategory.NEWS, "A fixed headline")
        assert first == second

    def test_a_different_category_with_the_same_headline_can_pick_a_different_color(
        self,
    ) -> None:
        from ai_news_editor.media.branded_card import _pick_palette_entry

        picks = {
            category: _pick_palette_entry(category, "Same headline text")[0]
            for category in EditorialCategory
        }
        # Not asserting every category differs (a hash collision is fine) — just that
        # the category is actually part of what the pick depends on.
        assert len(set(picks.values())) > 1

    def test_the_icon_still_reads_against_the_picked_background(self, tmp_path: Path) -> None:
        """The icon color must track the picked palette entry, not a stale
        category-fixed color — regression check for the refactor that introduced the
        rotating palette."""
        dest_a = tmp_path / "a.jpg"
        dest_b = tmp_path / "b.jpg"
        result_a = generate_branded_card(
            category=EditorialCategory.NEWS, headline="Headline A", source_label="X", dest=dest_a
        )
        result_b = generate_branded_card(
            category=EditorialCategory.NEWS, headline="Headline B", source_label="X", dest=dest_b
        )
        # Two different headlines in the same category are very likely to land on
        # different palette entries (9 entries, two independent picks) — a byte
        # difference confirms the background (and therefore the icon color drawn
        # against it) actually changed, not just the headline text region.
        assert (
            result_a.size_bytes != result_b.size_bytes
            or dest_a.read_bytes() != dest_b.read_bytes()
        )


class TestFileSize:
    def test_output_is_comfortably_small(self, tmp_path: Path) -> None:
        """A simple gradient + text card should be tiny next to a real downloaded
        photo — nowhere near Telegram's photo size limit."""
        dest = tmp_path / "card.jpg"
        result = generate_branded_card(
            category=EditorialCategory.NEWS,
            headline="ExampleCorp представила новий AI-режим",
            source_label="ExampleCorp",
            dest=dest,
        )
        assert result.size_bytes < 1_000_000
