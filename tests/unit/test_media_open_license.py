"""media.open_license — Step 6B, layer (B): verified open-license media (Wikimedia
Commons only this iteration — see the module's own docstring for why Pexels/Unsplash
are deliberately not implemented without real API credentials to test against).

Fixture JSON below is modeled on a *real* response shape confirmed by hand against the
live Commons API (``commons.wikimedia.org/w/api.php``) while writing this module —
field names (``LicenseShortName``, ``LicenseUrl``, ``Artist``, ``mime``, ``width``,
``height``) are exactly what the real API returns, not guessed.
"""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from ai_news_editor.media.models import DiscoveryMethod, MediaKind, RejectionReason
from ai_news_editor.media.open_license import (
    WIKIMEDIA_COMMONS_PROVIDER_NAME,
    OpenLicenseCandidate,
    discover_wikimedia_commons,
    download_and_process_open_license_asset,
)
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.sources.http import HttpClient


def _commons_page(
    *,
    title: str,
    mime: str = "image/jpeg",
    width: int = 1200,
    height: int = 800,
    license_short: str | None = "CC BY-SA 4.0",
    license_url: str | None = "https://creativecommons.org/licenses/by-sa/4.0",
    artist: str | None = '<a href="//commons.wikimedia.org/wiki/User:Someone">Someone</a>',
    url: str | None = "https://upload.wikimedia.org/wikipedia/commons/x/example.jpg",
) -> dict[str, object]:
    extmetadata: dict[str, object] = {}
    if license_short is not None:
        extmetadata["LicenseShortName"] = {"value": license_short}
    if license_url is not None:
        extmetadata["LicenseUrl"] = {"value": license_url}
    if artist is not None:
        extmetadata["Artist"] = {"value": artist}
    imageinfo: dict[str, object] = {"mime": mime, "width": width, "height": height}
    if url is not None:
        imageinfo["url"] = url
    imageinfo["extmetadata"] = extmetadata
    return {"title": title, "ns": 6, "imageinfo": [imageinfo]}


def _search_transport(pages: dict[str, dict[str, object]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"query": {"pages": pages}})

    return httpx.MockTransport(handler)


class TestDiscoverWikimediaCommons:
    def test_a_relevant_reusable_licensed_image_is_found(self) -> None:
        pages = {
            "1": _commons_page(title="File:Artificial Intelligence Concept.png"),
        }
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons(
            "artificial intelligence", http=http, keywords=["Artificial Intelligence"]
        )
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.title == "Artificial Intelligence Concept.png"
        assert candidate.creator == "Someone"
        assert candidate.license == "CC BY-SA 4.0"
        assert candidate.license_url == "https://creativecommons.org/licenses/by-sa/4.0"

    def test_a_pdf_is_never_a_candidate(self) -> None:
        pages = {"1": _commons_page(title="File:Report.pdf", mime="application/pdf")}
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons("artificial intelligence", http=http)
        assert candidates == []

    def test_a_non_commercial_license_is_refused(self) -> None:
        pages = {"1": _commons_page(title="File:X.png", license_short="CC BY-NC 4.0")}
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons("x", http=http)
        assert candidates == []

    def test_a_no_derivatives_license_is_refused(self) -> None:
        pages = {"1": _commons_page(title="File:X.png", license_short="CC BY-ND 4.0")}
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons("x", http=http)
        assert candidates == []

    @pytest.mark.parametrize("license_short", ["CC0", "Public domain", "CC BY 4.0", "CC BY-SA 3.0"])
    def test_reusable_licenses_are_accepted(self, license_short: str) -> None:
        pages = {"1": _commons_page(title="File:X.png", license_short=license_short)}
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons("x", http=http)
        assert len(candidates) == 1

    def test_a_missing_license_is_refused(self) -> None:
        pages = {"1": _commons_page(title="File:X.png", license_short=None)}
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons("x", http=http)
        assert candidates == []

    def test_an_image_with_no_named_creator_is_refused(self) -> None:
        """Never guess a credit line — no Artist field means no honest attribution,
        so the candidate is skipped rather than credited to "Unknown"."""
        pages = {"1": _commons_page(title="File:X.png", artist=None)}
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons("x", http=http)
        assert candidates == []

    def test_html_in_the_artist_field_is_stripped_to_plain_text(self) -> None:
        pages = {
            "1": _commons_page(
                title="File:X.png",
                artist='<a href="//commons.wikimedia.org/wiki/User:J.Doe" title="User:J.Doe">'
                "J.Doe</a>",
            )
        }
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons("x", http=http)
        assert candidates[0].creator == "J.Doe"

    def test_a_title_not_matching_any_keyword_is_refused(self) -> None:
        pages = {"1": _commons_page(title="File:Unrelated Landscape Photo.jpg")}
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons(
            "artificial intelligence", http=http, keywords=["Sundar Pichai"]
        )
        assert candidates == []

    def test_all_keywords_must_match_not_just_one(self) -> None:
        """Live-confirmed false positive that motivated this: a single generic
        keyword like a product name can collide with an unrelated real-world image
        (a place name, a common word) in Commons' huge index. Requiring every
        keyword present eliminates that — a title matching only one of two keywords
        is refused, not accepted."""
        pages = {
            "1": _commons_page(title="File:Sora, an Italian comune in Lazio.jpg"),
        }
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons(
            "OpenAI Sora", http=http, keywords=["OpenAI", "Sora"]
        )
        assert candidates == []

    def test_all_matching_keywords_together_is_accepted(self) -> None:
        pages = {
            "1": _commons_page(title="File:OpenAI Sora demo screenshot.png"),
        }
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons(
            "OpenAI Sora", http=http, keywords=["OpenAI", "Sora"]
        )
        assert len(candidates) == 1

    def test_no_keywords_means_no_title_filter(self) -> None:
        pages = {"1": _commons_page(title="File:Anything At All.jpg")}
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons("x", http=http, keywords=[])
        assert len(candidates) == 1

    def test_below_minimum_dimensions_is_refused(self) -> None:
        pages = {"1": _commons_page(title="File:Tiny.png", width=50, height=50)}
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons("x", http=http)
        assert candidates == []

    def test_a_network_failure_returns_no_candidates_not_an_exception(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        http = HttpClient(transport=httpx.MockTransport(handler), max_attempts=1)
        candidates = discover_wikimedia_commons("x", http=http)
        assert candidates == []

    def test_malformed_json_returns_no_candidates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        http = HttpClient(transport=httpx.MockTransport(handler))
        candidates = discover_wikimedia_commons("x", http=http)
        assert candidates == []

    def test_results_are_ranked_largest_first(self) -> None:
        pages = {
            "1": _commons_page(title="File:Small.png", width=300, height=300),
            "2": _commons_page(title="File:Big.png", width=3000, height=3000),
        }
        http = HttpClient(transport=_search_transport(pages))
        candidates = discover_wikimedia_commons("x", http=http)
        assert [c.title for c in candidates] == ["Big.png", "Small.png"]


class TestDownloadAndProcessOpenLicenseAsset:
    def test_a_relevant_licensed_candidate_is_downloaded_processed_and_attributed(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        body = io.BytesIO()
        Image.new("RGB", (800, 600), "blue").save(body, format="JPEG")
        image_bytes = body.getvalue()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=image_bytes)

        import socket

        monkeypatch.setattr(
            socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))]
        )

        candidate = OpenLicenseCandidate(
            url="https://upload.wikimedia.org/wikipedia/commons/x/example.jpg",
            title="Artificial Intelligence Concept.png",
            width=800,
            height=600,
            creator="Someone",
            license="CC BY-SA 4.0",
            license_url="https://creativecommons.org/licenses/by-sa/4.0",
        )
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = download_and_process_open_license_asset(
                candidate, workspace, transport=httpx.MockTransport(handler)
            )

        assert outcome.ok is True
        assert outcome.media is not None
        assert outcome.media.kind is MediaKind.IMAGE
        assert outcome.media.source_method is DiscoveryMethod.OPEN_LICENSE_PROVIDER
        assert outcome.media.media_provider == WIKIMEDIA_COMMONS_PROVIDER_NAME
        assert outcome.media.creator == "Someone"
        assert outcome.media.license == "CC BY-SA 4.0"
        assert outcome.media.license_url == "https://creativecommons.org/licenses/by-sa/4.0"
        assert "Someone" in (outcome.media.required_credit or "")
        assert WIKIMEDIA_COMMONS_PROVIDER_NAME in (outcome.media.required_credit or "")

    def test_a_download_failure_is_a_normal_rejection_not_an_exception(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        import ai_news_editor.media.open_license as open_license_module

        def failing_download_media(url, dest, *, kind, transport=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated download failure")

        monkeypatch.setattr(open_license_module, "download_media", failing_download_media)

        candidate = OpenLicenseCandidate(
            url="https://upload.wikimedia.org/wikipedia/commons/x/missing.jpg",
            title="Missing",
            width=800,
            height=600,
            creator="Someone",
            license="CC0",
            license_url=None,
        )
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = download_and_process_open_license_asset(candidate, workspace)
        assert outcome.ok is False
        assert outcome.reason == RejectionReason.PROCESSING_FAILED
