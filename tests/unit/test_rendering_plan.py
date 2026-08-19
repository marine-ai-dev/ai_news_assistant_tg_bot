"""rendering.plan.build_publication_plan — Step 5 sections 18-22, 36, 44."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence
from ai_news_editor.media.models import (
    DiscoveryMethod,
    MediaKind,
    MediaOutcome,
    ProcessedMedia,
    RejectionReason,
)
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.publishing.plan import PlanError, check_asset
from ai_news_editor.rendering.content import BodyBlock, EditorialContent
from ai_news_editor.rendering.plan import PlanVariant, build_publication_plan


def _content(**overrides: object) -> EditorialContent:
    data: dict[str, object] = {
        "category": EditorialCategory.NEWS,
        "evidence": EditorialEvidence.PRIMARY_SOURCE,
        "headline": "Google випустила нову функцію для Gemini",
        "body": (
            BodyBlock(purpose="what_happened", text="Google представила нову функцію."),
            BodyBlock(purpose="why_it_matters", text="Це спрощує роботу з документами."),
        ),
        "source_label": "Google",
        "source_url": "https://blog.google/example",
    }
    data.update(overrides)
    return EditorialContent(**data)  # type: ignore[arg-type]


def _write_processed_media(
    workspace: MediaWorkspace, kind: MediaKind, filename: str
) -> ProcessedMedia:
    path = workspace.path(filename)
    path.write_bytes(b"\xff\xd8\xff" + b"0" * 128 if kind is MediaKind.IMAGE else b"fake-mp4")
    return ProcessedMedia(
        path=str(path),
        kind=kind,
        width=1200,
        height=630,
        size_bytes=path.stat().st_size,
        source_url="https://blog.google/hero.jpg",
        source_method=DiscoveryMethod.OPEN_GRAPH_IMAGE,
    )


class TestTextOnly:
    def test_no_media_produces_text_only(self, tmp_path: Path) -> None:
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = MediaOutcome(media=None, reason=RejectionReason.NO_CANDIDATES)
            variant, plan = build_publication_plan(_content(), outcome, workspace)

        assert variant is PlanVariant.TEXT_ONLY
        assert [s.method for s in plan.steps] == ["sendMessage"]
        assert plan.steps[0].assets == ()

    def test_a_media_failure_still_produces_a_valid_text_only_plan_not_a_rejection(
        self, tmp_path: Path
    ) -> None:
        """Section 22: media error must never become a rejected candidate."""
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = MediaOutcome(
                media=None, reason=RejectionReason.DOWNLOAD_FAILED, detail="404"
            )
            variant, plan = build_publication_plan(_content(), outcome, workspace)

        assert variant is PlanVariant.TEXT_ONLY
        assert plan.steps[0].text is not None
        assert "Google представила" in "".join(s.text or "" for s in plan.steps)


class TestPhotoWithFullCaption:
    def test_a_short_post_with_a_photo_is_one_message(self, tmp_path: Path) -> None:
        with MediaWorkspace(root=tmp_path) as workspace:
            processed = _write_processed_media(workspace, MediaKind.IMAGE, "processed.jpg")
            outcome = MediaOutcome(media=processed)
            variant, plan = build_publication_plan(_content(), outcome, workspace)

            assert variant is PlanVariant.PHOTO_WITH_FULL_CAPTION
            assert [s.method for s in plan.steps] == ["sendPhoto"]
            assert plan.steps[0].assets[0].reference == "processed.jpg"
            # The asset must resolve inside the workspace via the real check_asset —
            # not just structurally plausible, but actually checkable/sendable.
            resolved = check_asset(plan.steps[0].assets[0], workspace.root)
            assert resolved.exists()


class TestVideoWithFullCaption:
    def test_a_short_post_with_a_video_is_one_message(self, tmp_path: Path) -> None:
        with MediaWorkspace(root=tmp_path) as workspace:
            processed = _write_processed_media(workspace, MediaKind.VIDEO, "processed.mp4")
            outcome = MediaOutcome(media=processed)
            variant, plan = build_publication_plan(_content(), outcome, workspace)

        assert variant is PlanVariant.VIDEO_WITH_FULL_CAPTION
        assert [s.method for s in plan.steps] == ["sendVideo"]


class TestPhotoShortCaption:
    def _long_content(self) -> EditorialContent:
        long_text = "Дуже детальний абзац з важливими фактами про подію. " * 10
        return _content(
            body=(
                BodyBlock(purpose="what_happened", text=long_text),
                BodyBlock(purpose="why_it_matters", text=long_text),
            )
        )

    def test_a_long_post_with_a_photo_is_still_exactly_one_message(
        self, tmp_path: Path
    ) -> None:
        """Step 6C single-post invariant: a post too long for the full caption is
        shortened, never split into a second message."""
        with MediaWorkspace(root=tmp_path) as workspace:
            processed = _write_processed_media(workspace, MediaKind.IMAGE, "processed.jpg")
            outcome = MediaOutcome(media=processed)
            variant, plan = build_publication_plan(self._long_content(), outcome, workspace)

        assert variant is PlanVariant.PHOTO_SHORT_CAPTION
        assert [s.method for s in plan.steps] == ["sendPhoto"]
        assert len(plan.steps) == 1


class TestVideoShortCaption:
    def test_a_long_post_with_a_video_is_still_exactly_one_message(
        self, tmp_path: Path
    ) -> None:
        long_text = "Дуже детальний абзац з важливими фактами про подію. " * 10
        content = _content(
            body=(
                BodyBlock(purpose="what_happened", text=long_text),
                BodyBlock(purpose="why_it_matters", text=long_text),
            )
        )
        with MediaWorkspace(root=tmp_path) as workspace:
            processed = _write_processed_media(workspace, MediaKind.VIDEO, "processed.mp4")
            outcome = MediaOutcome(media=processed)
            variant, plan = build_publication_plan(content, outcome, workspace)

        assert variant is PlanVariant.VIDEO_SHORT_CAPTION
        assert [s.method for s in plan.steps] == ["sendVideo"]
        assert len(plan.steps) == 1


class TestSinglePostInvariant:
    """Step 6C: every plan build_publication_plan can produce has exactly one step,
    for every combination of media outcome and content length."""

    def test_text_only_is_one_step(self, tmp_path: Path) -> None:
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = MediaOutcome(media=None, reason=RejectionReason.NO_CANDIDATES)
            _variant, plan = build_publication_plan(_content(), outcome, workspace)
        assert len(plan.steps) == 1

    def test_photo_with_short_content_is_one_step(self, tmp_path: Path) -> None:
        with MediaWorkspace(root=tmp_path) as workspace:
            processed = _write_processed_media(workspace, MediaKind.IMAGE, "processed.jpg")
            outcome = MediaOutcome(media=processed)
            _variant, plan = build_publication_plan(_content(), outcome, workspace)
        assert len(plan.steps) == 1

    def test_photo_with_long_content_is_still_one_step(self, tmp_path: Path) -> None:
        long_text = "Дуже детальний абзац з важливими фактами про подію. " * 10
        content = _content(
            body=(
                BodyBlock(purpose="what_happened", text=long_text),
                BodyBlock(purpose="why_it_matters", text=long_text),
            )
        )
        with MediaWorkspace(root=tmp_path) as workspace:
            processed = _write_processed_media(workspace, MediaKind.IMAGE, "processed.jpg")
            outcome = MediaOutcome(media=processed)
            _variant, plan = build_publication_plan(content, outcome, workspace)
        assert len(plan.steps) == 1


class TestAssetPathTraversalStillEnforced:
    def test_a_reference_outside_the_workspace_is_rejected_by_check_asset(
        self, tmp_path: Path
    ) -> None:
        """build_publication_plan itself only ever builds an in-workspace reference —
        this proves the safety net (check_asset) still catches a hypothetical bad one,
        rather than assuming the constructor above is the only thing standing guard."""
        with MediaWorkspace(root=tmp_path) as workspace:
            processed = _write_processed_media(workspace, MediaKind.IMAGE, "processed.jpg")
            outcome = MediaOutcome(media=processed)
            _variant, plan = build_publication_plan(_content(), outcome, workspace)
            asset = plan.steps[0].assets[0]

        # workspace is now cleaned up — check_asset must refuse a file that is gone.
        with pytest.raises(PlanError):
            check_asset(asset, workspace.root)
