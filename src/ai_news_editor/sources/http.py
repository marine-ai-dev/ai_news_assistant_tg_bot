"""The single outbound HTTP boundary.

Every request the application makes goes through :class:`HttpClient`. Centralising it
means timeouts, size caps, the User-Agent and URL safety checks are properties of the
application rather than things each adapter remembers to do.

Trust assumption: ``config/sources.yaml`` is local and operator-controlled, so this is
not a hardened SSRF gateway. It rejects non-HTTP schemes and literal private/loopback
addresses — enough to catch a mistyped config — but it does not resolve DNS to check
where a public hostname points. Treat the source config as trusted input.
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field
from types import TracebackType
from urllib.parse import urlsplit

import httpx

from ai_news_editor import __version__
from ai_news_editor.domain.errors import FatalError, RetryableError
from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_USER_AGENT = (
    f"AiNewsEditorBot/{__version__} "
    "(+https://github.com/MarineCinnamon/ai-news-editor; RSS reader for a Telegram channel)"
)
DEFAULT_TIMEOUT_SECONDS = 20.0
#: 12 MB. The largest real feed we read is ~700 KB, so this is generous headroom while
#: still refusing to buffer something pathological into memory.
DEFAULT_MAX_BYTES = 12 * 1024 * 1024
DEFAULT_MAX_ATTEMPTS = 3

_ALLOWED_SCHEMES = frozenset({"http", "https"})
#: Retried because they are plausibly transient. 4xx is never retried: a 404 or 403
#: will not fix itself, and retrying is just rude.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpError(RetryableError):
    """Base for outbound HTTP failures."""


class HttpTimeoutError(HttpError):
    """The request exceeded its timeout budget."""


class HttpTransportError(HttpError):
    """Connection-level failure: DNS, TLS, refused connection."""


class HttpStatusError(HttpError):
    """The server returned an unsuccessful status."""

    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"HTTP {status_code} from {url}")
        self.status_code = status_code
        self.url = url


class ResponseTooLargeError(HttpError):
    """The response exceeded the configured size cap."""


class UnsafeUrlError(FatalError):
    """The URL is not something this application is willing to request.

    Fatal rather than retryable: a bad URL is a configuration mistake, and retrying
    cannot help.
    """


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A completed response, already read into memory."""

    status_code: int
    url: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304


def validate_url(url: str) -> None:
    """Reject URLs this application will not fetch.

    Raises:
        UnsafeUrlError: non-HTTP scheme, missing host, or a literal private address.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"only http and https are allowed, got {parts.scheme!r}: {url}")
    if not parts.hostname:
        raise UnsafeUrlError(f"URL has no host: {url}")

    hostname = parts.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeUrlError(f"refusing to fetch a loopback address: {url}")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return  # A regular hostname; DNS is deliberately not resolved here.

    if address.is_loopback or address.is_private or address.is_link_local or address.is_reserved:
        raise UnsafeUrlError(f"refusing to fetch a private or loopback address: {url}")


class HttpClient:
    """A small, reusable HTTP client for reading feeds.

    Connections are pooled across requests. Pass ``transport`` to drive it from tests
    with :class:`httpx.MockTransport` — no monkeypatching required.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_attempts = max(1, max_attempts)
        self._retry_backoff_seconds = retry_backoff_seconds
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=5,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9,"
                " text/xml;q=0.9, */*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
            },
            transport=transport,
        )

    def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> HttpResponse:
        """GET a URL, optionally as a conditional request.

        Raises:
            UnsafeUrlError: the URL failed validation (not retried).
            HttpTimeoutError, HttpTransportError, HttpStatusError, ResponseTooLargeError.
        """
        validate_url(url)

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error: HttpError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._attempt(url, headers)
            except (HttpTimeoutError, HttpTransportError) as exc:
                last_error = exc
            except HttpStatusError as exc:
                if exc.status_code not in _RETRYABLE_STATUS:
                    raise
                last_error = exc

            if attempt < self._max_attempts:
                delay = self._retry_backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "retrying request",
                    extra={"url": url, "attempt": attempt, "reason": str(last_error)},
                )
                if delay > 0:
                    time.sleep(delay)

        assert last_error is not None  # loop always records one before exhausting
        raise last_error

    def _attempt(self, url: str, headers: dict[str, str]) -> HttpResponse:
        try:
            with self._client.stream("GET", url, headers=headers) as response:
                declared = response.headers.get("content-length")
                if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
                    raise ResponseTooLargeError(
                        f"{url} declares {declared} bytes, over the {self._max_bytes} limit"
                    )

                if response.status_code == 304:
                    return HttpResponse(
                        status_code=304,
                        url=str(response.url),
                        body=b"",
                        headers=_lower(response.headers),
                    )

                if response.status_code >= 400:
                    raise HttpStatusError(response.status_code, url)

                body = self._read_capped(response, url)
                return HttpResponse(
                    status_code=response.status_code,
                    url=str(response.url),
                    body=body,
                    headers=_lower(response.headers),
                )
        except httpx.TimeoutException as exc:
            raise HttpTimeoutError(f"timeout fetching {url}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise HttpTransportError(f"transport error fetching {url}: {exc}") from exc

    def _read_capped(self, response: httpx.Response, url: str) -> bytes:
        """Read the body, aborting if it grows past the cap.

        Streamed rather than read whole, so a server that lies about (or omits)
        Content-Length cannot make us buffer an unbounded body. httpx decompresses as
        it iterates, so this also bounds a decompression bomb.
        """
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self._max_bytes:
                raise ResponseTooLargeError(
                    f"{url} exceeded the {self._max_bytes} byte limit"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _lower(headers: httpx.Headers) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}
