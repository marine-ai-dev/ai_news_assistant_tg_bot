"""Safe, streaming media download — Step 4 (AI News Agent v2).

Deliberately not ``sources.http.HttpClient``: that client buffers a whole response into
memory (fine for a feed, wrong for a 10-50 MB photo/video) and follows redirects
automatically via httpx, which would skip this module's own redirect-by-redirect SSRF
re-validation. Every design choice here is about handling bytes this application does
not yet trust: bounded timeouts, a hard size cap enforced while streaming (not just
read once and measured after), Content-Type checked before the body is trusted to be
what it claims, and redirects followed one hop at a time so a private-network hop
cannot hide behind an initially-safe URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from ai_news_editor.domain.errors import FatalError, RetryableError
from ai_news_editor.media.limits import (
    DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
    DOWNLOAD_READ_TIMEOUT_SECONDS,
    MAX_DOWNLOAD_BYTES,
    MAX_DOWNLOAD_REDIRECTS,
)
from ai_news_editor.media.urlsafety import validate_media_url
from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

_USER_AGENT = "AiNewsEditorBot-media (+https://github.com/marine-ai-dev/ai_news_assistant_tg_bot)"

#: Accepted Content-Type prefixes, by the media kind the caller asked for. A response
#: whose declared type does not start with one of these is rejected before its body is
#: ever read — this is the guard against "HTML pretending to be image/video" (a login
#: wall, an error page, a redirect page served with a 200).
_ALLOWED_CONTENT_TYPE_PREFIXES = {
    "image": ("image/",),
    "video": ("video/",),
}


class MediaDownloadError(RetryableError):
    """A transient download failure — timeout, connection error, bad status."""


class MediaTooLargeError(FatalError):
    """The response is, or became, larger than the configured limit."""


class InvalidContentTypeError(FatalError):
    """The response's declared Content-Type does not match the expected media kind."""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    content_type: str
    size_bytes: int


def download_media(
    url: str,
    dest: Path,
    *,
    kind: str,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    transport: httpx.BaseTransport | None = None,
    max_redirects: int = MAX_DOWNLOAD_REDIRECTS,
) -> DownloadResult:
    """Download ``url`` to the local file ``dest``, or raise why it refused to.

    ``kind`` is ``"image"`` or ``"video"`` — the Content-Type prefix the response must
    match. Redirects are followed manually, one hop at a time, re-validating each new
    URL against the same SSRF rules as the original (see ``media.urlsafety``) — httpx's
    own ``follow_redirects=True`` would skip that re-check entirely.

    Raises:
        UnsafeMediaUrlError: the URL, or a redirect target, fails SSRF validation.
        MediaDownloadError: timeout, connection failure, or a non-2xx/3xx status.
        MediaTooLargeError: the declared or actual size exceeds ``max_bytes``.
        InvalidContentTypeError: the response's Content-Type does not match ``kind``.
    """
    allowed_prefixes = _ALLOWED_CONTENT_TYPE_PREFIXES[kind]
    current_url = url

    with httpx.Client(
        transport=transport,
        follow_redirects=False,
        timeout=httpx.Timeout(
            connect=DOWNLOAD_CONNECT_TIMEOUT_SECONDS, read=DOWNLOAD_READ_TIMEOUT_SECONDS,
            write=DOWNLOAD_CONNECT_TIMEOUT_SECONDS, pool=DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
        ),
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        for hop in range(max_redirects + 1):
            validate_media_url(current_url)
            try:
                with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location or hop == max_redirects:
                            raise MediaDownloadError(
                                f"too many redirects fetching {url} (stopped at {current_url})"
                            )
                        current_url = str(response.url.join(location))
                        continue

                    if response.status_code >= 400:
                        raise MediaDownloadError(f"HTTP {response.status_code} from {current_url}")

                    content_type = response.headers.get("content-type", "").split(";")[0].strip()
                    if not content_type.startswith(allowed_prefixes):
                        raise InvalidContentTypeError(
                            f"{current_url} declared Content-Type {content_type or '(none)'!r}, "
                            f"expected one starting with {allowed_prefixes}"
                        )

                    declared = response.headers.get("content-length")
                    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                        raise MediaTooLargeError(
                            f"{current_url} declares {declared} bytes, over the {max_bytes} "
                            "byte limit"
                        )

                    size = _stream_to_file(response, dest, max_bytes, current_url)
                    logger.info(
                        "media_download",
                        extra={"bytes": size, "content_type": content_type},
                    )
                    return DownloadResult(path=dest, content_type=content_type, size_bytes=size)
            except httpx.TimeoutException as exc:
                raise MediaDownloadError(f"timeout fetching {current_url}: {exc}") from exc
            except httpx.HTTPError as exc:
                raise MediaDownloadError(f"transport error fetching {current_url}: {exc}") from exc

    raise MediaDownloadError(f"too many redirects fetching {url}")  # pragma: no cover - unreachable


def _stream_to_file(response: httpx.Response, dest: Path, max_bytes: int, url: str) -> int:
    """Write the body to ``dest`` while streaming, aborting the moment it exceeds
    ``max_bytes`` — a lying or absent Content-Length never causes an unbounded write,
    and a partial file left by an aborted download is removed rather than kept."""
    total = 0
    try:
        with dest.open("wb") as handle:
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise MediaTooLargeError(f"{url} exceeded the {max_bytes} byte limit")
                handle.write(chunk)
    except MediaTooLargeError:
        dest.unlink(missing_ok=True)
        raise
    return total


__all__ = [
    "DownloadResult",
    "InvalidContentTypeError",
    "MediaDownloadError",
    "MediaTooLargeError",
    "download_media",
]
