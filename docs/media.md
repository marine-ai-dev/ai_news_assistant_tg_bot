# 🖼️ Media pipeline

Step 4 (AI News Agent v2) adds image/video discovery, download, validation and
compression, with Telegram `sendPhoto`/`sendVideo` support. This document explains the
architecture, the one rule everything else defers to, and where each piece lives.

## The one rule

**Media is optional, and never a blocker for a valid text post.** Every ordinary
failure — no candidate found, a broken download, a corrupt file, an oversized source,
ffmpeg being unavailable, a policy that forbids reuse — becomes a normal
`MediaOutcome(media=None, reason=...)`, never an exception a caller has to catch. A
text-only publication is not a degraded outcome of this pipeline; it is one of its two
designed outcomes.

## Architecture

```
article selected
  -> discover candidates (RSS enclosure / og:image / og:video / JSON-LD)
  -> policy gate (source's MediaPolicy)
  -> for each candidate, best first:
       download (SSRF-checked, size-capped, Content-Type-checked)
       validate (decode, dimensions, corruption)
       compress (image or video)
     first success wins; every failure falls through to the next candidate
  -> ProcessedMedia, or a RejectionReason explaining why there is none
```

Modules, each with one job (`src/ai_news_editor/media/`):

| Module | Job |
|---|---|
| `limits.py` | Every hard number, in one place — Telegram wire limits verified against the Bot API docs, this application's own compression targets and safety ceilings. |
| `models.py` | `MediaCandidate`, `MediaOutcome`, `RejectionReason` — structured discovery and an always-explainable non-exception outcome. |
| `workspace.py` | `MediaWorkspace` — a per-publication scratch directory with guaranteed cleanup. |
| `urlsafety.py` | SSRF-hardened URL validation (resolves DNS, not just literal IPs). |
| `download.py` | Streaming downloader: hard size cap, Content-Type check, redirect-by-redirect SSRF re-validation. |
| `discover.py` | RSS enclosure / Open Graph / JSON-LD discovery — structured metadata only. |
| `image.py` | Pillow: decode, EXIF-normalize, resize-down-only, iterative compression. |
| `video.py` | ffmpeg/ffprobe: probe, sanity-check, bounded-timeout transcode. |
| `pipeline.py` | Orchestrates all of the above with the candidate-fallback chain. |

Telegram integration lives where publishing already lived: `publishing/telegram.py`
(`send_video`, mirroring the existing `send_photo`), `publishing/rich.py` (a `sendVideo`
branch in the step dispatcher), and `publishing/plan.py` (video suffix/size checks,
video steps in `build_plan`). Nothing about the existing photo/document/comment
machinery changed — video was added alongside it, in the same shape.

## `RUNNER_TEMP`

`MediaWorkspace` writes under `$RUNNER_TEMP/ai-news-media/<unique-id>/` on a
GitHub-hosted runner — `RUNNER_TEMP` is the runner's own per-job scratch directory.
Locally (and in every test), `RUNNER_TEMP` is unset, so it falls back to the OS temp
directory's own `ai-news-media/` subfolder. Nothing is ever written to the repository
working tree, `automation_state/`, or SQLite — media never becomes a tracked file, a
persisted binary, or a CI artifact.

## Cleanup guarantee

`MediaWorkspace` is a context manager:

```python
with MediaWorkspace(draft_id) as workspace:
    ...
# workspace.root no longer exists — success, Telegram failure, a processing
# exception, or a candidate rejected outright all clean up the same way.
```

Cleanup runs in `__exit__`, which Python guarantees runs whether the `with` block
returned normally or raised. This application does not additionally rely on GitHub
eventually deleting the runner VM — the workspace removes itself explicitly, every
time, proven by tests on both the success and exception paths.

## Discovery strategy

Structured metadata only, in this order of preference: RSS/Atom enclosure (or Media
RSS `media:content`/`media:thumbnail`), Open Graph (`og:image`, `og:video`),
schema.org/JSON-LD `image`/`video` properties. No browser automation, no DOM scraping,
no "first `<img>` on the page" heuristic — those are exactly the techniques that pick
up avatars, logos, tracking pixels and unrelated recommendation thumbnails. A
candidate whose URL signals a logo/icon/favicon, or whose declared dimensions are
below the minimum (200x200), is filtered out before download is even considered.

Only media discovered from the selected article's own feed entry or page metadata may
be associated automatically — never a generic image search, never an unrelated stock
photo. There is no external image-search API anywhere in this pipeline.

## Media policy — copyright, enforced

`MediaPolicy` (`domain/enums.py`, from Step 2) is enforced here, not assumed away:

- **`NO_MEDIA`** (default): no download is ever attempted.
- **`LINK_PREVIEW_ONLY`**: no download, no reupload — Telegram's own unrelated link-
  preview card may still show the source's page image, which this application neither
  controls nor touches.
- **`DISCOVER_MEDIA`**: this pipeline may discover, download, validate and compress a
  candidate — but that only produces a `ProcessedMedia` file this standalone module
  hands back, exactly like Step 3's classification schema was additive and unwired.
  Nothing in this step attaches that file to a draft or a publication automatically:
  `publishing/plan.py`'s `publishable_media()` still excludes `MediaOrigin.SOURCE_MEDIA`
  from every real send, and no code path here creates a `MediaAsset` from a
  `ProcessedMedia` result. Turning "the pipeline processed a candidate" into "it went
  out on the channel" stays a deliberate, separate, human-reviewed decision — this
  policy governs discovery/processing capability, not an automatic republish.
- **`EXPLICIT_REUSE_ALLOWED`**: reuse is only ever assumed where a source's own
  registry entry documents explicit, checkable reuse terms. Not the default for any
  source in the current registry, and — like `DISCOVER_MEDIA` — still does not by
  itself wire a processed file into an actual publication.

`NO_MEDIA` and `LINK_PREVIEW_ONLY` are both checked before any candidate is even
discovered — `select_media` returns `POLICY_FORBIDS` immediately, with zero network
calls.

## Supported formats

**Images**: JPEG, PNG, WebP in; JPEG out (Telegram-safe, alpha flattened onto white,
EXIF orientation normalized, metadata stripped by omission, resized down only —
never upscaled). **Video**: any ffmpeg-readable input; H.264/AAC MP4 with `faststart`
out, resolution capped, duration sanity-checked before transcoding. **Audio**: not
built in this step — scope was kept to what the current source types actually produce
(image and video). The workspace/model types are shaped to add it later without
rework, but no audio-specific pipeline exists yet.

## Limits

Verified against the official Telegram Bot API docs (`sendPhoto`/`sendVideo`
parameter tables) at the time this module was written:

| | Telegram wire limit | This application's own target |
|---|---|---|
| Photo | 10 MB | 4 MB, 2560px longest side |
| Video | 50 MB | 20 MB, 1280px longest side, ≤180s |
| Caption | 1024 characters | (enforced by `publishing/plan.py`, unchanged) |

Download safety (`media/limits.py`): an 80 MB hard cap on any source file (well above
the compression targets, since the source is compressed *down* to them, but bounded so
one pathological URL cannot exhaust runner disk/time), a 10s connect / 30s read
timeout, at most 3 redirects (each re-validated against SSRF rules), a 40-megapixel
decode ceiling (decompression-bomb guard), and bounded ffmpeg/ffprobe subprocess
timeouts (120s / 15s) — no infinite process.

## Fallback order

Within one article: **valid first-party hero image > valid first-party video >
permitted structured image > none.** Images are tried before video (simpler, smaller,
no external dependency); within a kind, higher discovery confidence and larger
declared dimensions go first. Every candidate that fails — download error, wrong
Content-Type, corrupt file, too small once decoded, ffmpeg unavailable, processing
timeout — is skipped in favor of the next one. When every candidate fails, or none
were ever discovered, the outcome is `media=None` with a `RejectionReason`, and the
caller publishes text-only.

## Security

- **SSRF**: `media/urlsafety.py` resolves DNS and validates every resolved address —
  not just a literal IP written in the URL, which is as far as `sources/http.py`'s own
  (deliberately narrower, operator-trusted) `validate_url` goes. Rejects loopback,
  RFC1918 private ranges, link-local, reserved, multicast and unspecified addresses,
  and `localhost`/`*.localhost` by name. Not airtight against DNS rebinding between the
  check and the connection — documented as a known limitation, not silently assumed
  solved.
- **Redirects**: followed one hop at a time (never via httpx's own automatic
  following), each hop re-validated — a redirect to a private host is rejected before
  it is ever requested.
- **Content-Type**: a response whose declared type does not match the expected kind
  (`image/*` or `video/*`) is rejected before its body is read — this is what stops an
  HTML error/login page served with `200 OK` from being treated as a photo.
  A missing Content-Type is treated as failing this check, not as "assume the best."
- **Size**: checked from `Content-Length` up front where present, and enforced again
  while streaming — a server that lies about or omits the header cannot cause an
  unbounded write.
- **Decompression bombs**: the decoded pixel count is checked before any resize is
  attempted, so a small file that would decode to an enormous bitmap is rejected
  before it ever gets that large in memory.
- **Filenames**: never trusted from a remote URL or path. `MediaWorkspace.path()`
  refuses any absolute path or path-traversal component; every file this pipeline
  writes is named by this application, from a hash of the candidate URL.
- **ffmpeg**: invoked with a fixed argument list, never a shell string, and always
  under a bounded timeout that kills the process rather than let it hang.

Every one of these fails closed: an unsafe or unusable URL, file or response falls
back to the next candidate, never assumed safe to proceed with.

## Text-only fallback, always

If a source's policy forbids media, if nothing is discovered, if every candidate fails
for any reason above — the publication proceeds as a text-only post. This pipeline
never turns a valid, already-approved post into a lost publication because an image
failed to download.

## Copyright principle

Discovering a URL is not a license to republish it. The default (`NO_MEDIA`) assumes
nothing may be reused — and that gate is checked once, in `media.pipeline.select_media`,
before any download is even attempted. Everything downstream of a successful
`MediaOutcome` treats that gate as already having run; it is never re-decided or
weakened later just because a renderer would look better with an image.

**Step 4** stopped at producing a processed file: nothing wired that file into an
actual publication, and the human-approval draft flow's `publishing/plan.py` still
excludes `MediaOrigin.SOURCE_MEDIA` from `publishable_media()` outright — correct for
that flow, where `SOURCE_MEDIA` means "a bare URL reference," not something downloaded.

**Step 5** is the step that actually attaches it: `rendering/plan.py` builds a
`Step`/`MediaAsset` directly from a `ProcessedMedia` result, for the new
`pipeline_v2`-generated post path only — never for the human-approval draft flow, whose
own exclusion is untouched. This is safe specifically because the media only ever
reaches `rendering/plan.py` after `media.pipeline.select_media`'s own policy gate
already passed (`DISCOVER_MEDIA`/`EXPLICIT_REUSE_ALLOWED`) — "it's on the official
website" is still never, by itself, treated as permission; only an explicit registry
policy is.
