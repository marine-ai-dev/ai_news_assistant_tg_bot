"""Rendered post + media outcome -> a testable publication plan — Step 5 sections
18-21, 36; redesigned in Step 6C for the single-post invariant.

Reuses ``publishing.plan``'s own ``Step``/``Component``/``BundlePlan``/``check_asset``
— the machinery that already resends nothing on resume and already renders a dry-run
description — rather than inventing a second, overlapping plan shape. What is new here
is *how* a v2 asset is built: the human-approval draft flow's
``publishing.plan.publishable_media()`` deliberately excludes ``MediaOrigin.SOURCE_MEDIA``
because in that flow it means "a bare URL reference, never downloaded." Here it means
something Step 4's pipeline already downloaded, validated and re-compressed, only ever
reaching this module because its source's own ``MediaPolicy`` already permitted reuse
(``DISCOVER_MEDIA``/``EXPLICIT_REUSE_ALLOWED`` — see ``media.pipeline.select_media``).
That gate already ran; this module does not re-decide it, and does not weaken it for
the human-approval flow, which keeps its own exclusion untouched.

**Single-post invariant (Step 6C):** every plan this function returns has exactly one
``Step``. Real-channel visual review showed the previous "short caption on the photo,
then the full post as a second message" variants publishing as two separate Telegram
messages for one story — which is a duplicate post, not a design the channel wants.
There is no follow-up message any more: ``rendering.caption.plan_caption`` guarantees
its caption fits in a single media caption, escalating full text -> short summary ->
hard truncation until it does, so this module never needs a second step to carry an
overflow. Four shapes remain, one per {has-media, caption-fits-in-full} combination:
TEXT_ONLY, PHOTO_WITH_FULL_CAPTION, VIDEO_WITH_FULL_CAPTION, PHOTO_SHORT_CAPTION,
VIDEO_SHORT_CAPTION.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from ai_news_editor.domain.enums import MediaOrigin, MediaRole
from ai_news_editor.domain.models import MediaAsset
from ai_news_editor.media.models import DiscoveryMethod, MediaKind, MediaOutcome, ProcessedMedia
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.publishing.plan import BundlePlan, Component, Step
from ai_news_editor.rendering.caption import plan_caption
from ai_news_editor.rendering.content import EditorialContent
from ai_news_editor.rendering.render import render_editorial_post

logger = get_logger(__name__)


class PlanVariant(StrEnum):
    TEXT_ONLY = "TEXT_ONLY"
    PHOTO_WITH_FULL_CAPTION = "PHOTO_WITH_FULL_CAPTION"
    VIDEO_WITH_FULL_CAPTION = "VIDEO_WITH_FULL_CAPTION"
    PHOTO_SHORT_CAPTION = "PHOTO_SHORT_CAPTION"
    VIDEO_SHORT_CAPTION = "VIDEO_SHORT_CAPTION"


def build_publication_plan(
    content: EditorialContent, media: MediaOutcome, workspace: MediaWorkspace
) -> tuple[PlanVariant, BundlePlan]:
    """Assemble the testable, dry-run-able plan for one post — always exactly one step.

    Media is optional by construction: any ``media`` outcome that is not ``.ok`` (a
    forbidding policy, a failed download, a corrupt file, anything from Step 4's own
    fallback chain, or — vanishingly rarely now that a branded card is the guaranteed
    universal fallback — the branded-card generator itself failing) produces exactly
    the same ``TEXT_ONLY`` plan a source with no media at all would — never a rejected
    candidate, matching section 22.
    """
    if not media.ok:
        return PlanVariant.TEXT_ONLY, _text_only(content)

    assert media.media is not None
    asset = _asset_for(media.media, workspace)
    caption = plan_caption(content)
    is_photo = media.media.kind is MediaKind.IMAGE
    method = "sendPhoto" if is_photo else "sendVideo"

    if caption.shortened:
        variant = PlanVariant.PHOTO_SHORT_CAPTION if is_photo else PlanVariant.VIDEO_SHORT_CAPTION
        summary = f"{method}, shortened caption (single message — no follow-up)"
    else:
        variant = (
            PlanVariant.PHOTO_WITH_FULL_CAPTION if is_photo else PlanVariant.VIDEO_WITH_FULL_CAPTION
        )
        summary = f"{method} with the full post as caption"

    step = Step(
        component=Component.MAIN,
        method=method,
        summary=summary,
        text=caption.caption,
        parse_mode="MarkdownV2",
        assets=(asset,),
    )
    bundle = BundlePlan(steps=(step,), warnings=caption.warnings)
    logger.info(
        "publication_plan_built",
        extra={
            "variant": variant.value,
            "step_count": len(bundle.steps),
            "shortened": caption.shortened,
            "length_bucket": caption.length_bucket,
            "caption_chars": len(caption.caption),
        },
    )
    return variant, bundle


def _text_only(content: EditorialContent) -> BundlePlan:
    rendered = render_editorial_post(content)
    step = Step(
        component=Component.MAIN,
        method="sendMessage",
        summary="the full post, text only",
        text=rendered.full_text,
        parse_mode="MarkdownV2",
    )
    return BundlePlan(steps=(step,), warnings=rendered.warnings)


def _asset_for(media: ProcessedMedia, workspace: MediaWorkspace) -> MediaAsset:
    # workspace.root itself (not .resolve()'d) is what media.path was built relative
    # to (media.pipeline writes via workspace.path(), never through a resolved root) —
    # resolving only one side here would mismatch on a symlinked temp dir (e.g. macOS
    # /var -> /private/var).
    reference = Path(media.path).relative_to(workspace.root).as_posix()
    role = MediaRole.RESULT_IMAGE if media.kind is MediaKind.IMAGE else MediaRole.VIDEO

    if media.source_method is DiscoveryMethod.GENERATED_CARD:
        # Step 6B: made for the channel, not reused from anywhere — EDITORIAL_ASSET,
        # never SOURCE_MEDIA, and no source_url since nothing was downloaded.
        return MediaAsset(
            role=role,
            origin=MediaOrigin.EDITORIAL_ASSET,
            reference=reference,
            description="a locally-generated branded card (Pillow, no generative AI)",
        )

    description = f"media discovered via {media.source_method.value}"
    if media.required_credit:
        # Step 6B: an open-license asset's legally-required credit line, carried
        # through into the line a human reviewer actually reads.
        description = f"{description} — {media.required_credit}"
    return MediaAsset(
        role=role,
        origin=MediaOrigin.SOURCE_MEDIA,
        reference=reference,
        description=description,
        source_url=media.source_url,
    )


__all__ = ["PlanVariant", "build_publication_plan"]
