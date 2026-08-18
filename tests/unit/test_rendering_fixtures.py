"""rendering.fixtures.SAMPLE_CONTENT — Step 5 section 46."""

from __future__ import annotations

import pytest

from ai_news_editor.domain.enums import EditorialCategory
from ai_news_editor.rendering.fixtures import SAMPLE_CONTENT
from ai_news_editor.rendering.render import render_editorial_post


class TestSampleContent:
    def test_every_category_has_a_sample(self) -> None:
        assert set(SAMPLE_CONTENT.keys()) == set(EditorialCategory)

    @pytest.mark.parametrize("category", list(EditorialCategory))
    def test_every_sample_renders_without_error(self, category: EditorialCategory) -> None:
        rendered = render_editorial_post(SAMPLE_CONTENT[category])
        assert rendered.full_text
        assert rendered.full_text.startswith("*")

    @pytest.mark.parametrize("category", list(EditorialCategory))
    def test_every_sample_uses_only_invalid_urls(self, category: EditorialCategory) -> None:
        content = SAMPLE_CONTENT[category]
        assert ".invalid" in content.source_url
