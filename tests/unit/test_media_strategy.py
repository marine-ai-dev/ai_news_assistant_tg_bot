"""media.strategy.select_media_with_fallbacks — Step 6B: the four-layer order.

Every layer's own module is already unit-tested (test_media_pipeline.py,
test_media_licensed_assets.py, test_media_open_license.py, test_media_branded_card.py)
— these tests check *ordering and fallthrough*: given a fixed set of layer outcomes,
does this function try them in the right order and stop at the first success.
"""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from ai_news_editor.domain.enums import EditorialCategory, MediaPolicy
from ai_news_editor.media.branded_card import BrandedCardError
from ai_news_editor.media.licensed_assets import GOOGLE_SOURCE_IDS
from ai_news_editor.media.models import DiscoveryMethod
from ai_news_editor.media.strategy import select_media_with_fallbacks
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.sources.http import HttpClient

_GOOGLE_SOURCE_ID = next(iter(GOOGLE_SOURCE_IDS))

_PRESS_CORNER_HTML = """
<html><body>
<img src="/images/sundar-pichai.jpg" alt="Sundar Pichai, CEO of Google" width="800" height="600">
</body></html>
"""


def _jpeg_bytes(size: tuple[int, int] = (800, 600)) -> bytes:
    body = io.BytesIO()
    Image.new("RGB", size, "blue").save(body, format="JPEG")
    return body.getvalue()


def _commons_page(title: str) -> dict[str, object]:
    return {
        "title": f"File:{title}",
        "ns": 6,
        "imageinfo": [
            {
                "mime": "image/jpeg",
                "width": 1200,
                "height": 800,
                "url": "https://upload.wikimedia.org/wikipedia/commons/x/example.jpg",
                "extmetadata": {
                    "LicenseShortName": {"value": "CC BY-SA 4.0"},
                    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
                    "Artist": {"value": "Someone"},
                },
            }
        ],
    }


def _transport(
    *, press_corner_html: str | None, commons_pages: dict[str, object], image_bytes: bytes
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "commons.wikimedia.org" in url:
            return httpx.Response(200, json={"query": {"pages": commons_pages}})
        if "blog.google/image-library" in url:
            if press_corner_html is None:
                return httpx.Response(404)
            return httpx.Response(200, text=press_corner_html)
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=image_bytes)

    return httpx.MockTransport(handler)


@pytest.fixture
def _resolve_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))]
    )


class TestLayerOrderAndFallthrough:
    def test_layer_a_wins_when_the_source_is_google_and_press_corner_matches(
        self, tmp_path, _resolve_dns
    ) -> None:  # type: ignore[no-untyped-def]
        transport = _transport(
            press_corner_html=_PRESS_CORNER_HTML,
            commons_pages={"1": _commons_page("Artificial Intelligence.png")},
            image_bytes=_jpeg_bytes(),
        )
        http = HttpClient(transport=transport)
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media_with_fallbacks(
                workspace=workspace,
                http=http,
                source_id=_GOOGLE_SOURCE_ID,
                source_url="https://blog.google/example",
                media_policy=MediaPolicy.NO_MEDIA,
                category=EditorialCategory.NEWS,
                headline="Sundar Pichai announces something",
                source_label="Google",
                story_keywords=["Sundar Pichai"],
                download_transport=transport,
            )
        assert outcome.ok is True
        assert outcome.media is not None
        assert outcome.media.source_method is DiscoveryMethod.LICENSED_LIBRARY

    def test_layer_b_wins_when_the_source_is_not_google(
        self, tmp_path, _resolve_dns
    ) -> None:  # type: ignore[no-untyped-def]
        transport = _transport(
            press_corner_html=None,
            commons_pages={"1": _commons_page("Artificial Intelligence Concept.png")},
            image_bytes=_jpeg_bytes(),
        )
        http = HttpClient(transport=transport)
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media_with_fallbacks(
                workspace=workspace,
                http=http,
                source_id="openai_news",
                source_url="https://openai.com/example",
                media_policy=MediaPolicy.NO_MEDIA,
                category=EditorialCategory.NEWS,
                headline="Artificial Intelligence Concept announced",
                source_label="OpenAI",
                story_keywords=["Artificial Intelligence Concept"],
                download_transport=transport,
            )
        assert outcome.ok is True
        assert outcome.media is not None
        assert outcome.media.source_method is DiscoveryMethod.OPEN_LICENSE_PROVIDER

    def test_layer_c_wins_when_a_and_b_find_nothing(
        self, tmp_path, _resolve_dns
    ) -> None:  # type: ignore[no-untyped-def]
        transport = _transport(press_corner_html=None, commons_pages={}, image_bytes=_jpeg_bytes())
        http = HttpClient(transport=transport)
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media_with_fallbacks(
                workspace=workspace,
                http=http,
                source_id="openai_news",
                source_url="https://openai.com/example",
                media_policy=MediaPolicy.NO_MEDIA,
                category=EditorialCategory.NEWS,
                headline="Щось сталося",
                source_label="OpenAI",
                story_keywords=["OpenAI"],
                download_transport=transport,
            )
        assert outcome.ok is True
        assert outcome.media is not None
        assert outcome.media.source_method is DiscoveryMethod.GENERATED_CARD

    def test_a_non_google_source_never_reaches_the_press_corner_at_all(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """No HTTP request for the Press Corner URL should even happen for a non-
        Google source — asserted by making that URL a hard failure and confirming the
        pipeline still resolves via layer C without raising."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "blog.google" in str(request.url):
                raise AssertionError("layer A should never be attempted for a non-Google source")
            if "commons.wikimedia.org" in str(request.url):
                return httpx.Response(200, json={"query": {"pages": {}}})
            return httpx.Response(404)

        http = HttpClient(transport=httpx.MockTransport(handler))
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media_with_fallbacks(
                workspace=workspace,
                http=http,
                source_id="openai_news",
                source_url="https://openai.com/example",
                media_policy=MediaPolicy.NO_MEDIA,
                category=EditorialCategory.AI_TOOL,
                headline="Headline",
                source_label="OpenAI",
                story_keywords=["OpenAI"],
            )
        assert outcome.ok is True
        assert outcome.media is not None
        assert outcome.media.source_method is DiscoveryMethod.GENERATED_CARD

    def test_layer_d_text_only_when_even_the_branded_card_fails(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ai_news_editor.media.strategy as strategy_module

        def failing_card(**kwargs: object) -> None:
            raise BrandedCardError("simulated: committed font missing")

        monkeypatch.setattr(strategy_module, "generate_branded_card", failing_card)

        http = HttpClient(
            transport=_transport(press_corner_html=None, commons_pages={}, image_bytes=b"")
        )
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media_with_fallbacks(
                workspace=workspace,
                http=http,
                source_id="openai_news",
                source_url="https://openai.com/example",
                media_policy=MediaPolicy.NO_MEDIA,
                category=EditorialCategory.NEWS,
                headline="Headline",
                source_label="OpenAI",
                story_keywords=["OpenAI"],
            )
        assert outcome.ok is False
        assert outcome.media is None

    def test_no_story_keywords_skips_layer_b_without_a_network_call(self, tmp_path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "commons.wikimedia.org" in str(request.url):
                raise AssertionError("layer B should never be attempted with no keywords")
            return httpx.Response(404)

        http = HttpClient(transport=httpx.MockTransport(handler))
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media_with_fallbacks(
                workspace=workspace,
                http=http,
                source_id="openai_news",
                source_url="https://openai.com/example",
                media_policy=MediaPolicy.NO_MEDIA,
                category=EditorialCategory.NEWS,
                headline="Headline",
                source_label="OpenAI",
                story_keywords=[],
            )
        assert outcome.ok is True
        assert outcome.media is not None
        assert outcome.media.source_method is DiscoveryMethod.GENERATED_CARD
