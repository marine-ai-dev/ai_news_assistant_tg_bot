"""Verified open-license media — Step 6B (AI News Agent v2), layer (B) of the
four-layer media strategy: (A) explicit first-party licensed media
(``media.licensed_assets``), **(B) this module**, (C) a locally-generated branded card
(``media.branded_card``), (D) text/link-preview.

**Wikimedia Commons only, in this iteration.** Commons has a public search+metadata
API (``commons.wikimedia.org/w/api.php``) that needs no API key, so this module was
built against it and live-verified: a real, unauthenticated search for "artificial
intelligence" was run against the real API while writing this module, returning real
files with real ``LicenseShortName``/``LicenseUrl``/``Artist`` metadata (see the field
names this module reads below — they were confirmed to exist in that real response,
not assumed from documentation).

Pexels and Unsplash are deliberately **not** implemented here. Both require an API key
this project does not have — no ``AI_NEWS_PEXELS_API_KEY`` or
``AI_NEWS_UNSPLASH_ACCESS_KEY`` exists in ``Settings`` or ``.env``. Writing a client for
either that has never made one real authenticated call, and could not be live-verified
in this session, would be exactly the kind of unverified "looks done" work this
project's own discipline rejects (see ``docs/media.md`` and every other media module's
insistence on checking real behaviour, not assuming it). If real credentials are added
later, a Pexels/Unsplash module belongs here, following the same shape: search ->
license-field check -> relevance check -> download -> attribution carried structurally.
Until then, layer (B) is Wikimedia Commons alone, and a story with nothing relevant
there falls through to layer (C), exactly as the four-layer order intends.

**Never equal, generic treatment across providers** (the spec's own instruction): this
module does not pick "the first image the API returns" — every candidate's own license
field is checked, relevance to the story is required (**every** keyword must appear in
the file's own title, not just one — see ``_candidate_from_page``), and attribution is
carried through structurally, never fabricated. No image is ever substituted for an
unrelated one just because a search returned it.

The all-keywords rule was tightened after a real, live end-to-end run (wiring this
module into ``media.strategy``, before that module's own test suite existed) surfaced
an actual false positive: a headline mentioning "OpenAI Sora" matched a real Commons
photo of *Sora, an Italian comune in Lazio* — an unrelated place that happens to share
a product's name — because the original match rule accepted any one keyword. Requiring
every keyword trades recall for precision, which is correct here: layer (C) is always a
safe, correct fallback, and a wrong photo is not.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from ai_news_editor.media.download import download_media
from ai_news_editor.media.image import process_image
from ai_news_editor.media.limits import (
    MIN_CANDIDATE_HEIGHT,
    MIN_CANDIDATE_WIDTH,
    TARGET_PHOTO_BYTES,
)
from ai_news_editor.media.models import (
    DiscoveryMethod,
    MediaKind,
    MediaOutcome,
    ProcessedMedia,
    RejectionReason,
)
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.sources.http import HttpClient, HttpError

logger = get_logger(__name__)

WIKIMEDIA_COMMONS_PROVIDER_NAME = "Wikimedia Commons"
_COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"

#: License short-name substrings Commons' own ``LicenseShortName`` field uses that this
#: application will reuse with attribution. "NC" (non-commercial) and "ND" (no
#: derivatives) are excluded explicitly, defense-in-depth on top of Commons' own
#: free-content policy — see ``_is_reusable_license``.
_REUSABLE_LICENSE_PREFIXES = ("CC0", "PUBLIC DOMAIN", "CC BY")
_RESTRICTIVE_LICENSE_MARKERS = ("NC", "ND")

#: How many search results one query asks the Commons API for — small on purpose, this
#: is one relevant image for one post, not a crawl.
DEFAULT_SEARCH_LIMIT = 10

_HTML_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class OpenLicenseCandidate:
    """One Commons file whose license was actually checked, not assumed."""

    url: str
    title: str
    width: int
    height: int
    creator: str
    license: str
    license_url: str | None


def _is_reusable_license(short_name: str) -> bool:
    normalized = short_name.strip().upper()
    if any(marker in normalized for marker in _RESTRICTIVE_LICENSE_MARKERS):
        return False
    return normalized.startswith(_REUSABLE_LICENSE_PREFIXES)


def _strip_html(fragment: str) -> str:
    return html.unescape(_HTML_TAG.sub("", fragment)).strip()


def _candidate_from_page(
    page: dict[str, object], keywords: Sequence[str]
) -> OpenLicenseCandidate | None:
    raw_title = str(page.get("title", ""))
    title = raw_title.removeprefix("File:")
    imageinfo_list = page.get("imageinfo")
    if not isinstance(imageinfo_list, list) or not imageinfo_list:
        return None
    imageinfo = imageinfo_list[0]
    if not isinstance(imageinfo, dict):
        return None

    mime = str(imageinfo.get("mime", ""))
    if not mime.startswith("image/"):
        return None

    url = imageinfo.get("url")
    width = imageinfo.get("width")
    height = imageinfo.get("height")
    if not isinstance(url, str) or not isinstance(width, int) or not isinstance(height, int):
        return None
    if width < MIN_CANDIDATE_WIDTH or height < MIN_CANDIDATE_HEIGHT:
        return None

    if keywords and not all(kw.lower() in title.lower() for kw in keywords):
        # ALL, not ANY: a single short/generic keyword (a product name that also
        # happens to be a place name, a common word) matching alone was confirmed,
        # live, to surface a genuinely unrelated image (see this module's docstring
        # test note) — requiring every keyword trades recall for precision, which is
        # the right trade here since layer C (the branded card) is always a safe,
        # correct fallback and an unrelated photo is not.
        return None

    extmeta = imageinfo.get("extmetadata")
    if not isinstance(extmeta, dict):
        return None
    license_short = _extmeta_value(extmeta, "LicenseShortName")
    if not license_short or not _is_reusable_license(license_short):
        return None
    creator_raw = _extmeta_value(extmeta, "Artist")
    creator = _strip_html(creator_raw) if creator_raw else ""
    if not creator:
        # No named creator to credit — the license may still require attribution we
        # cannot honestly provide, so this candidate is skipped rather than guessed.
        return None
    license_url = _extmeta_value(extmeta, "LicenseUrl")

    return OpenLicenseCandidate(
        url=url,
        title=title,
        width=width,
        height=height,
        creator=creator,
        license=license_short,
        license_url=license_url or None,
    )


def _extmeta_value(extmeta: dict[str, object], key: str) -> str | None:
    entry = extmeta.get(key)
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    return str(value) if value else None


def discover_wikimedia_commons(
    query: str, *, http: HttpClient, keywords: Sequence[str] = (), limit: int = DEFAULT_SEARCH_LIMIT
) -> list[OpenLicenseCandidate]:
    """Search Commons' File namespace for ``query``, returning only candidates that
    pass every check: a real image mime type, minimum dimensions, a named creator to
    credit, a reusable license, and (when ``keywords`` is non-empty) a title match.

    Never raises for an ordinary network failure — returns an empty list, exactly like
    ``media.discover``'s own functions never raise for "nothing found."
    """
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|size|extmetadata|mime",
        "format": "json",
    }
    url = f"{_COMMONS_API_URL}?{urlencode(params)}"
    try:
        response = http.get(url)
    except HttpError as exc:
        logger.info("wikimedia_commons_search_failed", extra={"query": query, "reason": str(exc)})
        return []

    try:
        payload = json.loads(response.body)
    except json.JSONDecodeError:
        return []

    pages = payload.get("query", {}).get("pages", {})
    if not isinstance(pages, dict):
        return []

    candidates = []
    for page in pages.values():
        if not isinstance(page, dict):
            continue
        candidate = _candidate_from_page(page, keywords)
        if candidate is not None:
            candidates.append(candidate)
    return sorted(candidates, key=lambda c: -(c.width * c.height))


def download_and_process_open_license_asset(
    candidate: OpenLicenseCandidate,
    workspace: MediaWorkspace,
    *,
    transport: httpx.BaseTransport | None = None,
) -> MediaOutcome:
    """Download, validate and compress one already-checked candidate.

    Attribution travels with the result structurally (``ProcessedMedia.creator`` /
    ``.license`` / ``.license_url`` / ``.required_credit``) — never fabricated, always
    exactly what Commons' own metadata said.

    ``transport`` matches ``media.pipeline.select_media``'s own parameter of the same
    name — ``None`` means a real connection; tests pass an ``httpx.MockTransport``.
    """
    try:
        download = download_media(
            candidate.url,
            workspace.path("open-license-original.jpg"),
            kind="image",
            transport=transport,
        )
        processed = process_image(
            download.path,
            workspace.path("open-license-processed.jpg"),
            target_bytes=TARGET_PHOTO_BYTES,
        )
    except Exception as exc:
        logger.info("open_license_asset_failed", extra={"reason": str(exc)})
        return MediaOutcome(media=None, reason=RejectionReason.PROCESSING_FAILED, detail=str(exc))

    required_credit = (
        f"{candidate.creator} / {WIKIMEDIA_COMMONS_PROVIDER_NAME}, {candidate.license}"
    )
    return MediaOutcome(
        media=ProcessedMedia(
            path=str(processed.path),
            kind=MediaKind.IMAGE,
            width=processed.width,
            height=processed.height,
            size_bytes=processed.size_bytes,
            source_url=candidate.url,
            source_method=DiscoveryMethod.OPEN_LICENSE_PROVIDER,
            media_provider=WIKIMEDIA_COMMONS_PROVIDER_NAME,
            creator=candidate.creator,
            license=candidate.license,
            license_url=candidate.license_url,
            required_credit=required_credit,
        )
    )


__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "WIKIMEDIA_COMMONS_PROVIDER_NAME",
    "OpenLicenseCandidate",
    "discover_wikimedia_commons",
    "download_and_process_open_license_asset",
]
