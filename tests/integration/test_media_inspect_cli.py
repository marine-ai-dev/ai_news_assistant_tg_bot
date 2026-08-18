"""``ai-news media inspect`` — Step 4 section 33. No real network, no Telegram."""

from __future__ import annotations

import io
import socket

import httpx
import pytest
from PIL import Image
from typer.testing import CliRunner

from ai_news_editor.cli.main import app
from ai_news_editor.sources.http import HttpClient

runner = CliRunner()


@pytest.fixture(autouse=True)
def _resolve_invalid_hostnames_as_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """``*.example.invalid`` never actually resolves (RFC 2606) — media.urlsafety's
    real DNS check would otherwise reject the media URL as unresolvable."""
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))]
    )

ARTICLE_URL = "https://blog.example.invalid/posts/1"
IMAGE_URL = "https://blog.example.invalid/hero.jpg"

_ARTICLE_HTML = f"""
<html><head>
<meta property="og:image" content="{IMAGE_URL}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
</head><body>Article</body></html>
"""


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "red").save(buf, format="JPEG")
    return buf.getvalue()


def output_of(result: object) -> str:
    import contextlib

    parts = [getattr(result, "output", "") or ""]
    with contextlib.suppress(AttributeError, ValueError):
        parts.append(result.stderr or "")  # type: ignore[attr-defined]
    return " ".join(" ".join(parts).split())


class TestMediaInspectCommand:
    def test_discovers_and_reports_media_without_calling_telegram(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        image_body = _jpeg_bytes()

        def article_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=_ARTICLE_HTML.encode()
            )

        def fake_client(**kwargs: object) -> HttpClient:  # type: ignore[no-untyped-def]
            return HttpClient(transport=httpx.MockTransport(article_handler))

        monkeypatch.setattr("ai_news_editor.cli.media.HttpClient", fake_client)

        def media_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=image_body)

        import ai_news_editor.media.pipeline as pipeline_module

        original_select = pipeline_module.select_media

        def select_with_mock_transport(**kwargs: object):  # type: ignore[no-untyped-def]
            kwargs["transport"] = httpx.MockTransport(media_handler)
            return original_select(**kwargs)

        monkeypatch.setattr("ai_news_editor.cli.media.select_media", select_with_mock_transport)

        result = runner.invoke(app, ["media", "inspect", ARTICLE_URL])

        assert result.exit_code == 0
        output = output_of(result)
        assert "IMAGE" in output
        assert "OPEN_GRAPH_IMAGE" in output
        assert "removed after this command exits" in output

    def test_no_media_discovered_is_reported_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def article_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html><head></head><body>no media here</body></html>",
            )

        def fake_client(**kwargs: object) -> HttpClient:  # type: ignore[no-untyped-def]
            return HttpClient(transport=httpx.MockTransport(article_handler))

        monkeypatch.setattr("ai_news_editor.cli.media.HttpClient", fake_client)

        result = runner.invoke(app, ["media", "inspect", ARTICLE_URL])

        assert result.exit_code == 0
        assert "No media selected" in output_of(result)

    def test_a_fetch_failure_exits_nonzero_with_a_clear_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def article_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        def fake_client(**kwargs: object) -> HttpClient:  # type: ignore[no-untyped-def]
            return HttpClient(transport=httpx.MockTransport(article_handler))

        monkeypatch.setattr("ai_news_editor.cli.media.HttpClient", fake_client)

        result = runner.invoke(app, ["media", "inspect", ARTICLE_URL])

        assert result.exit_code == 2
        assert "Could not fetch" in output_of(result)
