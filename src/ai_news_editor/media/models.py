"""Structured media types — Step 4 (AI News Agent v2).

Discovery returns candidates, never files; a candidate is a claim about a URL, not
proof anything about it is safe or usable yet. Everything downstream (policy, download,
validation, processing) narrows a candidate down or rejects it — nothing here decodes,
fetches, or trusts a byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MediaKind(StrEnum):
    """What kind of media a candidate or a processed file is."""

    IMAGE = "IMAGE"
    VIDEO = "VIDEO"


class DiscoveryMethod(StrEnum):
    """Where a candidate was found. Recorded so a decision is always explainable —
    "why did we pick this one" always has an answer traceable to real, structured
    article metadata, never to guessing at page layout."""

    #: RSS/Atom `<enclosure>` or Media RSS (`media:content`/`media:thumbnail`).
    FEED_ENCLOSURE = "FEED_ENCLOSURE"
    #: Open Graph `og:image` / `og:image:*`.
    OPEN_GRAPH_IMAGE = "OPEN_GRAPH_IMAGE"
    #: Open Graph `og:video` / `og:video:*`.
    OPEN_GRAPH_VIDEO = "OPEN_GRAPH_VIDEO"
    #: schema.org / JSON-LD `image` or `video` property.
    JSON_LD = "JSON_LD"
    #: Step 6: an `<img>` on a source's own explicitly-licensed asset page — the one
    #: deliberate, narrowly-scoped exception to "never scrape `<img>` tags" (see
    #: `media.discover.discover_licensed_library_assets` and `media.licensed_assets`).
    #: Never used for an ordinary article page; only for a page whose own stated terms
    #: were hand-verified to permit exactly this.
    LICENSED_LIBRARY = "LICENSED_LIBRARY"
    #: Step 6B: not discovered anywhere — drawn locally by
    #: ``media.branded_card.generate_branded_card``, from the post's own category,
    #: headline and source label. The "universal safe fallback": always available,
    #: never a copyright question, never blocks publication.
    GENERATED_CARD = "GENERATED_CARD"
    #: Step 6B: a verified open-license media provider (see ``media.open_license``) —
    #: found via that provider's own search API, not scraped, and only ever a
    #: candidate whose license was actually checked field-by-field, never assumed from
    #: "it's on Wikimedia" alone.
    OPEN_LICENSE_PROVIDER = "OPEN_LICENSE_PROVIDER"


@dataclass(frozen=True, slots=True)
class MediaCandidate:
    """One discovered media URL, before anything has been downloaded or verified.

    Deliberately thin and structural: everything here comes from parsing feed/HTML
    metadata that already names an image or a video explicitly, never from scanning a
    page's DOM for the first ``<img>`` or guessing from layout.
    """

    url: str
    kind: MediaKind
    source_method: DiscoveryMethod
    #: The article/page this candidate was discovered on — for logging and for the
    #: "media must come from the selected article" rule (never a generic search).
    source_url: str
    #: Declared dimensions, when the metadata states them (``og:image:width`` etc.).
    #: ``None`` means unknown, not "no dimensions" — decided later, after download.
    width: int | None = None
    height: int | None = None
    mime_type: str | None = None
    #: How confident discovery is that this is a genuine hero image/video rather than
    #: a logo, icon, or unrelated asset — a coarse 0-1 signal used only to order
    #: candidates, never to bypass the minimum-dimension or policy checks.
    confidence: float = 0.5


class RejectionReason(StrEnum):
    """Why a candidate never became a publishable asset. Always recorded, never
    silently swallowed — a text-only fallback should be explainable, not mysterious."""

    NO_CANDIDATES = "NO_CANDIDATES"
    POLICY_FORBIDS = "POLICY_FORBIDS"
    TOO_SMALL = "TOO_SMALL"
    UNSAFE_URL = "UNSAFE_URL"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    TOO_LARGE = "TOO_LARGE"
    INVALID_CONTENT_TYPE = "INVALID_CONTENT_TYPE"
    CORRUPT_MEDIA = "CORRUPT_MEDIA"
    PROCESSING_FAILED = "PROCESSING_FAILED"
    PROCESSING_TIMEOUT = "PROCESSING_TIMEOUT"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    FFMPEG_UNAVAILABLE = "FFMPEG_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ProcessedMedia:
    """One successfully downloaded, validated and compressed local file, ready to
    hand to the publishing layer.

    The five attribution fields below default to ``None`` for a source-discovered
    image (``rendering.plan`` builds its own attribution from ``source_url`` for
    those, unchanged) — they exist so an open-license provider (Step 6B,
    ``media.open_license``) can carry its *real*, checked attribution structurally,
    never fabricated and never inferred from "it's probably fine."
    """

    path: str
    kind: MediaKind
    width: int
    height: int
    size_bytes: int
    source_url: str
    source_method: DiscoveryMethod
    #: e.g. "Wikimedia Commons" — who this asset actually came from.
    media_provider: str | None = None
    #: The named creator/author, as the provider itself records it.
    creator: str | None = None
    #: The license's short name (e.g. "CC BY-SA 4.0"), as the provider itself records it.
    license: str | None = None
    #: A link to the actual license text, when the provider gives one.
    license_url: str | None = None
    #: The exact credit line the license requires — carried verbatim so a caller never
    #: has to reconstruct or guess a legally-required attribution string.
    required_credit: str | None = None


@dataclass(frozen=True, slots=True)
class MediaOutcome:
    """The result of running the whole pipeline for one article: either a processed
    file, or a reason there isn't one — never an exception the caller must catch.

    A text-only publication is not a failure of this pipeline; it is one of its two
    normal outcomes, and ``reason`` says which of the many ways that happened.
    """

    media: ProcessedMedia | None
    reason: RejectionReason | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.media is not None


__all__ = [
    "DiscoveryMethod",
    "MediaCandidate",
    "MediaKind",
    "MediaOutcome",
    "ProcessedMedia",
    "RejectionReason",
]
