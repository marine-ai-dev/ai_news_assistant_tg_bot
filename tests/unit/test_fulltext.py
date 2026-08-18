"""sources.fulltext.fetch_fulltext — specifically the status_code it reports on
failure, which is what automation.pipeline's domain-cooldown logic keys off."""

from __future__ import annotations

import httpx
import pytest

from ai_news_editor.sources.fulltext import fetch_fulltext
from ai_news_editor.sources.http import HttpClient

URL = "https://example.invalid/article"

ARTICLE_HTML = (
    "<html><body><article><p>" + ("Це стаття з достатньою кількістю тексту. " * 40)
    + "</p></article></body></html>"
)


def _client(status: int, *, body: bytes = b"") -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": "text/html"})

    return HttpClient(transport=httpx.MockTransport(handler), retry_backoff_seconds=0)


@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 500, 503])
def test_a_failing_status_is_reported_on_the_result(status: int) -> None:
    result = fetch_fulltext(URL, http=_client(status))
    assert result.ok is False
    assert result.status_code == status


def test_a_successful_fetch_has_no_status_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=ARTICLE_HTML.encode(), headers={"content-type": "text/html"}
        )

    client = HttpClient(transport=httpx.MockTransport(handler), retry_backoff_seconds=0)
    result = fetch_fulltext(URL, http=client)
    assert result.ok is True
    assert result.status_code is None


def test_a_content_only_failure_has_no_status_code() -> None:
    """Too-short text is a candidate-specific problem, not an HTTP failure — nothing
    for automation.pipeline to key a domain cooldown off."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html><body><p>too short</p></body></html>",
                               headers={"content-type": "text/html"})

    client = HttpClient(transport=httpx.MockTransport(handler), retry_backoff_seconds=0)
    result = fetch_fulltext(URL, http=client)
    assert result.ok is False
    assert result.status_code is None
    assert "too short" in (result.reason or "")


def test_a_transport_failure_has_no_status_code() -> None:
    """A DNS/connection failure never reached a real HTTP response at all."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = HttpClient(transport=httpx.MockTransport(handler), retry_backoff_seconds=0)
    result = fetch_fulltext(URL, http=client)
    assert result.ok is False
    assert result.status_code is None
