"""The outbound HTTP boundary: safety checks, caps, retries, and error classification."""

from __future__ import annotations

import gzip

import httpx
import pytest

from ai_news_editor.sources.http import (
    DEFAULT_USER_AGENT,
    HttpClient,
    HttpStatusError,
    HttpTimeoutError,
    HttpTransportError,
    ResponseTooLargeError,
    UnsafeUrlError,
    validate_url,
)
from tests.conftest import make_http_client, static_transport

FEED_URL = "https://feed.invalid/rss.xml"


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.invalid/feed.xml",
            "gopher://example.invalid",
            "javascript:alert(1)",
        ],
    )
    def test_non_http_schemes_are_refused(self, url: str) -> None:
        with pytest.raises(UnsafeUrlError, match="only http and https"):
            validate_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8080/feed",
            "http://127.0.0.1/feed",
            "https://10.0.0.5/feed",
            "https://192.168.1.10/feed",
            "https://172.16.0.1/feed",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/feed",
        ],
    )
    def test_private_and_loopback_addresses_are_refused(self, url: str) -> None:
        """Catches a mistyped config, including the cloud metadata endpoint."""
        with pytest.raises(UnsafeUrlError):
            validate_url(url)

    @pytest.mark.parametrize(
        "url", ["https://example.invalid/feed.xml", "http://news.example.invalid/rss"]
    )
    def test_public_http_urls_are_allowed(self, url: str) -> None:
        validate_url(url)

    def test_url_without_a_host_is_refused(self) -> None:
        with pytest.raises(UnsafeUrlError, match="no host"):
            validate_url("https:///feed.xml")

    def test_client_refuses_before_making_a_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("must not be reached")

        client = make_http_client(httpx.MockTransport(handler))
        with pytest.raises(UnsafeUrlError):
            client.get("http://127.0.0.1/feed")


class TestSuccessfulFetch:
    def test_returns_body_and_status(self) -> None:
        client = make_http_client(static_transport(b"<rss/>"))
        response = client.get(FEED_URL)
        assert response.status_code == 200
        assert response.body == b"<rss/>"

    def test_exposes_caching_validators(self) -> None:
        client = make_http_client(
            static_transport(
                b"<rss/>",
                headers={"etag": 'W/"abc"', "last-modified": "Mon, 03 Aug 2026 10:30:00 GMT"},
            )
        )
        response = client.get(FEED_URL)
        assert response.etag == 'W/"abc"'
        assert response.last_modified == "Mon, 03 Aug 2026 10:30:00 GMT"

    def test_headers_are_case_insensitive(self) -> None:
        client = make_http_client(static_transport(b"<rss/>", headers={"ETag": '"X"'}))
        assert client.get(FEED_URL).etag == '"X"'

    def test_identifies_itself_honestly(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, content=b"<rss/>")

        make_http_client(httpx.MockTransport(handler)).get(FEED_URL)
        assert seen["user-agent"] == DEFAULT_USER_AGENT
        assert "AiNewsEditorBot" in seen["user-agent"]

    def test_follows_redirects(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rss.xml":
                return httpx.Response(301, headers={"location": "https://feed.invalid/final.xml"})
            return httpx.Response(200, content=b"<rss>final</rss>")

        response = make_http_client(httpx.MockTransport(handler)).get(FEED_URL)
        assert response.body == b"<rss>final</rss>"
        assert response.url.endswith("/final.xml")

    def test_gzipped_bodies_are_decoded(self) -> None:
        payload = gzip.compress(b"<rss>compressed</rss>")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=payload, headers={"content-encoding": "gzip"})

        assert b"compressed" in make_http_client(httpx.MockTransport(handler)).get(FEED_URL).body


class TestConditionalRequests:
    def test_sends_validators_when_supplied(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(304)

        client = make_http_client(httpx.MockTransport(handler))
        client.get(FEED_URL, etag='W/"e"', last_modified="Mon, 03 Aug 2026 10:30:00 GMT")
        assert seen["if-none-match"] == 'W/"e"'
        assert seen["if-modified-since"] == "Mon, 03 Aug 2026 10:30:00 GMT"

    def test_omits_validators_when_unknown(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, content=b"<rss/>")

        make_http_client(httpx.MockTransport(handler)).get(FEED_URL)
        assert "if-none-match" not in seen
        assert "if-modified-since" not in seen

    def test_304_is_returned_not_raised(self) -> None:
        response = make_http_client(static_transport(b"", status_code=304)).get(FEED_URL)
        assert response.not_modified
        assert response.status_code == 304
        assert response.body == b""


class TestErrorClassification:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
    def test_client_errors_raise_and_are_not_retried(self, status: int) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(status)

        client = make_http_client(httpx.MockTransport(handler))
        with pytest.raises(HttpStatusError) as info:
            client.get(FEED_URL)
        assert info.value.status_code == status
        assert attempts["n"] == 1, "a 404 will not fix itself; retrying is just rude"

    def test_timeout_is_classified(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(HttpTimeoutError):
            make_http_client(httpx.MockTransport(handler)).get(FEED_URL)

    def test_connection_failure_is_classified(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(HttpTransportError):
            make_http_client(httpx.MockTransport(handler)).get(FEED_URL)


class TestRetries:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_transient_statuses_are_retried_then_given_up_on(self, status: int) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(status)

        client = make_http_client(httpx.MockTransport(handler), max_attempts=3)
        with pytest.raises(HttpStatusError):
            client.get(FEED_URL)
        assert attempts["n"] == 3, "bounded, never infinite"

    def test_a_transient_failure_then_success(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, content=b"<rss>recovered</rss>")

        response = make_http_client(httpx.MockTransport(handler), max_attempts=3).get(FEED_URL)
        assert response.body == b"<rss>recovered</rss>"
        assert attempts["n"] == 2

    def test_timeouts_are_retried(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, content=b"<rss/>")

        make_http_client(httpx.MockTransport(handler), max_attempts=3).get(FEED_URL)
        assert attempts["n"] == 3

    def test_retries_can_be_disabled(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(503)

        client = make_http_client(httpx.MockTransport(handler), max_attempts=1)
        with pytest.raises(HttpStatusError):
            client.get(FEED_URL)
        assert attempts["n"] == 1


class TestSizeLimits:
    def test_oversized_declared_length_is_refused_without_downloading(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"x" * 10, headers={"content-length": "999999999"}
            )

        client = make_http_client(httpx.MockTransport(handler), max_bytes=1000)
        with pytest.raises(ResponseTooLargeError, match="declares"):
            client.get(FEED_URL)

    def test_oversized_streamed_body_is_refused(self) -> None:
        """A server that omits or lies about Content-Length must not blow up memory."""

        def handler(request: httpx.Request) -> httpx.Response:
            def stream() -> object:
                for _ in range(10):
                    yield b"x" * 500

            return httpx.Response(200, content=stream())

        client = make_http_client(httpx.MockTransport(handler), max_bytes=1000)
        with pytest.raises(ResponseTooLargeError, match="exceeded"):
            client.get(FEED_URL)

    def test_a_decompression_bomb_is_bounded(self) -> None:
        """Highly compressible payloads are capped on the decompressed size."""
        payload = gzip.compress(b"a" * (5 * 1024 * 1024))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=payload, headers={"content-encoding": "gzip"})

        client = make_http_client(httpx.MockTransport(handler), max_bytes=64 * 1024)
        with pytest.raises(ResponseTooLargeError):
            client.get(FEED_URL)

    def test_body_within_the_cap_is_returned(self) -> None:
        client = make_http_client(static_transport(b"x" * 500), max_bytes=1000)
        assert len(client.get(FEED_URL).body) == 500


class TestLifecycle:
    def test_works_as_a_context_manager(self) -> None:
        with make_http_client(static_transport(b"<rss/>")) as client:
            assert client.get(FEED_URL).status_code == 200

    def test_connection_is_reused_across_requests(self) -> None:
        client = make_http_client(static_transport(b"<rss/>"))
        assert client.get(FEED_URL).status_code == 200
        assert client.get(FEED_URL).status_code == 200
        client.close()

    def test_defaults_are_sane(self) -> None:
        client = HttpClient()
        try:
            assert "AiNewsEditorBot" in DEFAULT_USER_AGENT
        finally:
            client.close()
