"""Exactly one verified, narrow media-reuse permission — Step 6 (AI News Agent v2).

This is not a change to any source's ``MediaPolicy``. Every RSS source in
``config/sources.yaml`` — including every Google one (``google_ai_blog``,
``google_deepmind``, ``google_research``) — stays exactly ``NO_MEDIA``, unchanged.
What this module adds is a single, separate, explicitly-scoped path: Google's own
Press Corner Image Library page states, in its own words (verified by direct fetch
against ``https://blog.google/image-library/`` — not assumed, not inferred from "it's
an official page"):

    "Images on this page may be used for publication with credit: 'Source: Google.'"

That permission covers only the images actually hosted on that one page. It says
nothing about a Google blog post's own hero image, and this module never treats it as
if it did — an ordinary ``blog.google`` article's image is still never downloaded or
reuploaded, exactly as before. This path is only ever reached for a story whose
selected source is a Google one, and only ever attaches an image whose ``alt`` text on
that one page actually matches the story (see
``media.discover.discover_licensed_library_assets``) — never a substituted, unrelated
image. The required credit line travels with the asset every time it is used.
"""

from __future__ import annotations

from ai_news_editor.media.discover import discover_licensed_library_assets
from ai_news_editor.media.download import download_media
from ai_news_editor.media.image import process_image
from ai_news_editor.media.limits import TARGET_PHOTO_BYTES
from ai_news_editor.media.models import (
    DiscoveryMethod,
    MediaOutcome,
    ProcessedMedia,
    RejectionReason,
)
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

#: Verified by direct fetch on 2026-08-19 — see this module's own docstring for the
#: exact quoted permission text this URL and credit line rest on.
GOOGLE_PRESS_CORNER_URL = "https://blog.google/image-library/"
GOOGLE_PRESS_CORNER_CREDIT = "Source: Google"

#: Source ids (from config/sources.yaml) this path may ever be attempted for — never
#: any source outside this explicit allow-list, and never as a substitute for a
#: source's own (still-NO_MEDIA) discovery.
GOOGLE_SOURCE_IDS = frozenset({"google_ai_blog", "google_deepmind", "google_research"})


def download_and_process_press_corner_asset(
    *, source_id: str, story_keywords: list[str], press_corner_html: str, workspace: MediaWorkspace
) -> MediaOutcome:
    """The full narrow path: discover -> download -> process, or an honest reason why not.

    Never falls back to an unrelated image. A caller that gets a non-``.ok`` outcome
    here should fall back to text-only, exactly like any other media outcome.
    """
    if source_id not in GOOGLE_SOURCE_IDS:
        return MediaOutcome(media=None, reason=RejectionReason.POLICY_FORBIDS)

    candidates = discover_licensed_library_assets(
        press_corner_html, GOOGLE_PRESS_CORNER_URL, keywords=story_keywords
    )
    if not candidates:
        return MediaOutcome(
            media=None,
            reason=RejectionReason.NO_CANDIDATES,
            detail="no Press Corner image's alt text matched this story",
        )

    candidate = candidates[0]
    try:
        download = download_media(
            candidate.url, workspace.path("press-corner-original.jpg"), kind="image"
        )
        processed = process_image(
            download.path, workspace.path("press-corner-processed.jpg"),
            target_bytes=TARGET_PHOTO_BYTES,
        )
    except Exception as exc:
        logger.info("press_corner_asset_failed", extra={"reason": str(exc)})
        return MediaOutcome(media=None, reason=RejectionReason.PROCESSING_FAILED, detail=str(exc))

    return MediaOutcome(
        media=ProcessedMedia(
            path=str(processed.path),
            kind=candidate.kind,
            width=processed.width,
            height=processed.height,
            size_bytes=processed.size_bytes,
            source_url=candidate.url,
            source_method=DiscoveryMethod.LICENSED_LIBRARY,
        )
    )


__all__ = [
    "GOOGLE_PRESS_CORNER_CREDIT",
    "GOOGLE_PRESS_CORNER_URL",
    "GOOGLE_SOURCE_IDS",
    "download_and_process_press_corner_asset",
]
