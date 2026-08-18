"""media.download.download_media — Step 4 sections 10, 22."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ai_news_editor.media.download import (
    InvalidContentTypeError,
    MediaDownloadError,
    MediaTooLargeError,
    download_media,
)
from ai_news_editor.media.urlsafety import UnsafeMediaUrlError

#: A literal public IP, so tests never touch real DNS resolution — validate_media_url
#: accepts it directly without a socket.getaddrinfo call.
PUBLIC_IP_URL = "http://93.184.216.34/photo.jpg"


def _transport(handler):  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


class TestSuccessfulDownload:
    def test_a_valid_image_response_is_written_to_disk(self, tmp_path: Path) -> None:
        body = b"\xff\xd8\xff" + b"0" * 100  # fake JPEG-ish bytes

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=body)

        dest = tmp_path / "original.jpg"
        result = download_media(PUBLIC_IP_URL, dest, kind="image", transport=_transport(handler))

        assert result.path == dest
        assert dest.read_bytes() == body
        assert result.size_bytes == len(body)
        assert result.content_type == "image/jpeg"


class TestRedirects:
    def test_a_single_redirect_is_followed(self, tmp_path: Path) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if str(request.url) == PUBLIC_IP_URL:
                return httpx.Response(302, headers={"location": "http://93.184.216.35/final.jpg"})
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"ok")

        dest = tmp_path / "original.jpg"
        result = download_media(PUBLIC_IP_URL, dest, kind="image", transport=_transport(handler))

        assert len(calls) == 2
        assert result.size_bytes == 2

    def test_too_many_redirects_is_rejected(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": str(request.url)})

        dest = tmp_path / "original.jpg"
        with pytest.raises(MediaDownloadError, match="too many redirects"):
            download_media(
                PUBLIC_IP_URL, dest, kind="image", transport=_transport(handler), max_redirects=2
            )

    def test_a_redirect_to_a_private_address_is_rejected(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == PUBLIC_IP_URL:
                return httpx.Response(302, headers={"location": "http://127.0.0.1/internal"})
            raise AssertionError("must never actually request the redirect target")

        dest = tmp_path / "original.jpg"
        with pytest.raises(UnsafeMediaUrlError, match="private, loopback"):
            download_media(PUBLIC_IP_URL, dest, kind="image", transport=_transport(handler))


class TestContentType:
    def test_html_pretending_to_be_an_image_is_rejected(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"<html>nope</html>"
            )

        dest = tmp_path / "original.jpg"
        with pytest.raises(InvalidContentTypeError, match="text/html"):
            download_media(PUBLIC_IP_URL, dest, kind="image", transport=_transport(handler))
        assert not dest.exists()

    def test_a_missing_content_type_is_rejected(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"whatever")

        dest = tmp_path / "original.jpg"
        with pytest.raises(InvalidContentTypeError):
            download_media(PUBLIC_IP_URL, dest, kind="image", transport=_transport(handler))

    def test_video_kind_rejects_an_image_content_type(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x")

        dest = tmp_path / "original.mp4"
        with pytest.raises(InvalidContentTypeError):
            download_media(PUBLIC_IP_URL, dest, kind="video", transport=_transport(handler))


class TestSizeLimits:
    def test_a_declared_content_length_over_the_limit_is_rejected_before_reading_the_body(
        self, tmp_path: Path
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "image/jpeg", "content-length": "999999999"},
                content=b"short",
            )

        dest = tmp_path / "original.jpg"
        with pytest.raises(MediaTooLargeError, match="999999999"):
            download_media(
                PUBLIC_IP_URL, dest, kind="image", max_bytes=100, transport=_transport(handler)
            )

    def test_a_body_exceeding_the_limit_without_declaring_it_is_aborted_mid_stream(
        self, tmp_path: Path
    ) -> None:
        """No Content-Length header at all — the only guard left is the streaming cap."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x" * 500)

        dest = tmp_path / "original.jpg"
        with pytest.raises(MediaTooLargeError):
            download_media(
                PUBLIC_IP_URL, dest, kind="image", max_bytes=100, transport=_transport(handler)
            )
        assert not dest.exists()


class TestTransportFailures:
    def test_a_non_2xx_status_raises_media_download_error(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        dest = tmp_path / "original.jpg"
        with pytest.raises(MediaDownloadError, match="404"):
            download_media(PUBLIC_IP_URL, dest, kind="image", transport=_transport(handler))

    def test_a_transport_error_raises_media_download_error(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        dest = tmp_path / "original.jpg"
        with pytest.raises(MediaDownloadError, match="transport error"):
            download_media(PUBLIC_IP_URL, dest, kind="image", transport=_transport(handler))


class TestUnsafeUrlIsRejectedBeforeAnyRequest:
    def test_a_private_address_is_never_even_requested(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("an unsafe URL must never reach the transport")

        dest = tmp_path / "original.jpg"
        with pytest.raises(UnsafeMediaUrlError):
            download_media(
                "http://127.0.0.1/x.jpg", dest, kind="image", transport=_transport(handler)
            )
