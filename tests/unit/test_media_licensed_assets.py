"""media.licensed_assets — Step 6 section 5: the one verified, narrow media path.

Google's Press Corner Image Library page (https://blog.google/image-library/) states,
in its own words: "Images on this page may be used for publication with credit:
'Source: Google.'" — verified by direct fetch, not assumed. This module's job is to
use *only* that page, *only* for a Google source, and *only* an image whose alt text
actually matches the story — never substitute an unrelated image, never touch any
other source's still-NO_MEDIA policy.
"""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from ai_news_editor.media.discover import discover_licensed_library_assets
from ai_news_editor.media.licensed_assets import (
    GOOGLE_PRESS_CORNER_URL,
    GOOGLE_SOURCE_IDS,
    download_and_process_press_corner_asset,
)
from ai_news_editor.media.models import DiscoveryMethod, RejectionReason
from ai_news_editor.media.workspace import MediaWorkspace

_LIBRARY_HTML = """
<html><body>
<img src="/images/gemini-logo.png" alt="Gemini logo" width="800" height="600">
<img src="/images/sundar-pichai.jpg" alt="Sundar Pichai, CEO of Google" width="800" height="600">
<img src="/images/no-alt.jpg" width="800" height="600">
</body></html>
"""


class TestDiscoverLicensedLibraryAssets:
    def test_an_image_whose_alt_text_matches_a_keyword_is_found(self) -> None:
        # "gemini-logo.png" is deliberately excluded by this test's own keyword choice
        # — its URL contains "logo", which _filter_and_rank's existing logo-hint
        # filter (shared with every other discovery method) rejects on purpose.
        candidates = discover_licensed_library_assets(
            _LIBRARY_HTML, GOOGLE_PRESS_CORNER_URL, keywords=["Sundar Pichai"]
        )
        assert len(candidates) == 1
        assert candidates[0].source_method == DiscoveryMethod.LICENSED_LIBRARY
        assert candidates[0].url.endswith("sundar-pichai.jpg")

    def test_no_matching_keyword_finds_nothing(self) -> None:
        candidates = discover_licensed_library_assets(
            _LIBRARY_HTML, GOOGLE_PRESS_CORNER_URL, keywords=["Bard", "Waymo"]
        )
        assert candidates == []

    def test_an_image_with_no_alt_text_is_never_a_candidate(self) -> None:
        """Even if a URL keyword happens to match, no alt text means no candidate —
        this is the relevance signal, not a coincidence in the URL."""
        candidates = discover_licensed_library_assets(
            _LIBRARY_HTML, GOOGLE_PRESS_CORNER_URL, keywords=["no-alt"]
        )
        assert candidates == []

    def test_empty_keywords_finds_nothing(self) -> None:
        candidates = discover_licensed_library_assets(
            _LIBRARY_HTML, GOOGLE_PRESS_CORNER_URL, keywords=[]
        )
        assert candidates == []


class TestDownloadAndProcessPressCornerAsset:
    def test_a_non_google_source_is_refused_outright(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = download_and_process_press_corner_asset(
                source_id="openai_news",
                story_keywords=["Gemini"],
                press_corner_html=_LIBRARY_HTML,
                workspace=workspace,
            )
        assert outcome.ok is False
        assert outcome.reason == RejectionReason.POLICY_FORBIDS

    def test_a_google_source_with_no_relevant_image_falls_back_honestly(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        source_id = next(iter(GOOGLE_SOURCE_IDS))
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = download_and_process_press_corner_asset(
                source_id=source_id,
                story_keywords=["a completely unrelated topic"],
                press_corner_html=_LIBRARY_HTML,
                workspace=workspace,
            )
        assert outcome.ok is False
        assert outcome.reason == RejectionReason.NO_CANDIDATES

    def test_a_google_source_with_a_relevant_image_is_downloaded_and_processed(
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

        source_id = next(iter(GOOGLE_SOURCE_IDS))
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = download_and_process_press_corner_asset(
                source_id=source_id,
                story_keywords=["Sundar Pichai"],
                press_corner_html=_LIBRARY_HTML,
                workspace=workspace,
                transport=httpx.MockTransport(handler),
            )

        assert outcome.ok is True
        assert outcome.media is not None
        assert outcome.media.source_method == DiscoveryMethod.LICENSED_LIBRARY
