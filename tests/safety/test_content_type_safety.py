"""The approval gate does not care what kind of post it is. Never skip these.

Phase 7.5 adds two content formats that this newsroom writes itself. The risk it
introduces is a tempting one: content we wrote is content we trust, so why review it?
Because trusting our own output is exactly how an invented fact reaches a channel.

Every assertion here is one the news pipeline already satisfies, re-run for prompts and
explainers. If any of them can be answered differently depending on content type, the
gate has a hole.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

import httpx
import pytest

from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    ContentType,
    DraftStatus,
    PostFormat,
    PromptTopic,
)
from ai_news_editor.domain.errors import NotApprovedError
from ai_news_editor.domain.models import ContentItem, ExplainerBody, PromptBody
from ai_news_editor.publishing.gate import approve_draft, authorization_for_approved_draft
from ai_news_editor.publishing.service import prepare_publication, publish_draft
from ai_news_editor.publishing.telegram import TelegramClient, TelegramPublisher
from ai_news_editor.storage.repositories import ContentItemRepository, DraftRepository
from ai_news_editor.writing.format import render_version

pytestmark = pytest.mark.safety

CHANNEL = "@test_channel"
TOKEN = "123456789:" + "A" * 35

POST_BODY = (
    "Ось що можна зробити: надішліть список продуктів, які є вдома, і попросіть три "
    "прості страви. Далі — готовий текст, який можна скопіювати й вставити у будь-який "
    "AI-чат. Змініть його під себе: вкажіть, скільки часу у вас є."
)


class RecordingTransport(httpx.MockTransport):
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.sent.append(json.loads(request.content) if request.content else {})
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 7, "chat": {"id": -1}}}
            )

        super().__init__(handler)


def make_item(connection: sqlite3.Connection, content_type: ContentType) -> ContentItem:
    body: PromptBody | ExplainerBody
    if content_type is ContentType.PROMPT:
        body = PromptBody(
            what_you_can_do="швидко вирішити, що приготувати",
            prompt_text="Я надішлю список продуктів. Запропонуй три прості страви з них.",
            customization_tips=("вкажіть, скільки часу у вас є",),
        )
        topic: PromptTopic | None = PromptTopic.FOOD
    else:
        body = ExplainerBody(
            concept="Промпт",
            simple_explanation="Промпт — це те, що ви пишете ШІ.",
            real_life_example="Як записка колезі.",
            why_it_matters="Від формулювання залежить відповідь.",
        )
        topic = None

    return ContentItemRepository(connection).add(
        ContentItem(
            content_type=content_type,
            audience=AudienceTier.NEWCOMER,
            title=f"item-{uuid4().hex[:6]}",
            topic=topic,
            body=body,
            created_by="claude-code",
        )
    )


def make_draft(
    connection: sqlite3.Connection, content_type: ContentType
) -> tuple[Any, Any]:
    item = make_item(connection, content_type)
    drafts = DraftRepository(connection)
    draft, version = drafts.create(
        content_item_id=item.id,
        content_type=content_type,
        title="✨ Спробуйте цей промпт",
        body=POST_BODY,
        category=Category.EVERYDAY_AI,
        audience=AudienceTier.NEWCOMER,
        source_attribution="Матеріал каналу",
        source_url=None,
        post_format=PostFormat.QUICK,
        created_by="claude-code:content_v2",
    )
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    return drafts.get(draft.id), version


@pytest.mark.parametrize("content_type", [ContentType.PROMPT, ContentType.EXPLAINER])
class TestEditorialOriginalGetsNoShortcut:
    def test_it_lands_in_pending_review_not_approved(
        self, connection: sqlite3.Connection, content_type: ContentType
    ) -> None:
        draft, _ = make_draft(connection, content_type)
        assert draft.status is DraftStatus.PENDING_REVIEW

    def test_it_has_no_authorization_before_a_human_approves(
        self, connection: sqlite3.Connection, content_type: ContentType
    ) -> None:
        draft, _ = make_draft(connection, content_type)
        assert authorization_for_approved_draft(connection, draft.id) is None

    def test_it_cannot_reach_telegram_unapproved(
        self, connection: sqlite3.Connection, content_type: ContentType
    ) -> None:
        draft, _ = make_draft(connection, content_type)
        transport = RecordingTransport()
        with (
            TelegramClient(TOKEN, transport=transport) as client,
            pytest.raises(NotApprovedError),
        ):
            plan = prepare_publication(connection, draft.id, channel=CHANNEL)
            publish_draft(connection, plan, TelegramPublisher(client, CHANNEL))
        assert transport.sent == []

    def test_it_publishes_only_after_an_explicit_approval(
        self, connection: sqlite3.Connection, content_type: ContentType
    ) -> None:
        draft, version = make_draft(connection, content_type)
        approve_draft(connection, draft.id, actor="marina")

        transport = RecordingTransport()
        with TelegramClient(TOKEN, transport=transport) as client:
            plan = prepare_publication(connection, draft.id, channel=CHANNEL)
            publish_draft(connection, plan, TelegramPublisher(client, CHANNEL))

        assert len(transport.sent) == 1
        assert transport.sent[0]["text"] == render_version(version)

    def test_the_authorization_binds_the_same_way_as_news(
        self, connection: sqlite3.Connection, content_type: ContentType
    ) -> None:
        draft, version = make_draft(connection, content_type)
        authorization = approve_draft(connection, draft.id)
        assert authorization.content_hash == version.content_hash
        assert authorization.authorizes(version)


class TestNoFakeProvenance:
    def test_an_editorial_draft_has_no_article(
        self, connection: sqlite3.Connection
    ) -> None:
        draft, _ = make_draft(connection, ContentType.PROMPT)
        assert draft.article_id is None
        assert draft.content_item_id is not None

    def test_the_database_refuses_an_editorial_draft_with_an_article(
        self, connection: sqlite3.Connection, seeded_article
    ) -> None:
        """Enforced in SQL too, not only by the model."""
        item = make_item(connection, ContentType.PROMPT)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO drafts (id, article_id, content_item_id, content_type, status, "
                "created_at, updated_at) VALUES (?, ?, ?, 'PROMPT', 'DRAFTED', 'n', 'n')",
                (str(uuid4()), str(seeded_article.id), str(item.id)),
            )

    def test_the_database_refuses_a_news_draft_without_an_article(
        self, connection: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO drafts (id, article_id, content_item_id, content_type, status, "
                "created_at, updated_at) VALUES (?, NULL, NULL, 'NEWS', 'DRAFTED', 'n', 'n')",
                (str(uuid4()),),
            )

    def test_the_database_refuses_an_unknown_content_type(
        self, connection: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO drafts (id, content_item_id, content_type, status, created_at, "
                "updated_at) VALUES (?, ?, 'HOW_TO', 'DRAFTED', 'n', 'n')",
                (str(uuid4()), str(uuid4())),
            )

    def test_an_editorial_post_carries_no_source_line(
        self, connection: sqlite3.Connection
    ) -> None:
        """No source exists, so inventing an attribution line would be a small lie."""
        _draft, version = make_draft(connection, ContentType.EXPLAINER)
        assert "🔗 Джерело" not in render_version(version)

    def test_a_news_post_still_carries_its_source_line(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        """The regression that matters most: news must not lose its attribution."""
        from tests.conftest import DRAFT_CONTENT

        _draft, version = drafts.create(
            article_id=seeded_article.id, **DRAFT_CONTENT  # type: ignore[arg-type]
        )
        assert "🔗 Джерело" in render_version(version)


class TestNewcomerIsStorable:
    def test_a_newcomer_draft_version_is_accepted(
        self, connection: sqlite3.Connection
    ) -> None:
        _draft, version = make_draft(connection, ContentType.PROMPT)
        assert version.audience is AudienceTier.NEWCOMER

    def test_the_database_still_refuses_an_unknown_audience(
        self, connection: sqlite3.Connection
    ) -> None:
        item = make_item(connection, ContentType.PROMPT)
        draft_id = str(uuid4())
        connection.execute(
            "INSERT INTO drafts (id, content_item_id, content_type, status, created_at, "
            "updated_at) VALUES (?, ?, 'PROMPT', 'DRAFTED', 'n', 'n')",
            (draft_id, str(item.id)),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO draft_versions (id, draft_id, version_no, title, body, "
                "category, audience, source_attribution, content_hash, created_by, created_at) "
                "VALUES (?, ?, 1, 't', 'b', 'EVERYDAY_AI', 'EXPERT', 'a', 'h', 'c', 'n')",
                (str(uuid4()), draft_id),
            )
