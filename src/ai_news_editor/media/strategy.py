"""The full four-layer media strategy — Step 6B (AI News Agent v2).

One function, strict order, always returns a usable :class:`~media.models.MediaOutcome`
— never raises for an ordinary media failure, matching every other module in this
package. The order, exactly as specified:

0. Whatever this source's own registry ``MediaPolicy`` already permits discovering
   (unchanged Step 4 behaviour — ``media.pipeline.select_media``). Today no real
   source in ``config/sources.yaml`` is configured ``DISCOVER_MEDIA`` or
   ``EXPLICIT_REUSE_ALLOWED`` (confirmed by the Step 6 registry audit), so this layer
   is a no-op in practice — kept only so a source that later earns one of those
   policies is served by it automatically, without a second wiring change.
A. Explicit first-party licensed media (``media.licensed_assets``) — narrow, only
   ever attempted for a Google source (``GOOGLE_SOURCE_IDS``).
B. Verified open-license media (``media.open_license``) — Wikimedia Commons, keyword-
   matched against the story so an unrelated image is never substituted.
C. A locally-generated branded card (``media.branded_card``) — the universal safe
   fallback. Cannot fail for an ordinary reason (no network, no external data); a
   failure here means the committed font or the workspace disk itself is broken.
D. Text-only. Reached only if C itself raised — everything above already degrades to
   "try the next layer" on its own, so D is not a branch this function chooses, it is
   what happens when there is truly nothing left to try.

**Video is held to the same strict bar named in the spec ("only explicit first-party
or verified open license, never generic stock")** by construction, not by an extra
check: layer 0 is the only layer that can ever produce a video (gated by a
source's own explicit ``MediaPolicy``, unchanged from Step 4), layer A's Press Corner
path is images only, layer B (``open_license.discover_wikimedia_commons``) searches
Commons' image namespace only, and layer C draws a still image. No layer here ever
reaches for a generic stock-video provider, because none is wired in at all.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from ai_news_editor.domain.enums import EditorialCategory, MediaPolicy
from ai_news_editor.media.branded_card import BrandedCardError, generate_branded_card
from ai_news_editor.media.licensed_assets import (
    GOOGLE_PRESS_CORNER_URL,
    GOOGLE_SOURCE_IDS,
    download_and_process_press_corner_asset,
)
from ai_news_editor.media.models import MediaOutcome, RejectionReason
from ai_news_editor.media.open_license import (
    discover_wikimedia_commons,
    download_and_process_open_license_asset,
)
from ai_news_editor.media.pipeline import select_media
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.sources.http import HttpClient, HttpError

logger = get_logger(__name__)


def select_media_with_fallbacks(
    *,
    workspace: MediaWorkspace,
    http: HttpClient,
    source_id: str,
    source_url: str,
    media_policy: MediaPolicy,
    category: EditorialCategory,
    headline: str,
    source_label: str,
    story_keywords: Sequence[str],
    feed_payload_raw: str | None = None,
    html: str | None = None,
    download_transport: httpx.BaseTransport | None = None,
) -> MediaOutcome:
    """Run every layer in order, returning the first one that produces a usable asset.

    ``story_keywords`` drives both layer A's alt-text matching and layer B's search —
    pass short, specific terms (company/product name, the story's own subject), never
    generic words: the same discipline ``media.discover``/``media.licensed_assets``/
    ``media.open_license`` already each individually require, applied once here at the
    call site.

    ``download_transport`` matches ``media.pipeline.select_media``'s own ``transport``
    parameter — it is the *asset download* transport (image/video bytes), a separate
    concern from ``http`` (the API/HTML transport ``HttpClient`` already wraps).
    ``None`` means a real connection; tests pass an ``httpx.MockTransport``.
    """
    baseline = select_media(
        workspace=workspace,
        source_url=source_url,
        media_policy=media_policy,
        feed_payload_raw=feed_payload_raw,
        html=html,
        transport=download_transport,
    )
    if baseline.ok:
        return baseline

    licensed = _try_licensed_first_party(
        http=http,
        source_id=source_id,
        story_keywords=story_keywords,
        workspace=workspace,
        download_transport=download_transport,
    )
    if licensed is not None and licensed.ok:
        return licensed

    open_license = _try_open_license(
        http=http,
        story_keywords=story_keywords,
        workspace=workspace,
        download_transport=download_transport,
    )
    if open_license is not None and open_license.ok:
        return open_license

    try:
        card = generate_branded_card(
            category=category,
            headline=headline,
            source_label=source_label,
            dest=workspace.path("branded-card.jpg"),
        )
        return MediaOutcome(media=card)
    except BrandedCardError as exc:
        logger.warning("branded_card_failed", extra={"reason": str(exc)})
        return MediaOutcome(media=None, reason=RejectionReason.PROCESSING_FAILED, detail=str(exc))


def _try_licensed_first_party(
    *,
    http: HttpClient,
    source_id: str,
    story_keywords: Sequence[str],
    workspace: MediaWorkspace,
    download_transport: httpx.BaseTransport | None,
) -> MediaOutcome | None:
    if source_id not in GOOGLE_SOURCE_IDS:
        return None
    try:
        press_corner_html = http.get(GOOGLE_PRESS_CORNER_URL).body.decode(
            "utf-8", errors="replace"
        )
    except HttpError as exc:
        logger.info("press_corner_fetch_failed", extra={"reason": str(exc)})
        return None
    return download_and_process_press_corner_asset(
        source_id=source_id,
        story_keywords=list(story_keywords),
        press_corner_html=press_corner_html,
        workspace=workspace,
        transport=download_transport,
    )


def _try_open_license(
    *,
    http: HttpClient,
    story_keywords: Sequence[str],
    workspace: MediaWorkspace,
    download_transport: httpx.BaseTransport | None,
) -> MediaOutcome | None:
    if not story_keywords:
        return None
    query = " ".join(story_keywords)
    candidates = discover_wikimedia_commons(query, http=http, keywords=story_keywords)
    for candidate in candidates:
        outcome = download_and_process_open_license_asset(
            candidate, workspace, transport=download_transport
        )
        if outcome.ok:
            return outcome
    return None


__all__ = ["select_media_with_fallbacks"]
