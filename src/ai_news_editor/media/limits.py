"""Every hard number the media pipeline uses, in one place — Step 4 (AI News Agent v2).

Verified against the official Telegram Bot API reference (https://core.telegram.org/bots/api,
``sendPhoto``/``sendVideo`` parameter tables) at the time this module was written:

* "The photo must be at most 10 MB in size." — ``sendPhoto``.
* "Bots can currently send video files of up to 50 MB in size" — ``sendVideo``.
* Caption: "0-1024 characters after entities parsing" — shared by every media-sending
  method. ``publishing/plan.py`` already cites and enforces this as ``MAX_CAPTION_CHARS``;
  it is not duplicated here, to keep one number with one owner.

Every other constant below (compression targets, dimension caps, download limits,
timeouts) is this application's own policy, chosen to sit comfortably under the wire
limits above rather than at the edge of them — a photo compressed to exactly 10 MB has
no room for Telegram's own re-encoding or a slightly-too-generous size estimate.
"""

from __future__ import annotations

# --- Telegram wire limits (verified against the Bot API docs; see module docstring) --

#: sendPhoto: "The photo must be at most 10 MB in size."
MAX_TELEGRAM_PHOTO_BYTES = 10 * 1024 * 1024

#: sendVideo: "Bots can currently send video files of up to 50 MB in size."
MAX_TELEGRAM_VIDEO_BYTES = 50 * 1024 * 1024

# --- This application's own compression targets — comfortably under the wire limits --

#: Iterative JPEG/WebP compression stops once the file is at or under this size. Well
#: under MAX_TELEGRAM_PHOTO_BYTES so Telegram's own processing has headroom, and so a
#: slightly-too-generous size estimate never tips a post over the real cap.
TARGET_PHOTO_BYTES = 4 * 1024 * 1024

#: Neither dimension of a processed photo exceeds this after resizing. Comfortably
#: above what any Telegram client renders a photo at, so quality is not sacrificed for
#: no visible benefit — this bounds pixel count (and therefore memory/CPU and file
#: size), not visual quality.
MAX_PHOTO_DIMENSION = 2560

#: A photo already smaller than this in both dimensions is left at its native size —
#: this pipeline resizes down, never up. Upscaling a small source image manufactures
#: detail that was never there and makes a thumbnail look worse, not better.
MIN_UPSCALE_GUARD_DIMENSION = 200

#: Video compression target — comfortably under MAX_TELEGRAM_VIDEO_BYTES.
TARGET_VIDEO_BYTES = 20 * 1024 * 1024

#: Neither dimension of a processed video exceeds this after scaling.
MAX_VIDEO_DIMENSION = 1280

#: A source video longer than this is rejected before transcoding is even attempted —
#: this channel posts short news clips, not long-form video, and a multi-hour source
#: would spend the whole job's time budget on one file for no editorial reason.
MAX_VIDEO_DURATION_SECONDS = 180

# --- Download safety (applies to any remote media URL, not just Telegram's own limits) --

#: A source file larger than this is rejected before download even starts (via
#: Content-Length) or aborted mid-stream (if the server omits/understates it) — well
#: above the compression targets above, since the source is compressed *down* to them,
#: but still small enough that one pathological URL cannot exhaust runner disk/time.
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024

DOWNLOAD_CONNECT_TIMEOUT_SECONDS = 10.0
DOWNLOAD_READ_TIMEOUT_SECONDS = 30.0

#: Redirects are followed manually (see ``media.download``), one hop at a time, so
#: every intermediate host can be re-validated against the same SSRF rules as the
#: original URL — a small cap keeps a redirect chain from becoming its own DoS vector.
MAX_DOWNLOAD_REDIRECTS = 3

#: Minimum pixel dimensions a discovered image must meet to be considered a candidate
#: hero image at all — well above a typical favicon/logo/sprite/tracking pixel, so
#: those are rejected by shape alone before any policy or download decision is made.
MIN_CANDIDATE_WIDTH = 200
MIN_CANDIDATE_HEIGHT = 200

# --- Video processing (ffmpeg) ------------------------------------------------------

#: Hard wall-clock budget for one ffmpeg transcode. No infinite process: a hang is
#: treated exactly like a processing failure, and the pipeline falls back accordingly.
FFMPEG_TIMEOUT_SECONDS = 120.0
FFPROBE_TIMEOUT_SECONDS = 15.0

__all__ = [
    "DOWNLOAD_CONNECT_TIMEOUT_SECONDS",
    "DOWNLOAD_READ_TIMEOUT_SECONDS",
    "FFMPEG_TIMEOUT_SECONDS",
    "FFPROBE_TIMEOUT_SECONDS",
    "MAX_DOWNLOAD_BYTES",
    "MAX_DOWNLOAD_REDIRECTS",
    "MAX_PHOTO_DIMENSION",
    "MAX_TELEGRAM_PHOTO_BYTES",
    "MAX_TELEGRAM_VIDEO_BYTES",
    "MAX_VIDEO_DIMENSION",
    "MAX_VIDEO_DURATION_SECONDS",
    "MIN_CANDIDATE_HEIGHT",
    "MIN_CANDIDATE_WIDTH",
    "MIN_UPSCALE_GUARD_DIMENSION",
    "TARGET_PHOTO_BYTES",
    "TARGET_VIDEO_BYTES",
]
