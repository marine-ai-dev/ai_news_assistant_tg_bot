"""Publishing a bundle across several Telegram calls. Never skip these.

Telegram has no transaction across messages, so a rich post can go out in pieces. The
question every test here asks is the same one: **after something goes wrong, can the
main post be sent twice?**

It cannot. A comment that failed is an annoyance; a duplicate post on a channel is
something readers see and the owner cannot undo. Everything else — resume, deferral,
uncertainty — serves that.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    DraftStatus,
    MediaOrigin,
    MediaRole,
    PromptPlacement,
    PublicationStatus,
    ResourceType,
)
from ai_news_editor.domain.errors import PublicationOutcomeUncertainError, TelegramError
from ai_news_editor.domain.models import DraftVersion, MediaAsset, ResourceSpec
from ai_news_editor.publishing.plan import (
    MAX_CAPTION_CHARS,
    Component,
    PlanError,
    build_plan,
    describe,
    publishable_media,
)
from ai_news_editor.publishing.rich import ComponentRepository, execute, remaining_steps
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.storage.repositories import DraftRepository, PublicationRepository
from tests.conftest import DRAFT_CONTENT

pytestmark = pytest.mark.safety

#: A syntactically valid but entirely fake bot token. Assembled at runtime rather
#: than written as a literal: secret scanners match the *shape* of a Telegram
#: token and cannot tell a placeholder from a credential, and a repository that
#: cries wolf teaches its owner to ignore the alarm.
TOKEN = "123456789:" + "A" * 35
CHANNEL = "@test_channel"
DISCUSSION = -1009999
SHORT_BODY = "Короткий текст допису, якого вистачає для мінімальної довжини поста."
LONG_BODY = "Дуже довгий текст допису. " * 60  # comfortably over a caption


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    (root / "result.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    (root / "second.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    (root / "collection.pdf").write_bytes(b"%PDF-1.4\n" + b"0" * 64)
    return root


def image(**overrides: object) -> MediaAsset:
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
        "source_url": "https://x.invalid",
        "created_by": "test",
    }
    data.update(overrides)
    return DraftVersion(**data)  # type: ignore[arg-type]


class Recorder(httpx.MockTransport):
    """Answers every call and records the method used."""

    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.calls: list[str] = []
        self.payloads: list[dict[str, Any]] = []
        failures = failures or {}

        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            self.calls.append(method)
            if request.headers.get("content-type", "").startswith("application/json"):
                self.payloads.append(json.loads(request.content))
            else:
                self.payloads.append({})

            mode = failures.get(method)
            if mode == "fail":
                return httpx.Response(
                    400, json={"ok": False, "error_code": 400, "description": "nope"}
                )
            if mode == "timeout":
                raise httpx.ReadTimeout("lost", request=request)
            if method == "getChat":
                return httpx.Response(
                    200,
                    json={"ok": True, "result": {"id": -100777, "type": "channel",
                                                 "linked_chat_id": DISCUSSION}},
                )
            if method == "sendMediaGroup":
                return httpx.Response(
                    200,
                    json={"ok": True, "result": [
                        {"message_id": 11, "chat": {"id": -100777}},
                        {"message_id": 12, "chat": {"id": -100777}},
                    ]},
                )
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": -100777}}}
            )

        super().__init__(handler)

    def count(self, method: str) -> int:
        return self.calls.count(method)


class TestPlanShape:
    def test_a_text_only_post_is_one_message(self, media_root: Path) -> None:
        plan = build_plan(version(), media_root=media_root, discussion_available=False)
        assert [s.method for s in plan.steps] == ["sendMessage"]
        assert plan.components == (Component.MAIN,)

    def test_one_image_with_a_short_post_travels_as_a_caption(self, media_root: Path) -> None:
        plan = build_plan(
            version(media=(image(),)), media_root=media_root, discussion_available=False
        )
        assert [s.method for s in plan.steps] == ["sendPhoto"]
        assert plan.steps[0].text is not None

    def test_a_long_post_is_never_truncated_into_a_caption(self, media_root: Path) -> None:
        """The approved words ship in full, even if that costs an extra message."""
        plan = build_plan(
            version(body=LONG_BODY, media=(image(),)),
            media_root=media_root,
            discussion_available=False,
        )
        assert [s.method for s in plan.steps] == ["sendPhoto", "sendMessage"]
        assert plan.steps[0].text is None
        assert len(plan.steps[1].text or "") > MAX_CAPTION_CHARS
        assert any("truncated" in w for w in plan.warnings)

    def test_several_images_become_an_album_plus_the_post(self, media_root: Path) -> None:
        plan = build_plan(
            version(media=(image(), image(reference="second.png"))),
            media_root=media_root,
            discussion_available=False,
        )
        assert [s.method for s in plan.steps] == ["sendMediaGroup", "sendMessage"]

    def test_a_pdf_becomes_its_own_step(self, media_root: Path) -> None:
        plan = build_plan(
            version(
                media=(image(role=MediaRole.PDF, reference="collection.pdf"),),
                resource=ResourceSpec(
                    resource_type=ResourceType.PDF_COLLECTION, title="t", description="d"
                ),
            ),
            media_root=media_root,
            discussion_available=False,
        )
        assert Component.RESOURCE in plan.components
        assert plan.step_for(Component.RESOURCE).method == "sendDocument"  # type: ignore[union-attr]

    def test_a_comment_becomes_a_discussion_reply(self, media_root: Path) -> None:
        plan = build_plan(
            version(prompt_placement=PromptPlacement.COMMENT, comment_text="повний промпт"),
            media_root=media_root,
            discussion_available=True,
        )
        step = plan.step_for(Component.COMMENT)
        assert step is not None
        assert step.to_discussion is True

    def test_source_media_is_never_uploaded(self, media_root: Path) -> None:
        """It belongs to somebody else. We link to it; we do not republish it."""
        borrowed = MediaAsset(
            role=MediaRole.SOURCE_SCREENSHOT,
            origin=MediaOrigin.SOURCE_MEDIA,
            reference="https://example.invalid/pic.png",
            description="їхній скриншот",
            source_url="https://example.invalid/post",
        )
        assert publishable_media(version(media=(borrowed,))) == ()
        plan = build_plan(
            version(media=(borrowed,)), media_root=media_root, discussion_available=False
        )
        assert [s.method for s in plan.steps] == ["sendMessage"]

    def test_the_plan_is_describable_without_leaking_paths(self, media_root: Path) -> None:
        plan = build_plan(
            version(media=(image(),)), media_root=media_root, discussion_available=False
        )
        text = "\n".join(describe(plan))
        assert "sendPhoto" in text
        assert str(media_root) not in text


class TestCommentDeferral:
    def test_no_discussion_group_defers_the_comment(self, media_root: Path) -> None:
        plan = build_plan(
            version(prompt_placement=PromptPlacement.COMMENT, comment_text="промпт"),
            media_root=media_root,
            discussion_available=False,
        )
        assert plan.step_for(Component.COMMENT) is None
        assert plan.deferred[0][0] is Component.COMMENT

    def test_the_main_post_still_publishes(self, media_root: Path) -> None:
        plan = build_plan(
            version(prompt_placement=PromptPlacement.COMMENT, comment_text="промпт"),
            media_root=media_root,
            discussion_available=False,
        )
        assert Component.MAIN in plan.components

    def test_a_deferred_comment_is_never_folded_into_the_post(self, media_root: Path) -> None:
        """The post says the prompt is in the comments. Merging them is a different post."""
        approved = version(prompt_placement=PromptPlacement.COMMENT, comment_text="промпт")
        plan = build_plan(approved, media_root=media_root, discussion_available=False)
        main = plan.step_for(Component.MAIN)
        assert main is not None
        assert "промпт" not in (main.text or "").replace(approved.title, "")


class TestAssetChecks:
    def test_a_missing_file_stops_before_anything_is_sent(self, media_root: Path) -> None:
        with pytest.raises(PlanError, match="not there"):
            build_plan(
                version(media=(image(reference="nope.png"),)),
                media_root=media_root,
                discussion_available=False,
            )

    def test_a_wrong_file_type_is_refused(self, media_root: Path) -> None:
        (media_root / "notes.txt").write_text("hi", encoding="utf-8")
        with pytest.raises(PlanError, match="expected one of"):
            build_plan(
                version(media=(image(reference="notes.txt"),)),
                media_root=media_root,
                discussion_available=False,
            )

    def test_a_path_outside_the_media_directory_is_refused(self, media_root: Path) -> None:
        with pytest.raises(PlanError, match="outside the media directory"):
            build_plan(
                version(media=(image(reference="../../etc/passwd"),)),
                media_root=media_root,
                discussion_available=False,
            )

    def test_an_oversized_file_is_refused(self, media_root: Path, monkeypatch) -> None:
        monkeypatch.setattr("ai_news_editor.publishing.plan.MAX_PHOTO_BYTES", 8)
        with pytest.raises(PlanError, match="over the"):
            build_plan(
                version(media=(image(),)), media_root=media_root, discussion_available=False
            )

    def test_too_many_images_is_refused(self, media_root: Path) -> None:
        many = tuple(image(reference="result.png") for _ in range(11))
        with pytest.raises(PlanError, match="more than the"):
            build_plan(version(media=many), media_root=media_root, discussion_available=False)


class TestExecution:
    def _stored(self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
                **overrides: object):  # type: ignore[no-untyped-def]
        draft, created = drafts.create(
            article_id=seeded_article.id, **{**DRAFT_CONTENT, **overrides}  # type: ignore[arg-type]
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        return drafts.get(draft.id), created

    def _publication(self, connection: sqlite3.Connection, draft, version):  # type: ignore[no-untyped-def]
        from ai_news_editor.domain.models import Publication
        from ai_news_editor.publishing.gate import approve_draft

        authorization = approve_draft(connection, draft.id)
        return PublicationRepository(connection).add(
            Publication(
                draft_id=draft.id,
                draft_version_id=version.id,
                review_decision_id=authorization.decision_id,
                content_hash=version.content_hash,
                channel=CHANNEL,
                status=PublicationStatus.FAILED,
                failure_reason="placeholder attempt row for component tests",
            )
        )

    def test_every_component_is_recorded(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        draft, stored = self._stored(
            connection, drafts, seeded_article,
            prompt_placement=PromptPlacement.COMMENT, comment_text="повний промпт",
        )
        publication = self._publication(connection, draft, stored)
        plan = build_plan(stored, media_root=media_root, discussion_available=True)
        transport = Recorder()

        with TelegramClient(TOKEN, transport=transport) as client:
            outcomes = execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=DISCUSSION, media_root=media_root,
            )

        assert {o.component for o in outcomes} == {Component.MAIN, Component.COMMENT}
        rows = ComponentRepository(connection).for_publication(publication.id)
        assert {r["status"] for r in rows} == {"SUCCEEDED"}

    def test_a_comment_replies_to_the_main_message(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        draft, stored = self._stored(
            connection, drafts, seeded_article,
            prompt_placement=PromptPlacement.COMMENT, comment_text="повний промпт",
        )
        publication = self._publication(connection, draft, stored)
        plan = build_plan(stored, media_root=media_root, discussion_available=True)
        transport = Recorder()

        with TelegramClient(TOKEN, transport=transport) as client:
            execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=DISCUSSION, media_root=media_root,
            )

        comment_payload = transport.payloads[-1]
        assert comment_payload["chat_id"] == str(DISCUSSION)
        assert comment_payload["reply_parameters"] == {"message_id": 42}

    def test_a_failed_comment_does_not_unsend_the_post(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        """The realistic partial failure, and the one that must not cascade."""
        draft, stored = self._stored(
            connection, drafts, seeded_article,
            prompt_placement=PromptPlacement.COMMENT, comment_text="повний промпт",
        )
        publication = self._publication(connection, draft, stored)
        plan = build_plan(stored, media_root=media_root, discussion_available=True)

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            calls.append(method)
            payload = json.loads(request.content)
            if str(payload.get("chat_id")) == str(DISCUSSION):
                return httpx.Response(
                    400, json={"ok": False, "error_code": 400, "description": "no group"}
                )
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": -100777}}}
            )

        with (
            TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client,
            pytest.raises(TelegramError),
        ):
            execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=DISCUSSION, media_root=media_root,
            )

        components = ComponentRepository(connection)
        assert Component.MAIN in components.succeeded(stored.id)
        assert calls.count("sendMessage") == 2  # the post, then the failing comment

    def test_a_resume_sends_only_what_is_missing(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        """The main post is already on the channel. It must never go out again."""
        draft, stored = self._stored(
            connection, drafts, seeded_article,
            prompt_placement=PromptPlacement.COMMENT, comment_text="повний промпт",
        )
        publication = self._publication(connection, draft, stored)
        plan = build_plan(stored, media_root=media_root, discussion_available=True)

        from ai_news_editor.publishing.rich import ComponentOutcome

        ComponentRepository(connection).add(
            publication_id=publication.id,
            draft_id=draft.id,
            draft_version_id=stored.id,
            outcome=ComponentOutcome(
                component=Component.MAIN, method="sendMessage",
                status=PublicationStatus.SUCCEEDED, message_id=42, chat_id="-100777",
            ),
        )

        todo, unknown = remaining_steps(connection, plan, stored)
        assert [s.component for s in todo] == [Component.COMMENT]
        assert unknown == set()

        transport = Recorder()
        with TelegramClient(TOKEN, transport=transport) as client:
            execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=DISCUSSION, media_root=media_root,
            )
        assert transport.count("sendMessage") == 1

    def test_an_uncertain_component_blocks_the_whole_resume(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        """It may already be on the channel. Deciding that is a human's job."""
        draft, stored = self._stored(connection, drafts, seeded_article)
        publication = self._publication(connection, draft, stored)
        plan = build_plan(stored, media_root=media_root, discussion_available=False)

        from ai_news_editor.publishing.rich import ComponentOutcome

        ComponentRepository(connection).add(
            publication_id=publication.id,
            draft_id=draft.id,
            draft_version_id=stored.id,
            outcome=ComponentOutcome(
                component=Component.MAIN, method="sendMessage",
                status=PublicationStatus.UNCERTAIN, failure_reason="lost",
            ),
        )

        transport = Recorder()
        with (
            TelegramClient(TOKEN, transport=transport) as client,
            pytest.raises(PublicationOutcomeUncertainError, match="unknown state"),
        ):
            execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=None, media_root=media_root,
            )
        assert transport.count("sendMessage") == 0

    def test_a_lost_response_records_uncertainty_and_stops(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        draft, stored = self._stored(connection, drafts, seeded_article)
        publication = self._publication(connection, draft, stored)
        plan = build_plan(stored, media_root=media_root, discussion_available=False)

        transport = Recorder({"sendMessage": "timeout"})
        with (
            TelegramClient(TOKEN, transport=transport) as client,
            pytest.raises(PublicationOutcomeUncertainError),
        ):
            execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=None, media_root=media_root,
            )

        assert Component.MAIN in ComponentRepository(connection).uncertain(stored.id)

    def test_a_deferred_component_is_recorded_as_deferred(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        draft, stored = self._stored(
            connection, drafts, seeded_article,
            prompt_placement=PromptPlacement.COMMENT, comment_text="повний промпт",
        )
        publication = self._publication(connection, draft, stored)
        plan = build_plan(stored, media_root=media_root, discussion_available=False)

        transport = Recorder()
        with TelegramClient(TOKEN, transport=transport) as client:
            outcomes = execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=None, media_root=media_root,
            )

        deferred = [o for o in outcomes if o.status == "DEFERRED"]
        assert [o.component for o in deferred] == [Component.COMMENT]

    def test_an_image_post_uploads_and_records(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        draft, stored = self._stored(connection, drafts, seeded_article, media=(image(),))
        publication = self._publication(connection, draft, stored)
        plan = build_plan(stored, media_root=media_root, discussion_available=False)

        transport = Recorder()
        with TelegramClient(TOKEN, transport=transport) as client:
            execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=None, media_root=media_root,
            )

        assert transport.count("sendPhoto") == 1
        assert Component.MAIN in ComponentRepository(connection).succeeded(stored.id)

    def test_component_history_is_append_only(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        draft, stored = self._stored(connection, drafts, seeded_article)
        publication = self._publication(connection, draft, stored)
        plan = build_plan(stored, media_root=media_root, discussion_available=False)

        transport = Recorder()
        with TelegramClient(TOKEN, transport=transport) as client:
            execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=None, media_root=media_root,
            )

        for statement in (
            "UPDATE publication_components SET status = 'FAILED'",
            "DELETE FROM publication_components",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)


class TestClientUploads:
    """The upload calls themselves, over a mock transport."""

    def test_a_photo_is_uploaded_as_multipart(self, media_root: Path) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("content-type", ""))
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 7, "chat": {"id": -1}}}
            )

        with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
            sent = client.send_photo(CHANNEL, media_root / "result.png", caption="підпис")

        assert sent.message_id == 7
        assert seen[0].startswith("multipart/form-data")

    def test_a_document_is_uploaded(self, media_root: Path) -> None:
        with TelegramClient(TOKEN, transport=Recorder()) as client:
            sent = client.send_document(CHANNEL, media_root / "collection.pdf")
        assert sent.message_id == 42

    def test_a_media_group_returns_one_message_per_item(self, media_root: Path) -> None:
        with TelegramClient(TOKEN, transport=Recorder()) as client:
            messages = client.send_media_group(
                CHANNEL, [media_root / "result.png", media_root / "second.png"]
            )
        assert [m.message_id for m in messages] == [11, 12]

    def test_a_media_group_attaches_each_file_by_name(self, media_root: Path) -> None:
        """The multipart form the Bot API expects: attach://name per item."""
        bodies: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(request.content)
            return httpx.Response(200, json={"ok": True, "result": [
                {"message_id": 1, "chat": {"id": -1}},
            ]})

        with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
            client.send_media_group(CHANNEL, [media_root / "result.png"])

        assert b"attach://file0" in bodies[0]

    def test_a_lost_upload_response_is_uncertain_not_failed(self, media_root: Path) -> None:
        """An upload whose reply is lost may already be on the channel."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("lost", request=request)

        with (
            TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client,
            pytest.raises(PublicationOutcomeUncertainError),
        ):
            client.send_photo(CHANNEL, media_root / "result.png")

    def test_a_channel_with_no_discussion_group_returns_none(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"ok": True, "result": {"id": -100, "type": "channel"}}
            )

        with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
            assert client.linked_discussion_chat(CHANNEL) is None

    def test_a_linked_discussion_group_is_reported(self) -> None:
        with TelegramClient(TOKEN, transport=Recorder()) as client:
            assert client.linked_discussion_chat(CHANNEL) == DISCUSSION


class TestFullBundleExecution:
    """The shapes not yet exercised end to end: album, document, deferral rendering."""

    def _prepared(self, connection, drafts, seeded_article, **overrides):  # type: ignore[no-untyped-def]
        from ai_news_editor.domain.models import Publication
        from ai_news_editor.publishing.gate import approve_draft

        draft, stored = drafts.create(
            article_id=seeded_article.id, **{**DRAFT_CONTENT, **overrides}  # type: ignore[arg-type]
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        authorization = approve_draft(connection, draft.id)
        publication = PublicationRepository(connection).add(
            Publication(
                draft_id=draft.id,
                draft_version_id=stored.id,
                review_decision_id=authorization.decision_id,
                content_hash=stored.content_hash,
                channel=CHANNEL,
                status=PublicationStatus.FAILED,
                failure_reason="placeholder",
            )
        )
        return drafts.get(draft.id), stored, publication

    def test_an_album_post_sends_the_group_then_the_text(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        draft, stored, publication = self._prepared(
            connection, drafts, seeded_article,
            media=(image(), image(reference="second.png")),
        )
        plan = build_plan(stored, media_root=media_root, discussion_available=False)
        transport = Recorder()

        with TelegramClient(TOKEN, transport=transport) as client:
            execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=None, media_root=media_root,
            )

        assert transport.count("sendMediaGroup") == 1
        assert transport.count("sendMessage") == 1
        done = ComponentRepository(connection).succeeded(stored.id)
        assert {Component.MEDIA, Component.MAIN} <= done

    def test_a_pdf_bundle_sends_the_document(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        draft, stored, publication = self._prepared(
            connection, drafts, seeded_article,
            media=(image(role=MediaRole.PDF, reference="collection.pdf"),),
            resource=ResourceSpec(
                resource_type=ResourceType.PDF_COLLECTION, title="Збірка", description="d"
            ),
        )
        plan = build_plan(stored, media_root=media_root, discussion_available=False)
        transport = Recorder()

        with TelegramClient(TOKEN, transport=transport) as client:
            execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=None, media_root=media_root,
            )

        assert transport.count("sendDocument") == 1
        assert Component.RESOURCE in ComponentRepository(connection).succeeded(stored.id)

    def test_a_deferred_component_appears_in_the_description(self, media_root: Path) -> None:
        plan = build_plan(
            version(prompt_placement=PromptPlacement.COMMENT, comment_text="промпт"),
            media_root=media_root,
            discussion_available=False,
        )
        assert any("DEFERRED" in line for line in describe(plan))

    def test_a_step_knows_whether_it_uploads(self, media_root: Path) -> None:
        plan = build_plan(
            version(media=(image(),)), media_root=media_root, discussion_available=False
        )
        assert plan.steps[0].uploads is True

    def test_a_document_may_carry_a_caption(self, media_root: Path) -> None:
        payloads: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(request.content)
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 9, "chat": {"id": -1}}}
            )

        with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
            client.send_document(CHANNEL, media_root / "collection.pdf", caption="опис")

        assert b"caption" in payloads[0]


class TestActualOutboundPayloadCarriesParseMode:
    """The regression class this project actually hit: writing.format.render_post was
    correct, publishing.message.build_message was correct, and none of that mattered
    because publishing.plan.build_plan built its Step.text from render_version()
    directly and rich.run_step's sendMessage/sendPhoto payload never carried a
    parse_mode at all — so Telegram received literal '*' / '[...]  (...)' as plain text.
    These tests inspect the actual JSON body a mock transport receives, the same way a
    real Telegram request would be inspected, not just what build_plan/build_message
    compute in isolation."""

    def _stored(self, connection: sqlite3.Connection, drafts: DraftRepository, seeded_article,
                **overrides: object):  # type: ignore[no-untyped-def]
        draft, created = drafts.create(
            article_id=seeded_article.id, **{**DRAFT_CONTENT, **overrides}  # type: ignore[arg-type]
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        return drafts.get(draft.id), created

    def _publication(self, connection: sqlite3.Connection, draft, version):  # type: ignore[no-untyped-def]
        from ai_news_editor.domain.models import Publication
        from ai_news_editor.publishing.gate import approve_draft

        authorization = approve_draft(connection, draft.id)
        return PublicationRepository(connection).add(
            Publication(
                draft_id=draft.id,
                draft_version_id=version.id,
                review_decision_id=authorization.decision_id,
                content_hash=version.content_hash,
                channel=CHANNEL,
                status=PublicationStatus.FAILED,
                failure_reason="placeholder attempt row for payload-shape tests",
            )
        )

    def test_the_real_sendmessage_payload_carries_parse_mode_markdownv2(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        draft, stored = self._stored(connection, drafts, seeded_article)
        publication = self._publication(connection, draft, stored)
        plan = build_plan(stored, media_root=media_root, discussion_available=False)
        transport = Recorder()

        with TelegramClient(TOKEN, transport=transport) as client:
            execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=None, media_root=media_root,
            )

        send_message_payloads = [
            p for method, p in zip(transport.calls, transport.payloads, strict=True)
            if method == "sendMessage"
        ]
        assert len(send_message_payloads) == 1
        payload = send_message_payloads[0]
        assert payload["parse_mode"] == "MarkdownV2"
        assert "*" in payload["text"]
        assert "[Джерело: Example](https://example.invalid/item)" in payload["text"]
        assert "<b>" not in payload["text"]
        assert "<a href=" not in payload["text"]

    def test_the_real_payload_matches_build_message_exactly(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository,
        media_root: Path,
    ) -> None:
        """The two must never drift apart again — assert bit-for-bit equality with
        what build_message computes in isolation, not just similarity."""
        from ai_news_editor.publishing.message import build_message

        draft, stored = self._stored(connection, drafts, seeded_article)
        publication = self._publication(connection, draft, stored)
        plan = build_plan(stored, media_root=media_root, discussion_available=False)
        transport = Recorder()

        with TelegramClient(TOKEN, transport=transport) as client:
            execute(
                connection, client, plan, stored,
                publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                discussion_chat_id=None, media_root=media_root,
            )

        expected = build_message(stored)
        payload = next(
            p for method, p in zip(transport.calls, transport.payloads, strict=True)
            if method == "sendMessage"
        )
        assert payload["text"] == expected.payload_text
        assert payload["parse_mode"] == expected.parse_mode
