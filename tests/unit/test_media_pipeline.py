"""media.pipeline.select_media — Step 4 sections 25, 29: policy gating and fallback."""

from __future__ import annotations

import io
import json
import socket
from pathlib import Path

import httpx
import pytest
from PIL import Image

from ai_news_editor.domain.enums import MediaPolicy
from ai_news_editor.media.models import DiscoveryMethod, MediaKind, RejectionReason
from ai_news_editor.media.pipeline import select_media
from ai_news_editor.media.workspace import MediaWorkspace

SOURCE_URL = "https://blog.example.invalid/posts/1"


@pytest.fixture(autouse=True)
def _resolve_invalid_hostnames_as_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """``*.example.invalid`` never actually resolves (RFC 2606) — media.urlsafety's
    real DNS check would otherwise reject every URL in this file as unresolvable. The
    MockTransport below intercepts the request regardless of what this "resolves" to;
    this fixture only satisfies the pre-flight SSRF check, tested on its own merits in
    test_media_urlsafety.py."""
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))]
    )


def _jpeg_bytes(size: tuple[int, int] = (800, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format="JPEG")
    return buf.getvalue()


def _feed_with_image(url: str = "https://blog.example.invalid/hero.jpg") -> str:
    return json.dumps(
        {"enclosures": [{"href": url, "type": "image/jpeg", "width": 1200, "height": 630}]}
    )


class TestPolicyGate:
    def test_no_media_policy_never_attempts_a_download(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("NO_MEDIA must never make an HTTP request")

        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media(
                workspace=workspace,
                source_url=SOURCE_URL,
                media_policy=MediaPolicy.NO_MEDIA,
                feed_payload_raw=_feed_with_image(),
                transport=httpx.MockTransport(handler),
            )

        assert outcome.ok is False
        assert outcome.reason == RejectionReason.POLICY_FORBIDS

    def test_link_preview_only_never_attempts_a_download(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("LINK_PREVIEW_ONLY must never reupload")

        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media(
                workspace=workspace,
                source_url=SOURCE_URL,
                media_policy=MediaPolicy.LINK_PREVIEW_ONLY,
                feed_payload_raw=_feed_with_image(),
                transport=httpx.MockTransport(handler),
            )

        assert outcome.ok is False
        assert outcome.reason == RejectionReason.POLICY_FORBIDS

    def test_discover_media_policy_proceeds_to_download(self, tmp_path: Path) -> None:
        body = _jpeg_bytes()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=body)

        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media(
                workspace=workspace,
                source_url=SOURCE_URL,
                media_policy=MediaPolicy.DISCOVER_MEDIA,
                feed_payload_raw=_feed_with_image(),
                transport=httpx.MockTransport(handler),
            )
            assert outcome.ok is True
            assert outcome.media.kind == MediaKind.IMAGE
            assert Path(outcome.media.path).exists()


class TestNoCandidates:
    def test_no_media_fields_at_all_yields_no_candidates(self, tmp_path: Path) -> None:
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media(
                workspace=workspace,
                source_url=SOURCE_URL,
                media_policy=MediaPolicy.DISCOVER_MEDIA,
                feed_payload_raw=json.dumps({"title": "no media here"}),
            )
        assert outcome.ok is False
        assert outcome.reason == RejectionReason.NO_CANDIDATES


class TestFallback:
    def test_a_failed_download_falls_through_to_the_next_candidate(self, tmp_path: Path) -> None:
        good_body = _jpeg_bytes()

        def handler(request: httpx.Request) -> httpx.Response:
            if "broken" in str(request.url):
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=good_body)

        payload = json.dumps(
            {
                "enclosures": [
                    {"href": "https://blog.example.invalid/broken.jpg", "type": "image/jpeg"},
                    {"href": "https://blog.example.invalid/good.jpg", "type": "image/jpeg"},
                ]
            }
        )
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media(
                workspace=workspace,
                source_url=SOURCE_URL,
                media_policy=MediaPolicy.DISCOVER_MEDIA,
                feed_payload_raw=payload,
                transport=httpx.MockTransport(handler),
            )
        assert outcome.ok is True
        assert outcome.media.source_url.endswith("good.jpg")

    def test_every_candidate_failing_yields_a_reason_not_an_exception(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media(
                workspace=workspace,
                source_url=SOURCE_URL,
                media_policy=MediaPolicy.DISCOVER_MEDIA,
                feed_payload_raw=_feed_with_image(),
                transport=httpx.MockTransport(handler),
            )
        assert outcome.ok is False
        assert outcome.reason == RejectionReason.DOWNLOAD_FAILED

    def test_a_tiny_image_is_rejected_as_too_small(self, tmp_path: Path) -> None:
        tiny = _jpeg_bytes((50, 50))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=tiny)

        payload = json.dumps(
            {
                "enclosures": [
                    {"href": "https://blog.example.invalid/tiny.jpg", "type": "image/jpeg"}
                ]
            }
        )
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media(
                workspace=workspace,
                source_url=SOURCE_URL,
                media_policy=MediaPolicy.DISCOVER_MEDIA,
                feed_payload_raw=payload,
                transport=httpx.MockTransport(handler),
            )
        assert outcome.ok is False
        assert outcome.reason == RejectionReason.TOO_SMALL

    def test_video_candidate_falls_back_to_image_candidate_when_ffmpeg_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ai_news_editor.media import video as video_module

        monkeypatch.setattr(video_module, "ffmpeg_available", lambda: False)

        image_body = _jpeg_bytes()

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith(".mp4"):
                return httpx.Response(
                    200, headers={"content-type": "video/mp4"}, content=b"fakevideo"
                )
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=image_body)

        payload = json.dumps(
            {
                "enclosures": [
                    {"href": "https://blog.example.invalid/clip.mp4", "type": "video/mp4"},
                    {"href": "https://blog.example.invalid/hero.jpg", "type": "image/jpeg"},
                ]
            }
        )
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media(
                workspace=workspace,
                source_url=SOURCE_URL,
                media_policy=MediaPolicy.DISCOVER_MEDIA,
                feed_payload_raw=payload,
                transport=httpx.MockTransport(handler),
            )
        # Images are ranked before video, so the image candidate is tried first and
        # succeeds outright — ffmpeg's unavailability never even needs to matter here.
        assert outcome.ok is True
        assert outcome.media.kind == MediaKind.IMAGE

    def test_a_corrupt_download_falls_back_to_none_with_a_clear_reason(
        self, tmp_path: Path
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "image/jpeg"}, content=b"not a real image"
            )

        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media(
                workspace=workspace,
                source_url=SOURCE_URL,
                media_policy=MediaPolicy.DISCOVER_MEDIA,
                feed_payload_raw=_feed_with_image(),
                transport=httpx.MockTransport(handler),
            )
        assert outcome.ok is False
        assert outcome.reason == RejectionReason.CORRUPT_MEDIA


class TestDiscoveryMethodIsRecorded:
    def test_the_outcome_records_which_method_found_the_media(self, tmp_path: Path) -> None:
        body = _jpeg_bytes()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=body)

        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = select_media(
                workspace=workspace,
                source_url=SOURCE_URL,
                media_policy=MediaPolicy.DISCOVER_MEDIA,
                feed_payload_raw=_feed_with_image(),
                transport=httpx.MockTransport(handler),
            )
        assert outcome.media.source_method == DiscoveryMethod.FEED_ENCLOSURE
