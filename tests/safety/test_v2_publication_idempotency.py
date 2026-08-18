"""Idempotency of a v2 (rendering.plan) publication plan — Step 5 section 37.

A BundlePlan built by rendering.plan.build_publication_plan() is exactly the same
Step/Component/BundlePlan shape publishing/plan.py's own build_plan() produces, so it
runs through the existing, already-tested publishing/rich.py execute()/remaining_steps()
machinery unmodified. This file proves that specifically for the two new
short-caption-then-text variants: if the media step succeeds and the text step fails,
a resume must never resend the media.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_news_editor.domain.enums import DraftStatus, PublicationStatus
from ai_news_editor.domain.errors import TelegramError
from ai_news_editor.domain.models import Publication
from ai_news_editor.media.models import DiscoveryMethod, MediaKind, MediaOutcome, ProcessedMedia
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.publishing.gate import approve_draft
from ai_news_editor.publishing.plan import Component
from ai_news_editor.publishing.rich import ComponentRepository, execute, remaining_steps
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.rendering.content import BodyBlock, EditorialContent
from ai_news_editor.rendering.plan import PlanVariant, build_publication_plan
from ai_news_editor.storage.repositories import DraftRepository, PublicationRepository
from tests.conftest import DRAFT_CONTENT

pytestmark = pytest.mark.safety

CHANNEL = "@test_channel"
TOKEN = "123456789:" + "A" * 35


class Recorder(httpx.MockTransport):
    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.calls: list[str] = []
        failures = failures or {}

        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            self.calls.append(method)
            if failures.get(method) == "fail":
                return httpx.Response(
                    400, json={"ok": False, "error_code": 400, "description": "nope"}
                )
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": -100777}}}
            )

        super().__init__(handler)

    def count(self, method: str) -> int:
        return self.calls.count(method)


def _long_content() -> EditorialContent:
    long_text = "Дуже детальний абзац з важливими фактами про подію. " * 30
    return EditorialContent(
        category="NEWS",  # type: ignore[arg-type]
        evidence="PRIMARY_SOURCE",  # type: ignore[arg-type]
        headline="Google випустила нову функцію",
        body=(
            BodyBlock(purpose="what_happened", text=long_text),
            BodyBlock(purpose="why_it_matters", text=long_text),
        ),
        source_label="Google",
        source_url="https://blog.google/example",
    )


def _processed_media(workspace: MediaWorkspace) -> ProcessedMedia:
    path = workspace.path("processed.jpg")
    path.write_bytes(b"\xff\xd8\xff" + b"0" * 128)
    return ProcessedMedia(
        path=str(path),
        kind=MediaKind.IMAGE,
        width=1200,
        height=630,
        size_bytes=path.stat().st_size,
        source_url="https://blog.google/hero.jpg",
        source_method=DiscoveryMethod.OPEN_GRAPH_IMAGE,
    )


def _stored_draft(connection: sqlite3.Connection, drafts: DraftRepository, seeded_article: Any):
    draft, created = drafts.create(
        article_id=seeded_article.id, **DRAFT_CONTENT  # type: ignore[arg-type]
    )
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    return drafts.get(draft.id), created


def _placeholder_publication(
    connection: sqlite3.Connection, draft: Any, version: Any
) -> Publication:
    authorization = approve_draft(connection, draft.id)
    return PublicationRepository(connection).add(
        Publication(
            draft_id=draft.id,
            draft_version_id=version.id,
            review_decision_id=authorization.decision_id,
            content_hash=version.content_hash,
            channel=CHANNEL,
            status=PublicationStatus.FAILED,
            failure_reason="placeholder attempt row for v2 idempotency tests",
        )
    )


class TestPhotoPartialFailureIsNotResent:
    def test_media_succeeds_text_fails_then_resume_sends_only_text(
        self,
        connection: sqlite3.Connection,
        seeded_article: Any,
        drafts: DraftRepository,
        tmp_path: Path,
    ) -> None:
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = MediaOutcome(media=_processed_media(workspace))
            variant, plan = build_publication_plan(_long_content(), outcome, workspace)
            assert variant is PlanVariant.PHOTO_SHORT_CAPTION_THEN_TEXT

            draft, stored = _stored_draft(connection, drafts, seeded_article)
            publication = _placeholder_publication(connection, draft, stored)

            transport = Recorder(failures={"sendMessage": "fail"})
            with TelegramClient(TOKEN, transport=transport) as client, pytest.raises(TelegramError):
                execute(
                    connection, client, plan, stored,
                    publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                    discussion_chat_id=None, media_root=workspace.root,
                )

            components = ComponentRepository(connection)
            assert Component.MEDIA in components.succeeded(stored.id)
            assert Component.MAIN not in components.succeeded(stored.id)
            assert transport.count("sendPhoto") == 1

            # A retry must see only the text step as outstanding.
            todo, unknown = remaining_steps(connection, plan, stored)
            assert [s.component for s in todo] == [Component.MAIN]
            assert unknown == set()

            retry_transport = Recorder()
            with TelegramClient(TOKEN, transport=retry_transport) as client:
                execute(
                    connection, client, plan, stored,
                    publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                    discussion_chat_id=None, media_root=workspace.root,
                )

            # The retry only ever calls sendMessage — the photo is never resent.
            assert retry_transport.count("sendPhoto") == 0
            assert retry_transport.count("sendMessage") == 1
            assert Component.MAIN in ComponentRepository(connection).succeeded(stored.id)


class TestVideoPartialFailureIsNotResent:
    def test_media_succeeds_text_fails_then_resume_sends_only_text(
        self,
        connection: sqlite3.Connection,
        seeded_article: Any,
        drafts: DraftRepository,
        tmp_path: Path,
    ) -> None:
        with MediaWorkspace(root=tmp_path) as workspace:
            path = workspace.path("processed.mp4")
            path.write_bytes(b"fake-mp4")
            processed = ProcessedMedia(
                path=str(path), kind=MediaKind.VIDEO, width=1200, height=630,
                size_bytes=path.stat().st_size, source_url="https://blog.google/clip.mp4",
                source_method=DiscoveryMethod.OPEN_GRAPH_VIDEO,
            )
            outcome = MediaOutcome(media=processed)
            variant, plan = build_publication_plan(_long_content(), outcome, workspace)
            assert variant is PlanVariant.VIDEO_SHORT_CAPTION_THEN_TEXT

            draft, stored = _stored_draft(connection, drafts, seeded_article)
            publication = _placeholder_publication(connection, draft, stored)

            transport = Recorder(failures={"sendMessage": "fail"})
            with TelegramClient(TOKEN, transport=transport) as client, pytest.raises(TelegramError):
                execute(
                    connection, client, plan, stored,
                    publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                    discussion_chat_id=None, media_root=workspace.root,
                )

            components = ComponentRepository(connection)
            assert Component.MEDIA in components.succeeded(stored.id)
            assert transport.count("sendVideo") == 1

            todo, _unknown = remaining_steps(connection, plan, stored)
            assert [s.component for s in todo] == [Component.MAIN]

            retry_transport = Recorder()
            with TelegramClient(TOKEN, transport=retry_transport) as client:
                execute(
                    connection, client, plan, stored,
                    publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                    discussion_chat_id=None, media_root=workspace.root,
                )

            assert retry_transport.count("sendVideo") == 0
            assert retry_transport.count("sendMessage") == 1


class TestTextOnlyIdempotency:
    def test_a_text_only_plan_is_not_resent_after_success(
        self,
        connection: sqlite3.Connection,
        seeded_article: Any,
        drafts: DraftRepository,
        tmp_path: Path,
    ) -> None:
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = MediaOutcome(media=None)
            variant, plan = build_publication_plan(_long_content(), outcome, workspace)
            assert variant is PlanVariant.TEXT_ONLY

            draft, stored = _stored_draft(connection, drafts, seeded_article)
            publication = _placeholder_publication(connection, draft, stored)

            transport = Recorder()
            with TelegramClient(TOKEN, transport=transport) as client:
                execute(
                    connection, client, plan, stored,
                    publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                    discussion_chat_id=None, media_root=workspace.root,
                )

            todo, unknown = remaining_steps(connection, plan, stored)
            assert todo == ()
            assert unknown == set()

            # A second resume call must not send anything at all.
            second_transport = Recorder()
            with TelegramClient(TOKEN, transport=second_transport) as client:
                execute(
                    connection, client, plan, stored,
                    publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                    discussion_chat_id=None, media_root=workspace.root,
                )
            assert second_transport.calls == []
