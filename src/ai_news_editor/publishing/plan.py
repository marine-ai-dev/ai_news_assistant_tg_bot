"""Turning an approved bundle into an ordered list of Telegram calls.

A rich post is several messages: an image, the text, a comment in the discussion group,
a PDF. Telegram has no transaction across them. So the plan is built first — completely,
deterministically, before anything is sent — and every step is recorded as it completes.

Two properties follow from that, and both matter more than they look:

**The plan is decided before the first send.** A dry run shows exactly the calls a real
run would make, because it is the same function. Nothing is worked out halfway through.

**Steps are resumable individually.** If the text goes out and the comment fails, the
text is never sent again. The recorded components say which parts exist, and a retry
picks up only what is missing.

The caption question, and why it is answered this way
-----------------------------------------------------

Telegram limits a photo caption to far less than a message body — a third of the posts
in this project already exceed it. Truncating an approved post to fit is out of the
question: a human approved specific words, and cutting them changes what was approved
into something nobody read.

So the approved text always ships **in full, in one message**. When a post has one image
and the text fits a caption, they go together. When it does not fit, the image goes
first without a caption and the text follows immediately. Both are decided here, visible
in the dry run, and neither drops a character.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ai_news_editor.domain.enums import MediaOrigin
from ai_news_editor.domain.models import DraftVersion, MediaAsset
from ai_news_editor.media.limits import MAX_TELEGRAM_VIDEO_BYTES
from ai_news_editor.publishing.message import build_message

#: Bot API caption limit, in UTF-16 code units. The exact figure could not be re-read
#: from the documentation in this session — the page truncates before the sendPhoto
#: parameter table — so the long-standing documented value is used and every plan is
#: checked against it. Being wrong in the safe direction costs one extra message.
MAX_CAPTION_CHARS = 1024

#: A media group carries between two and ten items.
MEDIA_GROUP_MIN = 2
MEDIA_GROUP_MAX = 10

#: What this application is willing to upload. Not a security boundary — the files are
#: the owner's own — but a mistyped path should fail loudly rather than send something
#: strange to a channel.
ALLOWED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
ALLOWED_DOCUMENT_SUFFIXES = frozenset({".pdf"})
#: Step 4 (AI News Agent v2): media.video.process_video only ever writes .mp4.
ALLOWED_VIDEO_SUFFIXES = frozenset({".mp4"})

#: Bots may upload photos up to 10 MB and other files up to 50 MB through the cloud API.
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
#: Step 4: same 50 MB non-photo upload limit as MAX_DOCUMENT_BYTES, imported from
#: media.limits (verified against the Bot API docs there) rather than a second literal.
MAX_VIDEO_BYTES = MAX_TELEGRAM_VIDEO_BYTES


class Component(StrEnum):
    """The parts of a bundle, each published and recorded separately."""

    #: The post itself, with or without an attached image.
    MAIN = "MAIN"
    #: Extra images beyond the one carried with the main message.
    MEDIA = "MEDIA"
    #: The first comment, in the channel's linked discussion group.
    COMMENT = "COMMENT"
    #: A downloadable file.
    RESOURCE = "RESOURCE"


class PlanError(Exception):
    """The approved bundle cannot be turned into a safe sequence of calls."""


@dataclass(frozen=True, slots=True)
class Step:
    """One Telegram call, fully determined before anything is sent."""

    component: Component
    method: str
    #: What a human reads in the dry run. Never contains a token or an absolute path.
    summary: str
    text: str | None = None
    #: Set together with ``text`` — see build_plan. ``None`` means send as plain text;
    #: never left for the caller to work out separately from a bare string, which is
    #: exactly the gap that once let a NEWS post's markup reach Telegram unescaped and
    #: unparsed (rich.run_step used to build its own payload from ``text`` alone).
    parse_mode: str | None = None
    assets: tuple[MediaAsset, ...] = ()
    #: True when this step needs the channel's linked discussion group rather than the
    #: channel itself.
    to_discussion: bool = False

    @property
    def uploads(self) -> bool:
        return bool(self.assets)


@dataclass(frozen=True, slots=True)
class BundlePlan:
    """Every call a publication will make, in order."""

    steps: tuple[Step, ...]
    #: Components that cannot be sent yet and why — a comment with no discussion group
    #: configured, for instance. Recorded rather than silently dropped.
    deferred: tuple[tuple[Component, str], ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def step_for(self, component: Component) -> Step | None:
        return next((s for s in self.steps if s.component is component), None)

    @property
    def components(self) -> tuple[Component, ...]:
        return tuple(step.component for step in self.steps)


def publishable_media(version: DraftVersion) -> tuple[MediaAsset, ...]:
    """Assets this application is willing to upload.

    Source media is excluded on purpose. It belongs to somebody else, it is recorded by
    URL so a reader can go and look, and re-uploading it to a channel is republication
    of another person's work. Everything the channel made itself is fair game.
    """
    return tuple(
        asset for asset in version.media if asset.origin is not MediaOrigin.SOURCE_MEDIA
    )


def check_asset(asset: MediaAsset, media_root: Path) -> Path:
    """Resolve an asset to a real file, or say precisely what is wrong.

    Raises:
        PlanError: missing, outside the media directory, wrong type, or too large.
            Checked before the first send so a bundle never goes out half-published
            because the second file turned out not to exist.
    """
    candidate = (media_root / asset.reference).resolve()
    root = media_root.resolve()
    if not candidate.is_relative_to(root):
        raise PlanError(
            f"{asset.reference!r} resolves outside the media directory; a post may only "
            "publish files that belong to it"
        )
    if not candidate.is_file():
        raise PlanError(
            f"the approved bundle needs {asset.reference!r} and it is not there. Nothing "
            "was sent — a missing file is a reason to stop, not to publish part of a post"
        )

    suffix = candidate.suffix.lower()
    is_document = asset.role.value == "PDF"
    is_video = asset.role.value == "VIDEO"
    if is_document:
        allowed = ALLOWED_DOCUMENT_SUFFIXES
    elif is_video:
        allowed = ALLOWED_VIDEO_SUFFIXES
    else:
        allowed = ALLOWED_IMAGE_SUFFIXES
    if suffix not in allowed:
        raise PlanError(
            f"{asset.reference!r} is a {suffix or 'file with no extension'}; expected one "
            f"of {', '.join(sorted(allowed))}"
        )

    if is_document:
        limit = MAX_DOCUMENT_BYTES
    elif is_video:
        limit = MAX_VIDEO_BYTES
    else:
        limit = MAX_PHOTO_BYTES
    size = candidate.stat().st_size
    if size > limit:
        raise PlanError(
            f"{asset.reference!r} is {size // (1024 * 1024)} MB, over the "
            f"{limit // (1024 * 1024)} MB the Bot API accepts"
        )
    return candidate


def build_plan(
    version: DraftVersion,
    *,
    media_root: Path,
    discussion_available: bool,
) -> BundlePlan:
    """Work out every call, before making any of them.

    Raises:
        PlanError: an approved asset is missing or unusable. Raised here, so a bundle
            fails whole rather than arriving in pieces.
    """
    # build_message is the one place text and parse_mode are decided together — see its
    # own docstring. Every Step below carries both, exactly as it computed them, rather
    # than each call site re-deriving (or forgetting to derive) a parse_mode of its own.
    message = build_message(version)
    text = message.payload_text
    parse_mode = message.parse_mode
    media = publishable_media(version)
    images = tuple(a for a in media if a.role.value not in ("PDF", "VIDEO"))
    documents = tuple(a for a in media if a.role.value == "PDF")
    videos = tuple(a for a in media if a.role.value == "VIDEO")

    if videos and images:
        # Step 4 (AI News Agent v2): a version is not expected to carry both — video
        # and a photo/album are two different Telegram methods with two different
        # caption/grouping rules, and mixing them has no clear "what does this post
        # look like" answer. Raised here rather than silently picking one.
        raise PlanError(
            "a draft version may carry a video or images, not both — got "
            f"{len(videos)} video(s) and {len(images)} image(s)"
        )
    if len(videos) > 1:
        raise PlanError(f"a post may carry at most one video, got {len(videos)}")

    # Every file is checked now. Finding a missing image after the text has gone out
    # would leave a post on the channel that a human never approved in that form.
    for asset in (*images, *documents, *videos):
        check_asset(asset, media_root)

    steps: list[Step] = []
    warnings: list[str] = []
    deferred: list[tuple[Component, str]] = []

    fits_caption = len(text) <= MAX_CAPTION_CHARS

    if videos and fits_caption:
        steps.append(
            Step(
                component=Component.MAIN,
                method="sendVideo",
                summary=f"video {videos[0].reference} with the full post as caption",
                text=text,
                parse_mode=parse_mode,
                assets=videos,
            )
        )
    elif videos:
        steps.append(
            Step(
                component=Component.MEDIA,
                method="sendVideo",
                summary=f"video {videos[0].reference}, no caption",
                assets=videos,
            )
        )
        warnings.append(
            f"the post is {len(text)} characters, over the {MAX_CAPTION_CHARS}-character "
            "caption limit, so the video is sent first and the full text follows. "
            "Nothing is truncated."
        )
        steps.append(
            Step(
                component=Component.MAIN,
                method="sendMessage",
                summary="the approved post, in full",
                text=text,
                parse_mode=parse_mode,
            )
        )
    elif images and len(images) == 1 and fits_caption:
        steps.append(
            Step(
                component=Component.MAIN,
                method="sendPhoto",
                summary=f"photo {images[0].reference} with the full post as caption",
                text=text,
                parse_mode=parse_mode,
                assets=images,
            )
        )
    elif images:
        # The text does not fit a caption, or there is more than one image. Media first,
        # then the post in full — never a shortened caption.
        if len(images) == 1:
            steps.append(
                Step(
                    component=Component.MEDIA,
                    method="sendPhoto",
                    summary=f"photo {images[0].reference}, no caption",
                    assets=images,
                )
            )
            warnings.append(
                f"the post is {len(text)} characters, over the {MAX_CAPTION_CHARS}-character "
                "caption limit, so the image is sent first and the full text follows. "
                "Nothing is truncated."
            )
        else:
            if len(images) > MEDIA_GROUP_MAX:
                raise PlanError(
                    f"{len(images)} images is more than the {MEDIA_GROUP_MAX} Telegram "
                    "accepts in one group"
                )
            steps.append(
                Step(
                    component=Component.MEDIA,
                    method="sendMediaGroup",
                    summary=f"{len(images)} images as one album",
                    assets=images,
                )
            )
        steps.append(
            Step(
                component=Component.MAIN,
                method="sendMessage",
                summary="the approved post, in full",
                text=text,
                parse_mode=parse_mode,
            )
        )
    else:
        steps.append(
            Step(
                component=Component.MAIN,
                method="sendMessage",
                summary="the approved post, in full",
                text=text,
                parse_mode=parse_mode,
            )
        )

    if version.comment_text:
        if discussion_available:
            steps.append(
                Step(
                    component=Component.COMMENT,
                    method="sendMessage",
                    summary="the approved comment, as a reply in the discussion group",
                    text=version.comment_text,
                    to_discussion=True,
                )
            )
        else:
            # Never quietly folded into the post. The human approved a post that says
            # "прომпт у коментарях" and a comment to go with it; publishing the post
            # without the comment is a broken promise, and merging them is a different
            # post.
            deferred.append(
                (
                    Component.COMMENT,
                    "the channel has no linked discussion group, so a comment cannot be "
                    "posted. Link a discussion group in Telegram, or revise the draft to "
                    "carry the prompt inline",
                )
            )

    if documents:
        steps.append(
            Step(
                component=Component.RESOURCE,
                method="sendDocument",
                summary=f"file {documents[0].reference}",
                assets=documents[:1],
            )
        )
    elif version.resource is not None and version.resource.asset is not None:
        raise PlanError(  # pragma: no cover - the asset is always in media too
            "the resource names a file that is not in the approved media"
        )

    return BundlePlan(steps=tuple(steps), deferred=tuple(deferred), warnings=tuple(warnings))


def describe(plan: BundlePlan) -> list[str]:
    """The plan as a human reads it in a dry run."""
    lines: list[str] = []
    for index, step in enumerate(plan.steps, start=1):
        target = "discussion group" if step.to_discussion else "channel"
        lines.append(f"{index}. {step.component.value}: {step.method} → {target}")
        lines.append(f"   {step.summary}")
    for component, reason in plan.deferred:
        lines.append(f"—  {component.value}: DEFERRED — {reason}")
    return lines
