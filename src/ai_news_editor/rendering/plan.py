"""Rendered post + media outcome -> a testable publication plan — Step 5 sections
18-21, 36.

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

Five practical shapes fall out of "one optional media item + one caption/text
decision," matching section 36 exactly without a sixth type ever being needed:
TEXT_ONLY, PHOTO_WITH_FULL_CAPTION, VIDEO_WITH_FULL_CAPTION,
PHOTO_SHORT_CAPTION_THEN_TEXT, VIDEO_SHORT_CAPTION_THEN_TEXT.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from ai_news_editor.domain.enums import MediaOrigin, MediaRole
from ai_news_editor.domain.models import MediaAsset
from ai_news_editor.media.models import MediaKind, MediaOutcome, ProcessedMedia
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.publishing.plan import BundlePlan, Component, Step
from ai_news_editor.rendering.caption import plan_caption
from ai_news_editor.rendering.content import EditorialContent
from ai_news_editor.rendering.render import render_editorial_post


class PlanVariant(StrEnum):
    TEXT_ONLY = "TEXT_ONLY"
    PHOTO_WITH_FULL_CAPTION = "PHOTO_WITH_FULL_CAPTION"
    VIDEO_WITH_FULL_CAPTION = "VIDEO_WITH_FULL_CAPTION"
    PHOTO_SHORT_CAPTION_THEN_TEXT = "PHOTO_SHORT_CAPTION_THEN_TEXT"
    VIDEO_SHORT_CAPTION_THEN_TEXT = "VIDEO_SHORT_CAPTION_THEN_TEXT"


def build_publication_plan(
    content: EditorialContent, media: MediaOutcome, workspace: MediaWorkspace
) -> tuple[PlanVariant, BundlePlan]:
    """Assemble the testable, dry-run-able plan for one post.

    Media is optional by construction: any ``media`` outcome that is not ``.ok`` (a
    forbidding policy, a failed download, a corrupt file, anything from Step 4's own
    fallback chain) produces exactly the same ``TEXT_ONLY`` plan a source with no
    media at all would — never a rejected candidate, matching section 22.
    """
    if not media.ok:
        return PlanVariant.TEXT_ONLY, _text_only(content)

    assert media.media is not None
    asset = _asset_for(media.media, workspace)
    caption = plan_caption(content)
    is_photo = media.media.kind is MediaKind.IMAGE
    method = "sendPhoto" if is_photo else "sendVideo"

    if caption.mode == "single":
        variant = (
            PlanVariant.PHOTO_WITH_FULL_CAPTION if is_photo else PlanVariant.VIDEO_WITH_FULL_CAPTION
        )
        step = Step(
            component=Component.MAIN,
            method=method,
            summary=f"{method} with the full post as caption",
            text=caption.caption,
            parse_mode="MarkdownV2",
            assets=(asset,),
        )
        return variant, BundlePlan(steps=(step,), warnings=caption.warnings)

    variant = (
        PlanVariant.PHOTO_SHORT_CAPTION_THEN_TEXT
        if is_photo
        else PlanVariant.VIDEO_SHORT_CAPTION_THEN_TEXT
    )
    assert caption.followup is not None
    media_step = Step(
        component=Component.MEDIA,
        method=method,
        summary=f"{method}, short caption",
        text=caption.caption,
        parse_mode="MarkdownV2",
        assets=(asset,),
    )
    text_step = Step(
        component=Component.MAIN,
        method="sendMessage",
        summary="the full post, unabridged",
        text=caption.followup,
        parse_mode="MarkdownV2",
    )
    return variant, BundlePlan(steps=(media_step, text_step), warnings=caption.warnings)


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
    return MediaAsset(
        role=role,
        origin=MediaOrigin.SOURCE_MEDIA,
        reference=reference,
        description=f"media discovered via {media.source_method.value}",
        source_url=media.source_url,
    )


__all__ = ["PlanVariant", "build_publication_plan"]
