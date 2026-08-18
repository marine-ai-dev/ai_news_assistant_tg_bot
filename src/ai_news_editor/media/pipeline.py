"""Orchestration: discover → policy gate → download → validate → compress — Step 4
(AI News Agent v2).

The one rule everything else in this module exists to serve: **media is optional, and
never a blocker for a valid text post.** ``select_media`` never raises for an ordinary
media failure — a missing candidate, a broken download, a corrupt file, an oversized
source, ffmpeg being unavailable, a policy that forbids reuse. Every one of those
becomes a normal :class:`~media.models.MediaOutcome` with ``media=None`` and a
``reason``, for the caller to fall back to text-only. Only a genuine programming error
(a bad argument, an unreadable workspace) still raises.

No Gemini dependency anywhere in this module — media selection is entirely
deterministic, based on source policy, discovery confidence, media kind, dimensions,
and file size (see ``docs/media.md`` for the selection philosophy).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx

from ai_news_editor.domain.enums import MediaPolicy
from ai_news_editor.media import discover, image, video
from ai_news_editor.media.download import (
    InvalidContentTypeError,
    MediaDownloadError,
    MediaTooLargeError,
    download_media,
)
from ai_news_editor.media.limits import MIN_CANDIDATE_HEIGHT, MIN_CANDIDATE_WIDTH
from ai_news_editor.media.models import (
    MediaCandidate,
    MediaKind,
    MediaOutcome,
    ProcessedMedia,
    RejectionReason,
)
from ai_news_editor.media.urlsafety import UnsafeMediaUrlError
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

#: Policies under which media may even be discovered/downloaded at all. NO_MEDIA and
#: LINK_PREVIEW_ONLY both stop before any download — the difference between them is
#: Telegram's own unrelated link-preview behaviour, which this application neither
#: controls nor needs to touch.
_DOWNLOAD_ALLOWED_POLICIES = frozenset(
    {MediaPolicy.DISCOVER_MEDIA, MediaPolicy.EXPLICIT_REUSE_ALLOWED}
)


def select_media(
    *,
    workspace: MediaWorkspace,
    source_url: str,
    media_policy: MediaPolicy,
    feed_payload_raw: str | None = None,
    html: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> MediaOutcome:
    """Discover, download, validate and compress the best available media for one
    article — or explain, without raising, why there is none.

    ``workspace`` must already be open (used as ``with MediaWorkspace(...) as ws:``);
    this function only ever writes inside it, and cleanup remains the caller's
    responsibility so the processed file survives until publication actually uses it.
    """
    if media_policy not in _DOWNLOAD_ALLOWED_POLICIES:
        logger.info("media_discovery", extra={"source_url": source_url, "candidates": 0})
        return MediaOutcome(
            media=None,
            reason=RejectionReason.POLICY_FORBIDS,
            detail=f"source media_policy is {media_policy.value}; no download attempted",
        )

    candidates = _discover(feed_payload_raw, html, source_url)
    logger.info(
        "media_discovery", extra={"source_url": source_url, "candidates": len(candidates)}
    )
    if not candidates:
        return MediaOutcome(media=None, reason=RejectionReason.NO_CANDIDATES)

    last_reason = RejectionReason.NO_CANDIDATES
    last_detail: str | None = None

    for candidate in _ranked(candidates):
        try:
            processed = _try_candidate(candidate, workspace, transport)
        except _Rejected as rejection:
            last_reason, last_detail = rejection.reason, rejection.detail
            continue

        logger.info(
            "media_selected",
            extra={"type": processed.kind.value, "method": processed.source_method.value},
        )
        return MediaOutcome(media=processed)

    return MediaOutcome(media=None, reason=last_reason, detail=last_detail)


class _Rejected(Exception):
    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _discover(
    feed_payload_raw: str | None, html: str | None, source_url: str
) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    if feed_payload_raw is not None:
        candidates.extend(discover.discover_from_feed_entry(feed_payload_raw, source_url))
    if html is not None:
        candidates.extend(discover.discover_from_html(html, source_url))

    seen: set[str] = set()
    unique: list[MediaCandidate] = []
    for candidate in candidates:
        if candidate.url not in seen:
            seen.add(candidate.url)
            unique.append(candidate)
    return unique


def _ranked(candidates: list[MediaCandidate]) -> list[MediaCandidate]:
    """"valid first-party hero image > valid first-party video/poster > ... > none" —
    images are tried before videos (simpler, smaller, no ffmpeg dependency), and within
    each kind, higher discovery confidence and larger declared dimensions go first."""

    def key(candidate: MediaCandidate) -> tuple[int, float, int]:
        kind_rank = 0 if candidate.kind is MediaKind.IMAGE else 1
        area = (candidate.width or 0) * (candidate.height or 0)
        return (kind_rank, -candidate.confidence, -area)

    return sorted(candidates, key=key)


def _try_candidate(
    candidate: MediaCandidate, workspace: MediaWorkspace, transport: httpx.BaseTransport | None
) -> ProcessedMedia:
    kind = "image" if candidate.kind is MediaKind.IMAGE else "video"
    try:
        download = download_media(
            candidate.url, workspace.path(f"original-{_slug(candidate)}"), kind=kind,
            transport=transport,
        )
    except UnsafeMediaUrlError as exc:
        raise _Rejected(RejectionReason.UNSAFE_URL, str(exc)) from exc
    except InvalidContentTypeError as exc:
        raise _Rejected(RejectionReason.INVALID_CONTENT_TYPE, str(exc)) from exc
    except MediaTooLargeError as exc:
        raise _Rejected(RejectionReason.TOO_LARGE, str(exc)) from exc
    except MediaDownloadError as exc:
        raise _Rejected(RejectionReason.DOWNLOAD_FAILED, str(exc)) from exc

    if candidate.kind is MediaKind.IMAGE:
        return _process_downloaded_image(candidate, download.path, workspace)
    return _process_downloaded_video(candidate, download.path, workspace)


def _process_downloaded_image(
    candidate: MediaCandidate, src: Path, workspace: MediaWorkspace
) -> ProcessedMedia:
    dest = workspace.path(f"processed-{_slug(candidate)}.jpg")
    try:
        processed = image.process_image(src, dest)
    except image.CorruptImageError as exc:
        raise _Rejected(RejectionReason.CORRUPT_MEDIA, str(exc)) from exc
    except image.ImageTooLargeError as exc:
        raise _Rejected(RejectionReason.TOO_LARGE, str(exc)) from exc
    except image.ImageProcessingError as exc:
        raise _Rejected(RejectionReason.PROCESSING_FAILED, str(exc)) from exc

    if processed.width < MIN_CANDIDATE_WIDTH or processed.height < MIN_CANDIDATE_HEIGHT:
        raise _Rejected(
            RejectionReason.TOO_SMALL,
            f"{processed.width}x{processed.height} is under the "
            f"{MIN_CANDIDATE_WIDTH}x{MIN_CANDIDATE_HEIGHT} minimum",
        )

    return ProcessedMedia(
        path=str(processed.path), kind=MediaKind.IMAGE, width=processed.width,
        height=processed.height, size_bytes=processed.size_bytes,
        source_url=candidate.url, source_method=candidate.source_method,
    )


def _process_downloaded_video(
    candidate: MediaCandidate, src: Path, workspace: MediaWorkspace
) -> ProcessedMedia:
    if not video.ffmpeg_available():
        raise _Rejected(RejectionReason.FFMPEG_UNAVAILABLE, "ffmpeg/ffprobe not installed")

    dest = workspace.path(f"processed-{_slug(candidate)}.mp4")
    try:
        processed = video.process_video(src, dest)
    except video.CorruptVideoError as exc:
        raise _Rejected(RejectionReason.CORRUPT_MEDIA, str(exc)) from exc
    except video.VideoTooLargeError as exc:
        raise _Rejected(RejectionReason.TOO_LARGE, str(exc)) from exc
    except video.VideoProcessingTimeoutError as exc:
        raise _Rejected(RejectionReason.PROCESSING_TIMEOUT, str(exc)) from exc
    except video.VideoProcessingError as exc:
        raise _Rejected(RejectionReason.PROCESSING_FAILED, str(exc)) from exc
    except video.VideoUnavailableError as exc:
        raise _Rejected(RejectionReason.FFMPEG_UNAVAILABLE, str(exc)) from exc

    if processed.width < MIN_CANDIDATE_WIDTH or processed.height < MIN_CANDIDATE_HEIGHT:
        raise _Rejected(
            RejectionReason.TOO_SMALL,
            f"{processed.width}x{processed.height} is under the "
            f"{MIN_CANDIDATE_WIDTH}x{MIN_CANDIDATE_HEIGHT} minimum",
        )

    return ProcessedMedia(
        path=str(processed.path), kind=MediaKind.VIDEO, width=processed.width,
        height=processed.height, size_bytes=processed.size_bytes,
        source_url=candidate.url, source_method=candidate.source_method,
    )


def _slug(candidate: MediaCandidate) -> str:
    return hashlib.sha256(candidate.url.encode("utf-8")).hexdigest()[:16]


__all__ = ["select_media"]
