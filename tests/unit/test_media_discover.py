"""media.discover — Step 4 section 24: fixture tests for every discovery method."""

from __future__ import annotations

import json

from ai_news_editor.media.discover import discover_from_feed_entry, discover_from_html
from ai_news_editor.media.models import DiscoveryMethod, MediaKind

SOURCE_URL = "https://blog.example.invalid/posts/1"


class TestFeedEnclosureImage:
    def test_an_image_enclosure_is_discovered(self) -> None:
        payload = json.dumps(
            {
                "enclosures": [
                    {
                        "href": "https://blog.example.invalid/hero.jpg",
                        "type": "image/jpeg",
                        "width": "1200",
                        "height": "630",
                    }
                ]
            }
        )
        candidates = discover_from_feed_entry(payload, SOURCE_URL)
        assert len(candidates) == 1
        assert candidates[0].kind == MediaKind.IMAGE
        assert candidates[0].source_method == DiscoveryMethod.FEED_ENCLOSURE
        assert candidates[0].width == 1200
        assert candidates[0].height == 630


class TestFeedEnclosureVideo:
    def test_a_video_enclosure_is_discovered(self) -> None:
        payload = json.dumps(
            {"enclosures": [{"href": "https://blog.example.invalid/clip.mp4", "type": "video/mp4"}]}
        )
        candidates = discover_from_feed_entry(payload, SOURCE_URL)
        assert len(candidates) == 1
        assert candidates[0].kind == MediaKind.VIDEO

    def test_media_content_field_is_also_read(self) -> None:
        payload = json.dumps(
            {
                "media_content": [
                    {"url": "https://blog.example.invalid/media.mp4", "type": "video/mp4"}
                ]
            }
        )
        candidates = discover_from_feed_entry(payload, SOURCE_URL)
        assert len(candidates) == 1
        assert candidates[0].kind == MediaKind.VIDEO


class TestOpenGraphImage:
    def test_og_image_is_discovered(self) -> None:
        html = """
        <html><head>
        <meta property="og:image" content="https://blog.example.invalid/og.jpg">
        <meta property="og:image:width" content="1200">
        <meta property="og:image:height" content="628">
        </head></html>
        """
        candidates = discover_from_html(html, SOURCE_URL)
        assert len(candidates) == 1
        assert candidates[0].kind == MediaKind.IMAGE
        assert candidates[0].source_method == DiscoveryMethod.OPEN_GRAPH_IMAGE
        assert candidates[0].width == 1200


class TestOpenGraphVideo:
    def test_og_video_is_discovered(self) -> None:
        html = """
        <html><head>
        <meta property="og:video" content="https://blog.example.invalid/og.mp4">
        </head></html>
        """
        candidates = discover_from_html(html, SOURCE_URL)
        assert len(candidates) == 1
        assert candidates[0].kind == MediaKind.VIDEO
        assert candidates[0].source_method == DiscoveryMethod.OPEN_GRAPH_VIDEO


class TestJsonLdImage:
    def test_a_json_ld_image_property_is_discovered(self) -> None:
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type": "NewsArticle", "image": "https://blog.example.invalid/ld.jpg"}
        </script>
        </head></html>
        """
        candidates = discover_from_html(html, SOURCE_URL)
        assert len(candidates) == 1
        assert candidates[0].source_method == DiscoveryMethod.JSON_LD
        assert candidates[0].url == "https://blog.example.invalid/ld.jpg"

    def test_a_json_ld_image_object_with_url_is_discovered(self) -> None:
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type": "NewsArticle", "image": {"@type": "ImageObject", "url": "https://blog.example.invalid/ld2.jpg"}}
        </script>
        </head></html>
        """
        candidates = discover_from_html(html, SOURCE_URL)
        assert len(candidates) == 1
        assert candidates[0].url == "https://blog.example.invalid/ld2.jpg"


class TestNoMedia:
    def test_an_entry_with_no_media_fields_returns_nothing(self) -> None:
        assert discover_from_feed_entry(json.dumps({"title": "x"}), SOURCE_URL) == []

    def test_html_with_no_media_metadata_returns_nothing(self) -> None:
        assert discover_from_html("<html><head></head><body>hi</body></html>", SOURCE_URL) == []

    def test_malformed_json_payload_returns_nothing_rather_than_raising(self) -> None:
        assert discover_from_feed_entry("not json", SOURCE_URL) == []


class TestLogoAndTinyImageRejected:
    def test_a_url_naming_logo_is_rejected(self) -> None:
        html = """
        <html><head>
        <meta property="og:image" content="https://blog.example.invalid/site-logo.png">
        </head></html>
        """
        assert discover_from_html(html, SOURCE_URL) == []

    def test_a_declared_tiny_image_is_rejected(self) -> None:
        html = """
        <html><head>
        <meta property="og:image" content="https://blog.example.invalid/icon.jpg">
        <meta property="og:image:width" content="32">
        <meta property="og:image:height" content="32">
        </head></html>
        """
        assert discover_from_html(html, SOURCE_URL) == []


class TestInvalidUrlRejected:
    def test_an_enclosure_with_no_href_or_url_is_skipped(self) -> None:
        payload = json.dumps({"enclosures": [{"type": "image/jpeg"}]})
        assert discover_from_feed_entry(payload, SOURCE_URL) == []

    def test_an_enclosure_with_an_unrecognized_type_and_extension_is_skipped(self) -> None:
        payload = json.dumps(
            {
                "enclosures": [
                    {"href": "https://blog.example.invalid/thing", "type": "application/zip"}
                ]
            }
        )
        assert discover_from_feed_entry(payload, SOURCE_URL) == []


class TestDedupAndRanking:
    def test_duplicate_urls_across_methods_are_deduplicated(self) -> None:
        html = """
        <html><head>
        <meta property="og:image" content="https://blog.example.invalid/hero.jpg">
        <script type="application/ld+json">
        {"image": "https://blog.example.invalid/hero.jpg"}
        </script>
        </head></html>
        """
        candidates = discover_from_html(html, SOURCE_URL)
        assert len(candidates) == 1

    def test_relative_urls_are_resolved_against_the_source(self) -> None:
        html = """
        <html><head>
        <meta property="og:image" content="/media/hero.jpg">
        </head></html>
        """
        candidates = discover_from_html(html, SOURCE_URL)
        assert candidates[0].url == "https://blog.example.invalid/media/hero.jpg"
