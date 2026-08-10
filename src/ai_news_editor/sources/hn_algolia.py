"""Hacker News community-signal adapter (Algolia API).

Hacker News answers one question for this product: *is the tech community suddenly
discussing this?* It never answers *is this true?*

That distinction is structural, not advisory. The source is configured
``COMMUNITY_SIGNAL`` and ``signal_only``, which means the processing pipeline refuses
to derive articles from it. Its records become :class:`CommunitySignal` rows attached
to articles we already hold — attention metadata beside a story, never the story.
Comments are never read, and no HN text becomes article content.

Scope is bounded on purpose: a handful of AI-related queries over a recent time window,
each capped. Downloading the HN corpus would be both rude and useless.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import FetchOutcome, SourceKind
from ai_news_editor.domain.models import RawItem, Source
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.sources.base import FetchContext, FetchResult
from ai_news_editor.sources.http import HttpClient, HttpError, HttpStatusError, UnsafeUrlError

logger = get_logger(__name__)

CONTENT_TYPE = "application/json"
DISCUSSION_URL = "https://news.ycombinator.com/item?id={object_id}"


class HnSignalOptions(BaseModel):
    """Bounded query configuration for the HN signal reader."""

    model_config = ConfigDict(extra="forbid")

    #: Search terms. Each becomes one bounded request.
    queries: tuple[str, ...] = ("artificial intelligence", "AI")
    #: How far back to look. Community attention is only interesting while it is fresh.
    window_hours: int = Field(default=48, ge=1, le=24 * 14)
    #: Results per query. Small: this is a signal sample, not a crawl.
    hits_per_query: int = Field(default=50, ge=1, le=200)
    #: Ignore stories with fewer points than this — noise below a few upvotes.
    min_points: int = Field(default=2, ge=0)


class HnSignalAdapter:
    """Reads recent Hacker News discussions as community signal."""

    kind: ClassVar[SourceKind] = SourceKind.HN_SIGNAL

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def fetch(self, source: Source, context: FetchContext) -> FetchResult:
        try:
            options = HnSignalOptions.model_validate(source.config)
        except Exception as exc:
            return FetchResult.failed(source.id, f"invalid hn_signal options: {exc}")

        since = int((now_utc() - timedelta(hours=options.window_hours)).timestamp())
        fetched_at = now_utc()
        items: dict[str, RawItem] = {}
        warnings: list[str] = []
        last_status: int | None = None

        for query in options.queries:
            url = (
                f"{source.url}?query={_quote(query)}&tags=story"
                f"&numericFilters=created_at_i>{since},points>={options.min_points}"
                f"&hitsPerPage={options.hits_per_query}"
            )
            try:
                response = self._http.get(
                    url, etag=context.etag, last_modified=context.last_modified
                )
            except HttpStatusError as exc:
                return FetchResult.failed(source.id, str(exc), http_status=exc.status_code)
            except (HttpError, UnsafeUrlError) as exc:
                return FetchResult.failed(source.id, f"{type(exc).__name__}: {exc}")

            last_status = response.status_code
            if response.not_modified:
                # The search endpoint rarely sends validators, but the contract is the
                # same for every adapter: 304 is a successful check with nothing new.
                return FetchResult.not_modified(
                    source.id,
                    etag=response.etag or context.etag,
                    last_modified=response.last_modified or context.last_modified,
                )
            try:
                payload = json.loads(response.body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                return FetchResult.failed(
                    source.id, f"invalid JSON from HN API: {exc}", http_status=last_status
                )
            if not isinstance(payload, dict) or "hits" not in payload:
                return FetchResult.failed(
                    source.id, "unexpected HN API response shape", http_status=last_status
                )

            for hit in payload.get("hits") or []:
                item = _to_raw_item(source, context, hit, fetched_at, warnings)
                if item is not None and item.external_id not in items:
                    items[str(item.external_id)] = item

        return FetchResult(
            source_id=source.id,
            outcome=FetchOutcome.OK,
            items=tuple(items.values()),
            http_status=last_status,
            warnings=tuple(warnings),
        )


def _quote(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


def _to_raw_item(
    source: Source,
    context: FetchContext,
    hit: Any,
    fetched_at: datetime,
    warnings: list[str],
) -> RawItem | None:
    if not isinstance(hit, dict):
        return None
    object_id = hit.get("objectID")
    if not object_id:
        return None

    # A story with no outbound link is an Ask HN or a self post: pure discussion, which
    # cannot be matched to an article and is not a signal about any external story.
    url = hit.get("url")
    if not url or not isinstance(url, str):
        return None

    title = hit.get("title")
    if not isinstance(title, str) or not title.strip():
        warnings.append(f"skipped HN story {object_id} with no title")
        return None

    return RawItem(
        source_id=source.id,
        external_id=str(object_id),
        title_original=title.strip(),
        url_original=url,
        author=hit.get("author") if isinstance(hit.get("author"), str) else None,
        published_at=_created_at(hit),
        fetched_at=fetched_at,
        # Deliberately no summary or content: HN supplies discussion, not article text,
        # and none of it may become article content.
        summary_raw=None,
        content_raw=None,
        payload_raw=json.dumps(hit, ensure_ascii=False, sort_keys=True, default=str),
        content_type=CONTENT_TYPE,
        fetch_run_id=context.run_id,
    )


def _created_at(hit: dict[str, Any]) -> datetime | None:
    epoch = hit.get("created_at_i")
    if isinstance(epoch, int | float):
        return datetime.fromtimestamp(float(epoch), tz=UTC)
    raw = hit.get("created_at")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def signal_fields(payload: str) -> dict[str, Any]:
    """Pull the attention metrics out of a stored HN payload.

    Only counts and identifiers — never comment text.
    """
    try:
        hit = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(hit, dict):
        return {}
    return {
        "points": hit.get("points") if isinstance(hit.get("points"), int) else None,
        "num_comments": hit.get("num_comments")
        if isinstance(hit.get("num_comments"), int)
        else None,
        "discussion_url": DISCUSSION_URL.format(object_id=hit.get("objectID"))
        if hit.get("objectID")
        else None,
    }
