"""Video support in the publishing plan/rich-publication path — Step 4 (AI News
Agent v2), sections 15-17, 28.

Mirrors the existing image tests in test_rich_publication.py/test_post_bundle.py, one
level up: check_asset's video branch, build_plan's video steps (caption fits / does
not fit / mixed with images is rejected), TelegramClient.send_video's multipart
upload, and rich.run_step's sendVideo dispatch — inspecting the actual outbound
mocked request, not just the renderer output.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    MediaOrigin,
    MediaRole,
)
from ai_news_editor.domain.models import DraftVersion, MediaAsset
from ai_news_editor.publishing.plan import (
    Component,
    PlanError,
    Step,
    build_plan,
    check_asset,
)
from ai_news_editor.publishing.rich import run_step
from ai_news_editor.publishing.telegram import TelegramClient

pytestmark = pytest.mark.safety

TOKEN = "123456789:" + "A" * 35
CHANNEL = "@test_channel"
SHORT_BODY = "Короткий текст допису, якого вистачає для мінімальної довжини поста."
LONG_BODY = "Дуже довгий текст допису. " * 60  # comfortably over a caption


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    (root / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
    (root / "result.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return root


def video_asset(**overrides: object) -> MediaAsset:
    data: dict[str, object] = {
        "role": MediaRole.VIDEO,
        "origin": MediaOrigin.OWNER_GENERATED,
        "reference": "clip.mp4",
        "description": "коротке відео",
        "tool_used": "ffmpeg",
    }
    data.update(overrides)
    return MediaAsset(**data)  # type: ignore[arg-type]


def image_asset(**overrides: object) -> MediaAsset:
    data: dict[str, object] = {
        "role": MediaRole.RESULT_IMAGE,
        "origin": MediaOrigin.OWNER_GENERATED,
        "reference": "result.png",
        "description": "результат",
        "tool_used": "Gemini",
    }
    data.update(overrides)
    return MediaAsset(**data)  # type: ignore[arg-type]


def version(**overrides: object) -> DraftVersion:
    data: dict[str, object] = {
        "draft_id": uuid4(),
        "version_no": 1,
        "title": "🆕 Заголовок",
        "body": SHORT_BODY,
        "category": Category.EVERYDAY_AI,
        "audience": AudienceTier.NEWCOMER,
        "source_attribution": "🔗 Джерело: X\nhttps://x.invalid",
        "created_by": "test",
    }
    data.update(overrides)
    return DraftVersion(**data)  # type: ignore[arg-type]


class TestCheckAssetVideo:
    def test_a_valid_video_resolves(self, media_root: Path) -> None:
        resolved = check_asset(video_asset(), media_root)
        assert resolved == media_root / "clip.mp4"

    def test_a_wrong_extension_for_a_video_role_is_refused(self, media_root: Path) -> None:
        with pytest.raises(PlanError, match="expected one of"):
            check_asset(video_asset(reference="result.png"), media_root)

    def test_an_oversized_video_is_refused(
        self, media_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ai_news_editor.publishing.plan as plan_module

        monkeypatch.setattr(plan_module, "MAX_VIDEO_BYTES", 10)
        with pytest.raises(PlanError, match="MB"):
            check_asset(video_asset(), media_root)


class TestBuildPlanVideo:
    def test_a_short_post_with_video_travels_as_one_caption_message(
        self, media_root: Path
    ) -> None:
        plan = build_plan(
            version(media=(video_asset(),)), media_root=media_root, discussion_available=False
        )
        assert [s.method for s in plan.steps] == ["sendVideo"]
        assert plan.steps[0].component is Component.MAIN
        assert plan.steps[0].text is not None

    def test_a_long_post_with_video_sends_video_then_text_in_full(
        self, media_root: Path
    ) -> None:
        plan = build_plan(
            version(body=LONG_BODY, media=(video_asset(),)),
            media_root=media_root,
            discussion_available=False,
        )
        from ai_news_editor.publishing.plan import MAX_CAPTION_CHARS

        assert [s.method for s in plan.steps] == ["sendVideo", "sendMessage"]
        assert plan.steps[0].text is None  # no caption
        assert len(plan.steps[1].text or "") > MAX_CAPTION_CHARS
        assert plan.warnings  # explains why it split, per the module's own discipline

    def test_video_and_images_together_is_rejected(self, media_root: Path) -> None:
        with pytest.raises(PlanError, match="video or images, not both"):
            build_plan(
                version(media=(video_asset(), image_asset())),
                media_root=media_root,
                discussion_available=False,
            )

    def test_more_than_one_video_is_rejected(self, media_root: Path) -> None:
        (media_root / "second.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
        with pytest.raises(PlanError, match="at most one video"):
            build_plan(
                version(media=(video_asset(), video_asset(reference="second.mp4"))),
                media_root=media_root,
                discussion_available=False,
            )


class TestSendVideoUpload:
    def test_send_video_uploads_as_multipart(self, media_root: Path) -> None:
        content_types: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            content_types.append(request.headers.get("content-type", ""))
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 9, "chat": {"id": -1}}}
            )

        with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
            sent = client.send_video(CHANNEL, media_root / "clip.mp4", caption="підпис")

        assert sent.message_id == 9
        assert content_types[0].startswith("multipart/form-data")


class TestRunStepSendVideo:
    def test_run_step_dispatches_sendvideo_with_caption_and_parse_mode(
        self, media_root: Path
    ) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["content_type"] = request.headers.get("content-type", "")
            seen["body"] = request.content
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 21, "chat": {"id": -100}}}
            )

        step = Step(
            component=Component.MAIN,
            method="sendVideo",
            summary="video with caption",
            text="*Заголовок*\n\nТекст",
            parse_mode="MarkdownV2",
            assets=(video_asset(),),
        )
        with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
            outcome = run_step(client, step, CHANNEL, media_root, None)

        assert outcome.status.value == "SUCCEEDED"
        assert outcome.message_id == 21
        assert "sendVideo" in str(seen["path"])
        assert str(seen["content_type"]).startswith("multipart/form-data")
        body = seen["body"]
        assert isinstance(body, bytes)
        # MarkdownV2 markup must reach Telegram exactly as build_message computed it —
        # never re-escaped, never dropped — same guarantee sendPhoto already has.
        assert b"parse_mode" in body
        assert b"MarkdownV2" in body
        assert b"\xd0\x97\xd0\xb0\xd0\xb3\xd0\xbe\xd0\xbb\xd0\xbe\xd0\xb2\xd0\xbe\xd0\xba" in body

    def test_run_step_dispatches_sendvideo_without_caption(self, media_root: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 22, "chat": {"id": -100}}}
            )

        step = Step(
            component=Component.MEDIA,
            method="sendVideo",
            summary="video only",
            assets=(video_asset(),),
        )
        with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
            outcome = run_step(client, step, CHANNEL, media_root, None)

        assert outcome.status.value == "SUCCEEDED"
        assert outcome.message_id == 22


class TestSourceMediaNeverUploadedEvenAsVideo:
    def test_source_video_is_excluded_from_publishable_media(self, media_root: Path) -> None:
        borrowed = video_asset(
            origin=MediaOrigin.SOURCE_MEDIA,
            reference="https://x.invalid/v.mp4",
            source_url="https://x.invalid/article",
        )
        plan = build_plan(
            version(media=(borrowed,)), media_root=media_root, discussion_available=False
        )
        assert [s.method for s in plan.steps] == ["sendMessage"]
