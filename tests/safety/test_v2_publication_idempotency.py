"""Idempotency of a v2 (rendering.plan) publication plan — Step 5 section 37; updated
in Step 6C for the single-post invariant.

A BundlePlan built by rendering.plan.build_publication_plan() is exactly the same
Step/Component/BundlePlan shape publishing/plan.py's own build_plan() produces, so it
runs through the existing, already-tested publishing/rich.py execute()/remaining_steps()
machinery unmodified.

Step 6C removed the two-step "short caption on the photo, then the full post as a
second message" variants entirely — a long post is now shortened to fit a single
caption instead of ever producing a second Telegram message. This file now proves the
single-post invariant itself (every plan is exactly one step, so there is nothing left
to partially fail across two sends) and that a successful single-step publish is never
resent on resume.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_news_editor.domain.enums import DraftStatus, PublicationStatus
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
    """Long enough that the full post would not fit a caption — proves the shortened
    single-step path, never a second message, still round-trips through execute()."""
    long_text = "Дуже детальний абзац з важливими фактами про подію. " * 10
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


class TestSinglePostInvariantHolds:
    def test_a_long_post_with_a_photo_is_exactly_one_plan_step(self, tmp_path: Path) -> None:
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = MediaOutcome(media=_processed_media(workspace))
            variant, plan = build_publication_plan(_long_content(), outcome, workspace)

        assert variant is PlanVariant.PHOTO_SHORT_CAPTION
        assert len(plan.steps) == 1
        assert plan.steps[0].component is Component.MAIN


class TestPhotoPublishIsNotResentOnRetry:
    def test_a_successful_photo_publish_is_never_resent(
        self,
        connection: sqlite3.Connection,
        seeded_article: Any,
        drafts: DraftRepository,
        tmp_path: Path,
    ) -> None:
        with MediaWorkspace(root=tmp_path) as workspace:
            outcome = MediaOutcome(media=_processed_media(workspace))
            variant, plan = build_publication_plan(_long_content(), outcome, workspace)
            assert variant is PlanVariant.PHOTO_SHORT_CAPTION

            draft, stored = _stored_draft(connection, drafts, seeded_article)
            publication = _placeholder_publication(connection, draft, stored)

            transport = Recorder()
            with TelegramClient(TOKEN, transport=transport) as client:
                execute(
                    connection, client, plan, stored,
                    publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                    discussion_chat_id=None, media_root=workspace.root,
                )

            assert transport.count("sendPhoto") == 1
            assert transport.calls == ["sendPhoto"]  # never a second, follow-up send
            assert Component.MAIN in ComponentRepository(connection).succeeded(stored.id)

            todo, unknown = remaining_steps(connection, plan, stored)
            assert todo == ()
            assert unknown == set()

            # A resume/retry call must not resend anything at all.
            retry_transport = Recorder()
            with TelegramClient(TOKEN, transport=retry_transport) as client:
                execute(
                    connection, client, plan, stored,
                    publication_id=publication.id, draft_id=draft.id, channel=CHANNEL,
                    discussion_chat_id=None, media_root=workspace.root,
                )
            assert retry_transport.calls == []


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
            assert len(plan.steps) == 1

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
