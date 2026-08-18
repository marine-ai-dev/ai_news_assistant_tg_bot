"""Conservative, structured media discovery — Step 4 (AI News Agent v2).

Every candidate this module returns traces back to metadata that names an image or a
video *explicitly* — a feed enclosure, an Open Graph tag, a schema.org property. There
is no browser automation, no DOM scraping, no "first ``<img>`` on the page" heuristic:
those pick up avatars, logos, tracking pixels and unrelated recommendation thumbnails
as often as they pick up the actual hero image, and this module exists specifically to
not do that.

Pure functions over already-fetched data. Fetching the article's HTML (if a caller
wants Open Graph / JSON-LD discovery in addition to feed-enclosure discovery) is the
caller's job, via the existing trusted ``sources.http.HttpClient`` — this module never
makes a network request itself.
"""

from __future__ import annotations

import json
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ai_news_editor.media.limits import MIN_CANDIDATE_HEIGHT, MIN_CANDIDATE_WIDTH
from ai_news_editor.media.models import DiscoveryMethod, MediaCandidate, MediaKind
from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

#: A source's own image/video field naming this exact substring is treated as *not* a
#: hero image, even though it is structurally an enclosure/og-tag — the conservative
#: reading of section 7's "do not download avatars, logos, tracking pixels": some
#: publishers put a masthead logo in `og:image` when no article-specific image exists.
_LOGO_HINTS = ("logo", "favicon", "avatar", "sprite", "icon-", "pixel.gif", "spacer.")


def discover_from_feed_entry(payload_raw: str, source_url: str) -> list[MediaCandidate]:
    """Candidates from a feed entry's own structured fields.

    ``payload_raw`` is the JSON already stored on ``RawItem`` — a faithful record of
    everything ``feedparser`` extracted (see ``sources.rss._payload``) — so this reads
    fields already parsed elsewhere rather than re-parsing feed XML itself.
    """
    try:
        entry = json.loads(payload_raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(entry, dict):
        return []

    candidates: list[MediaCandidate] = []
    for enclosure in entry.get("enclosures") or []:
        candidate = _from_enclosure(enclosure, source_url)
        if candidate is not None:
            candidates.append(candidate)

    for media_field in ("media_content", "media_thumbnail"):
        for item in entry.get(media_field) or []:
            candidate = _from_enclosure(item, source_url, confidence=0.6)
            if candidate is not None:
                candidates.append(candidate)

    return _filter_and_rank(candidates)


def discover_from_html(html: str, source_url: str) -> list[MediaCandidate]:
    """Candidates from a fetched article page's Open Graph and JSON-LD metadata."""
    try:
        tree = HTMLParser(html)
    except Exception:  # pragma: no cover - selectolax raises exceedingly rarely
        return []

    candidates: list[MediaCandidate] = [*_open_graph(tree, source_url), *_json_ld(tree, source_url)]
    return _filter_and_rank(candidates)


def _from_enclosure(
    item: object, source_url: str, *, confidence: float = 0.8
) -> MediaCandidate | None:
    if not isinstance(item, dict):
        return None
    url = item.get("href") or item.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    mime_type = item.get("type") if isinstance(item.get("type"), str) else None
    kind = _kind_from_mime(mime_type) or _kind_from_url(url)
    if kind is None:
        return None
    width = _to_int(item.get("width"))
    height = _to_int(item.get("height"))
    return MediaCandidate(
        url=urljoin(source_url, url.strip()),
        kind=kind,
        source_method=DiscoveryMethod.FEED_ENCLOSURE,
        source_url=source_url,
        width=width,
        height=height,
        mime_type=mime_type,
        confidence=confidence,
    )


def _open_graph(tree: HTMLParser, source_url: str) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    for prop, kind, method in (
        ("og:image", MediaKind.IMAGE, DiscoveryMethod.OPEN_GRAPH_IMAGE),
        ("og:video", MediaKind.VIDEO, DiscoveryMethod.OPEN_GRAPH_VIDEO),
    ):
        for node in tree.css(f'meta[property="{prop}"]'):
            url = node.attributes.get("content")
            if not url or not url.strip():
                continue
            width = _meta_int(tree, f"{prop}:width")
            height = _meta_int(tree, f"{prop}:height")
            candidates.append(
                MediaCandidate(
                    url=urljoin(source_url, url.strip()),
                    kind=kind,
                    source_method=method,
                    source_url=source_url,
                    width=width,
                    height=height,
                    confidence=0.9,
                )
            )
    return candidates


def _meta_int(tree: HTMLParser, prop: str) -> int | None:
    node = tree.css_first(f'meta[property="{prop}"]')
    if node is None:
        return None
    return _to_int(node.attributes.get("content"))


def _json_ld(tree: HTMLParser, source_url: str) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    for node in tree.css('script[type="application/ld+json"]'):
        text = node.text(strip=True)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        for entry in payload if isinstance(payload, list) else [payload]:
            candidates.extend(_json_ld_media(entry, source_url))
    return candidates


def _json_ld_media(entry: object, source_url: str) -> list[MediaCandidate]:
    if not isinstance(entry, dict):
        return []
    candidates: list[MediaCandidate] = []
    for field, kind in (("image", MediaKind.IMAGE), ("video", MediaKind.VIDEO)):
        value = entry.get(field)
        for url in _json_ld_urls(value):
            candidates.append(
                MediaCandidate(
                    url=urljoin(source_url, url),
                    kind=kind,
                    source_method=DiscoveryMethod.JSON_LD,
                    source_url=source_url,
                    confidence=0.7,
                )
            )
    return candidates


def _json_ld_urls(value: object) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, dict):
        url = value.get("url") or value.get("contentUrl")
        return [url.strip()] if isinstance(url, str) and url.strip() else []
    if isinstance(value, list):
        urls: list[str] = []
        for item in value:
            urls.extend(_json_ld_urls(item))
        return urls
    return []


def _kind_from_mime(mime_type: str | None) -> MediaKind | None:
    if not mime_type:
        return None
    if mime_type.startswith("image/"):
        return MediaKind.IMAGE
    if mime_type.startswith("video/"):
        return MediaKind.VIDEO
    return None


_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".m4v")


def _kind_from_url(url: str) -> MediaKind | None:
    lowered = url.lower().split("?")[0]
    if lowered.endswith(_IMAGE_SUFFIXES):
        return MediaKind.IMAGE
    if lowered.endswith(_VIDEO_SUFFIXES):
        return MediaKind.VIDEO
    return None


def _to_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _filter_and_rank(candidates: list[MediaCandidate]) -> list[MediaCandidate]:
    """Drop obvious non-hero media, dedup by URL, order by confidence.

    Dimension filtering here is best-effort only: many candidates declare no
    width/height at all, and those are kept — the real minimum-size check happens
    after download, once the actual decoded dimensions are known (see
    ``media.pipeline``). This pass only rejects what is *already* known to be too
    small, and anything whose URL itself signals a logo/icon/pixel.
    """
    seen: set[str] = set()
    kept: list[MediaCandidate] = []
    for candidate in candidates:
        if candidate.url in seen:
            continue
        seen.add(candidate.url)

        lowered_url = candidate.url.lower()
        if any(hint in lowered_url for hint in _LOGO_HINTS):
            continue
        if candidate.width is not None and candidate.width < MIN_CANDIDATE_WIDTH:
            continue
        if candidate.height is not None and candidate.height < MIN_CANDIDATE_HEIGHT:
            continue

        kept.append(candidate)

    return sorted(kept, key=lambda c: c.confidence, reverse=True)


__all__ = ["discover_from_feed_entry", "discover_from_html"]
