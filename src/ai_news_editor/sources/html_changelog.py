"""Config-driven HTML changelog adapter.

Several consumer-AI products publish no usable feed, so their release notes and
newsroom pages have to be read directly. This adapter exists for exactly that, and it
is driven entirely by CSS selectors declared in ``config/sources.yaml`` — there is no
per-site Python. Adding a vendor is a config entry, not a new class.

Two deliberate constraints:

*Only listing pages.* It reads the entries a changelog or newsroom page already shows.
It never follows links to fetch individual article bodies, because that is the first
step towards a general-purpose crawler.

*Fail visibly.* Selectors break when a site is redesigned. A source that previously
produced entries and now yields none reports an error rather than quietly succeeding
with nothing, so breakage surfaces instead of looking like a quiet news day.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field
from selectolax.parser import HTMLParser, Node

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import FetchOutcome, SourceKind
from ai_news_editor.domain.models import RawItem, Source
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.pipeline.text import clean_text, clean_title
from ai_news_editor.sources.base import FetchContext, FetchResult
from ai_news_editor.sources.http import HttpClient, HttpError, HttpStatusError, UnsafeUrlError

logger = get_logger(__name__)

CONTENT_TYPE = "text/html"

#: Token meaning "the matched item element itself" rather than a descendant.
SELF = ":self"

#: Date formats accepted from page markup, tried in order. A small explicit list keeps
#: parsing deterministic and avoids a date-guessing dependency.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d %Y",
    "%B %d %Y",
)


class HtmlChangelogOptions(BaseModel):
    """Selector configuration for one HTML source.

    Strictly validated: a malformed selector block should stop the run rather than
    silently collect nothing.
    """

    model_config = ConfigDict(extra="forbid")

    #: Selects each entry on the listing page.
    item_selector: str = Field(min_length=1)
    #: Where the title text lives inside an item. Defaults to the item's own text.
    title_selector: str | None = None
    #: Element carrying the link. ``:self`` when the item element is itself the anchor.
    link_selector: str = SELF
    link_attribute: str = "href"
    #: Optional date element; the attribute is preferred, falling back to its text.
    date_selector: str | None = None
    date_attribute: str = "datetime"
    #: Optional regex with one group, used when the date lives in the URL instead.
    date_from_url: str | None = None
    #: Optional summary element inside the item.
    summary_selector: str | None = None
    #: A source that previously produced entries and now yields fewer than this is
    #: reporting breakage, not a quiet week.
    min_expected_items: int = Field(default=1, ge=0)


class HtmlChangelogAdapter:
    """Reads entries from an HTML listing or changelog page."""

    kind: ClassVar[SourceKind] = SourceKind.HTML_CHANGELOG

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def fetch(self, source: Source, context: FetchContext) -> FetchResult:
        try:
            options = HtmlChangelogOptions.model_validate(source.config)
        except Exception as exc:
            return FetchResult.failed(source.id, f"invalid html_changelog options: {exc}")

        try:
            response = self._http.get(
                source.url, etag=context.etag, last_modified=context.last_modified
            )
        except HttpStatusError as exc:
            return FetchResult.failed(source.id, str(exc), http_status=exc.status_code)
        except (HttpError, UnsafeUrlError) as exc:
            return FetchResult.failed(source.id, f"{type(exc).__name__}: {exc}")

        if response.not_modified:
            return FetchResult.not_modified(
                source.id,
                etag=response.etag or context.etag,
                last_modified=response.last_modified or context.last_modified,
            )

        return self._parse(source, context, options, response)

    def _parse(
        self,
        source: Source,
        context: FetchContext,
        options: HtmlChangelogOptions,
        response: Any,
    ) -> FetchResult:
        try:
            tree = HTMLParser(response.body.decode("utf-8", errors="replace"))
        except Exception as exc:
            return FetchResult.failed(
                source.id, f"could not parse HTML: {exc}", http_status=response.status_code
            )

        nodes = tree.css(options.item_selector)
        if len(nodes) < options.min_expected_items:
            # The loud failure that matters: the page loaded fine but our selectors no
            # longer match it. Reporting success here would hide a broken source for
            # weeks. The selector is included so the failure is diagnosable without
            # storing the whole page.
            return FetchResult.failed(
                source.id,
                f"selector {options.item_selector!r} matched {len(nodes)} items, expected at "
                f"least {options.min_expected_items} — the page structure has probably changed",
                http_status=response.status_code,
            )

        warnings: list[str] = []
        fetched_at = now_utc()
        items: list[RawItem] = []
        for node in nodes[: context.max_items]:
            item = self._to_raw_item(source, context, options, node, fetched_at, warnings)
            if item is not None:
                items.append(item)

        if not items:
            return FetchResult.failed(
                source.id,
                f"{len(nodes)} items matched but none yielded a usable link",
                http_status=response.status_code,
            )

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
        options: HtmlChangelogOptions,
        node: Node,
        fetched_at: datetime,
        warnings: list[str],
    ) -> RawItem | None:
        href = _extract_attr(node, options.link_selector, options.link_attribute)
        link = _absolute(href, source.url)
        if not link:
            warnings.append("skipped an item with no link")
            return None

        title = clean_title(_extract_text(node, options.title_selector))
        if not title:
            warnings.append(f"skipped an item with no title: {link}")
            return None

        summary = (
            clean_text(_extract_text(node, options.summary_selector))
            if options.summary_selector
            else None
        )
        published = _extract_date(node, options, link)

        return RawItem(
            source_id=source.id,
            # The entry URL is the stable identity: changelog pages carry no guid.
            external_id=link,
            title_original=title,
            url_original=link,
            author=None,
            published_at=published,
            fetched_at=fetched_at,
            summary_raw=summary,
            content_raw=None,
            payload_raw=json.dumps(
                {"title": title, "link": link, "summary": summary,
                 "published": published.isoformat() if published else None},
                ensure_ascii=False,
                sort_keys=True,
            ),
            content_type=CONTENT_TYPE,
            fetch_run_id=context.run_id,
        )


def _select(node: Node, selector: str | None) -> Node | None:
    if selector is None:
        return node
    if selector == SELF:
        return node
    found = node.css(selector)
    return found[0] if found else None


def _extract_text(node: Node, selector: str | None) -> str | None:
    target = _select(node, selector)
    if target is None:
        return None
    text = target.text(separator=" ")
    return text or None


def _extract_attr(node: Node, selector: str, attribute: str) -> str | None:
    target = _select(node, selector)
    if target is None:
        return None
    value = target.attributes.get(attribute)
    return value.strip() if value else None


def _absolute(link: str | None, base_url: str) -> str | None:
    """Resolve a page-relative link against the listing page URL."""
    if not link:
        return None
    if link.startswith(("http://", "https://")):
        return link
    from urllib.parse import urljoin

    return urljoin(base_url, link)


def _extract_date(node: Node, options: HtmlChangelogOptions, link: str) -> datetime | None:
    """Read the entry date, from markup or from the URL. Never guessed."""
    if options.date_from_url:
        match = re.search(options.date_from_url, link)
        if match:
            parsed = _parse_date(match.group(1))
            if parsed:
                return parsed

    if options.date_selector:
        target = _select(node, options.date_selector)
        if target is not None:
            raw = target.attributes.get(options.date_attribute) or target.text(separator=" ")
            parsed = _parse_date(raw)
            if parsed:
                return parsed
    return None


def _parse_date(value: str | None) -> datetime | None:
    """Parse a date string into aware UTC, or ``None`` if no known format matches."""
    if not value:
        return None
    text = " ".join(value.split()).strip().rstrip(",")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
