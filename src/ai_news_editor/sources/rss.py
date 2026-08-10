"""RSS and Atom adapter.

Parsing delegates to ``feedparser``, which handles the long tail of real-world feed
dialects. This module's job is the mapping from a parsed entry to a :class:`RawItem`
with honest provenance: present fields are recorded, absent fields stay ``None``.
Nothing is invented to make a record look complete.

Feed content is data, never instruction. Text is stored verbatim and interpreted by no
one at this layer; an entry telling the system to do something is just a string.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any, ClassVar

import feedparser

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import FetchOutcome, SourceKind
from ai_news_editor.domain.models import RawItem, Source
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.sources.base import FetchContext, FetchResult
from ai_news_editor.sources.http import (
    HttpClient,
    HttpError,
    HttpStatusError,
    UnsafeUrlError,
)

logger = get_logger(__name__)

CONTENT_TYPE = "application/rss+xml"


class RssAdapter:
    """Fetches and parses an RSS or Atom feed."""

    kind: ClassVar[SourceKind] = SourceKind.RSS

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def fetch(self, source: Source, context: FetchContext) -> FetchResult:
        """Read the feed. Returns a typed result; never raises for I/O failures."""
        try:
            response = self._http.get(
                source.url, etag=context.etag, last_modified=context.last_modified
            )
        except HttpStatusError as exc:
            return FetchResult.failed(source.id, str(exc), http_status=exc.status_code)
        except (HttpError, UnsafeUrlError) as exc:
            return FetchResult.failed(source.id, f"{type(exc).__name__}: {exc}")

        if response.not_modified:
            # The server confirmed nothing changed. Keep the validators we already had:
            # a 304 usually echoes no ETag of its own.
            return FetchResult.not_modified(
                source.id,
                etag=response.etag or context.etag,
                last_modified=response.last_modified or context.last_modified,
            )

        return self._parse(source, context, response.body, response)

    def _parse(
        self,
        source: Source,
        context: FetchContext,
        body: bytes,
        response: Any,
    ) -> FetchResult:
        if not body.strip():
            # An empty 200 is a failure, not "a feed with no news". Reporting zero
            # items would make an outage look like a quiet day.
            return FetchResult.failed(
                source.id, "empty response body", http_status=response.status_code
            )

        parsed = feedparser.parse(body)
        warnings: list[str] = []

        # feedparser flags anything non-strict as "bozo", including feeds that parse
        # perfectly well, so bozo alone is not a failure — nor is an empty feed, which
        # is a legitimate quiet day. The failure case is *no entries* combined with
        # either an unrecognised format (we were served an error page) or a parse
        # error (the document was truncated mid-stream).
        if not parsed.entries and (parsed.bozo or not parsed.get("version")):
            reason = getattr(parsed, "bozo_exception", None) or "not a recognisable feed"
            return FetchResult.failed(
                source.id,
                f"could not parse feed: {reason}",
                http_status=response.status_code,
            )
        if parsed.bozo:
            warnings.append(f"feed is not strictly valid: {getattr(parsed, 'bozo_exception', '')}")

        fetched_at = now_utc()
        items: list[RawItem] = []
        for entry in parsed.entries[: context.max_items]:
            item = self._to_raw_item(source, context, entry, fetched_at, warnings)
            if item is not None:
                items.append(item)

        return FetchResult(
            source_id=source.id,
            outcome=FetchOutcome.OK,
            items=tuple(items),
            etag=response.etag,
            last_modified=response.last_modified,
            http_status=response.status_code,
            warnings=tuple(warnings),
        )

    def _to_raw_item(
        self,
        source: Source,
        context: FetchContext,
        entry: Any,
        fetched_at: datetime,
        warnings: list[str],
    ) -> RawItem | None:
        link = _clean(entry.get("link"))
        if not link:
            warnings.append(f"skipped an entry with no link: {_clean(entry.get('title')) or '?'}")
            return None

        return RawItem(
            source_id=source.id,
            external_id=_external_id(entry, link),
            title_original=_clean(entry.get("title")),
            url_original=link,
            author=_author(entry),
            published_at=_published_at(entry),
            fetched_at=fetched_at,
            summary_raw=_clean(entry.get("summary")),
            content_raw=_content(entry),
            payload_raw=_payload(entry),
            content_type=CONTENT_TYPE,
            fetch_run_id=context.run_id,
        )


def _clean(value: object) -> str | None:
    """Normalize a feed string, treating blank as absent."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _external_id(entry: Any, link: str) -> str:
    """A stable per-entry identity for ingestion idempotency.

    Prefers the feed's own guid/id. When a feed supplies none, derives a deterministic
    id from the entry link so that re-reading the same feed does not create duplicates.
    The ``derived:`` prefix keeps it obvious which ids we invented.
    """
    for key in ("id", "guid"):
        candidate = _clean(entry.get(key))
        if candidate:
            return candidate
    digest = hashlib.sha256(link.encode("utf-8")).hexdigest()
    return f"derived:{digest[:40]}"


def _author(entry: Any) -> str | None:
    author = _clean(entry.get("author"))
    if author:
        return author
    detail = entry.get("author_detail")
    if isinstance(detail, dict):
        return _clean(detail.get("name"))
    return None


def _published_at(entry: Any) -> datetime | None:
    """Convert a feed timestamp to aware UTC, or ``None`` when the feed omits one.

    A missing publication time is left missing: inventing one would corrupt ordering
    and, later, the recency signals the editorial layer depends on. ``fetched_at``
    records when *we* saw the entry and stays semantically distinct.

    feedparser normalizes any timezone it recognises to UTC in ``*_parsed``, so the
    original offset is honoured rather than assumed.
    """
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
            except (ValueError, OverflowError, TypeError):
                continue
    return None


def _content(entry: Any) -> str | None:
    """The full entry body when the feed inlines one.

    Only what the feed itself provided — fetching the linked page is out of scope.
    """
    content = entry.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            return _clean(first.get("value"))
    return None


def _payload(entry: Any) -> str:
    """A faithful JSON record of the parsed entry, for provenance.

    feedparser does not expose the raw XML slice for an individual entry, so this
    preserves every field it extracted instead. Non-serializable values (notably
    ``time.struct_time``) are converted rather than dropped.
    """
    return json.dumps(_jsonable(entry), ensure_ascii=False, sort_keys=True, default=str)


def _jsonable(value: Any) -> Any:
    # struct_time is checked first because it subclasses tuple: the sequence branch
    # would otherwise turn a timestamp into a bare list of integers.
    if isinstance(value, time.struct_time):
        return datetime.fromtimestamp(calendar.timegm(value), tz=UTC).isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)
