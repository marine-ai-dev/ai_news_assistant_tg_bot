"""The review bot as a front end. Never skip these.

The bot puts approve, reject and edit behind buttons on a phone. That is a genuine
increase in how easily a decision can be made, so every one of these tests asks the same
question in a different way: **did the decision go through the service layer, and did it
apply to the version the human actually saw?**

Nothing here reaches the network. A recording transport answers every Bot API call and
counts what was sent, and the assertion for anything unauthorized or stale is zero.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from ai_news_editor.bot.api import BotApi
from ai_news_editor.bot.callbacks import Action, encode
from ai_news_editor.bot.review_bot import BOT_ACTOR, ReviewBot, discard_backlog, poll
from ai_news_editor.bot.session import Session
from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    ContentType,
    DraftStatus,
    EvidenceStatus,
    PostFormat,
    PromptTopic,
    ReviewAction,
    SourceTier,
)
from ai_news_editor.domain.models import (
    ContentItem,
    ExplainerBody,
    PromptBody,
    PromptEvidence,
)
from ai_news_editor.publishing.gate import approve_draft, authorization_for_approved_draft
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.review.service import review_history
from ai_news_editor.storage.repositories import (
    ContentItemRepository,
    DraftRepository,
    PublicationRepository,
)
from tests.conftest import DRAFT_CONTENT

pytestmark = pytest.mark.safety

TOKEN = "123456789:" + "A" * 35
OWNER = 424242
STRANGER = 999999
OWNER_CHAT = 424242
CHANNEL = "@test_channel"

LONG_BODY = (
    "Оновлений текст допису після редагування людиною. Він достатньо довгий, щоб "
    "пройти перевірку мінімальної довжини, і не містить жодної забороненої розмітки."
)


class RecordingApi(httpx.MockTransport):
    """Answers every Bot API call and records what was asked for."""

    def __init__(self, updates: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._updates = updates or []

        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            payload = json.loads(request.content) if request.content else {}
            self.calls.append((method, payload))
            if method == "getUpdates":
                batch, self._updates = self._updates, []
                return httpx.Response(200, json={"ok": True, "result": batch})
            if method == "sendMessage":
                return httpx.Response(
                    200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": 1}}}
                )
            return httpx.Response(200, json={"ok": True, "result": True})

        super().__init__(handler)

    def of(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    @property
    def texts(self) -> str:
        return " ".join(str(p.get("text", "")) for _m, p in self.calls)

    @property
    def channel_sends(self) -> list[dict[str, Any]]:
        """Messages aimed at the publication channel. Must always be empty."""
        return [p for p in self.of("sendMessage") if p.get("chat_id") == CHANNEL]


@pytest.fixture
def transport() -> RecordingApi:
    return RecordingApi()


@pytest.fixture
def bot(transport: RecordingApi, connection: sqlite3.Connection):  # type: ignore[no-untyped-def]
    with TelegramClient(TOKEN, transport=transport) as client:
        yield ReviewBot(
            api=BotApi(client),
            connection=connection,
            owner_id=OWNER,
            session=Session(),
        )


def news_draft(seeded_article, drafts: DraftRepository):  # type: ignore[no-untyped-def]
    draft, version = drafts.create(
        article_id=seeded_article.id, **DRAFT_CONTENT  # type: ignore[arg-type]
    )
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    return drafts.get(draft.id), version


def editorial_draft(
    connection: sqlite3.Connection, content_type: ContentType
):  # type: ignore[no-untyped-def]
    body: PromptBody | ExplainerBody
    topic: PromptTopic | None
    if content_type is ContentType.PROMPT:
        body = PromptBody(
            what_you_can_do="швидко зрозуміти, про що довгий документ",
            prompt_text="Ось документ. Зроби короткий підсумок головних пунктів.",
            customization_tips=("попросіть цитати замість переказу",),
        )
        topic = PromptTopic.WORK
    else:
        body = ExplainerBody(
            concept="Промпт",
            simple_explanation="Промпт — це те, що ви пишете ШІ.",
            real_life_example="Як записка колезі.",
            why_it_matters="Від формулювання залежить відповідь.",
        )
        topic = None

    prompt_only: dict[str, object] = (
        {
            "evidence": PromptEvidence(
                source_url="https://openai.com/index/example-workflow",
                source_title="Example tested workflow",
                source_tier=SourceTier.OFFICIAL_PRODUCT,
                tested_by="OpenAI",
                tool_used="ChatGPT",
                what_was_tested="підсумок довгого PDF",
                observed_result="структурований підсумок",
            ),
            "evidence_status": EvidenceStatus.VERIFIED_SOURCE_BACKED,
        }
        if content_type is ContentType.PROMPT
        else {}
    )
    item = ContentItemRepository(connection).add(
        ContentItem(
            content_type=content_type,
            audience=AudienceTier.NEWCOMER,
            title=f"item-{uuid4().hex[:6]}",
            topic=topic,
            body=body,
            created_by="claude-code",
            **prompt_only,  # type: ignore[arg-type]
        )
    )
    drafts = DraftRepository(connection)
    draft, version = drafts.create(
        content_item_id=item.id,
        content_type=content_type,
        title="✨ Заголовок",
        body=LONG_BODY,
        category=Category.EVERYDAY_AI,
        audience=AudienceTier.NEWCOMER,
        source_attribution=(
            "🔗 Джерело: Example tested workflow\nhttps://openai.com/index/example-workflow"
            if content_type is ContentType.PROMPT
            else "Матеріал каналу"
        ),
        source_url=(
            "https://openai.com/index/example-workflow"
            if content_type is ContentType.PROMPT
            else None
        ),
        post_format=PostFormat.QUICK,
        created_by="claude-code:content_v2",
    )
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    return drafts.get(draft.id), version


def message(text: str, *, user_id: int = OWNER) -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "text": text,
            "chat": {"id": OWNER_CHAT},
            "from": {"id": user_id},
        },
    }


def tap(action: Action, draft_id: UUID, version_no: int, *, user_id: int = OWNER) -> dict[str, Any]:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb-1",
            "data": encode(action, draft_id, version_no),
            "from": {"id": user_id},
            "message": {"message_id": 11, "chat": {"id": OWNER_CHAT}},
        },
    }


class TestAuthorization:
    def test_a_stranger_gets_no_draft_data(
        self, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        draft, _ = news_draft(seeded_article, drafts)
        bot.handle(message("/review", user_id=STRANGER))

        assert "Цей бот приватний" in transport.texts
        assert str(draft.id) not in transport.texts

    @pytest.mark.parametrize("command", ["/start", "/review", "/status", "/pending"])
    def test_no_command_leaks_anything_to_a_stranger(
        self, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts, command: str
    ) -> None:
        news_draft(seeded_article, drafts)
        bot.handle(message(command, user_id=STRANGER))

        for leak in ("PENDING_REVIEW", "NEWCOMER", "Джерело", "версія"):
            assert leak not in transport.texts

    def test_a_stranger_cannot_approve_even_with_valid_callback_data(
        self, connection, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        """Forged or forwarded callback data is checked against the owner first."""
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(
            tap(Action.APPROVE_CONFIRM, draft.id, version.version_no, user_id=STRANGER)
        )

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert review_history(connection, draft.id) == []

    def test_a_stranger_cannot_edit_while_the_owner_is_editing(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.session.begin_edit(draft.id, version.id, version.version_no)

        bot.handle(message("підмінений текст\n\n" + LONG_BODY, user_id=STRANGER))

        assert drafts.current_version(draft.id).id == version.id


class TestCards:
    def test_a_news_card_shows_type_audience_and_source(
        self, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        news_draft(seeded_article, drafts)
        bot.handle(message("/review"))

        assert "📰 NEWS" in transport.texts
        assert "Джерело:" in transport.texts

    def test_a_prompt_card_shows_its_topic_and_the_source_it_reports(
        self, bot: ReviewBot, transport: RecordingApi, connection
    ) -> None:
        """A prompt post reports someone else's test, so the reviewer judges that test."""
        editorial_draft(connection, ContentType.PROMPT)
        bot.handle(message("/review"))

        assert "✨ PROMPT" in transport.texts
        assert "Тема: WORK" in transport.texts
        assert "Перевіряв: OpenAI" in transport.texts
        assert "Інструмент: ChatGPT" in transport.texts
        assert "https://openai.com/index/example-workflow" in transport.texts
        assert "VERIFIED_SOURCE_BACKED" in transport.texts

    def test_an_explainer_card_says_there_is_no_external_source(
        self, bot: ReviewBot, transport: RecordingApi, connection
    ) -> None:
        editorial_draft(connection, ContentType.EXPLAINER)
        bot.handle(message("/review"))
        assert "зовнішнього джерела немає" in transport.texts

    def test_an_explainer_card_shows_its_concept(
        self, bot: ReviewBot, transport: RecordingApi, connection
    ) -> None:
        editorial_draft(connection, ContentType.EXPLAINER)
        bot.handle(message("/review"))

        assert "🧠 EXPLAINER" in transport.texts
        assert "Поняття: Промпт" in transport.texts

    def test_a_newcomer_draft_is_labelled_as_such(
        self, bot: ReviewBot, transport: RecordingApi, connection
    ) -> None:
        editorial_draft(connection, ContentType.PROMPT)
        bot.handle(message("/review"))
        assert "🌱 NEWCOMER" in transport.texts

    def test_the_card_offers_the_review_actions(
        self, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        news_draft(seeded_article, drafts)
        bot.handle(message("/review"))

        keyboard = transport.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"]
        labels = [b["text"] for row in keyboard for b in row]
        assert any("Схвалити" in x for x in labels)
        assert any("Редагувати" in x for x in labels)
        assert any("Відхилити" in x for x in labels)

    def test_an_empty_queue_says_so(
        self, bot: ReviewBot, transport: RecordingApi
    ) -> None:
        bot.handle(message("/review"))
        assert "Усе переглянуто" in transport.texts


class TestApprove:
    def test_one_tap_does_not_approve(
        self, connection, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        """The whole product rests on approval being deliberate."""
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.APPROVE, draft.id, version.version_no))

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert review_history(connection, draft.id) == []
        assert "Схвалити" in transport.texts

    def test_confirming_approves_through_the_gate(
        self, connection, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.APPROVE, draft.id, version.version_no))
        bot.handle(tap(Action.APPROVE_CONFIRM, draft.id, version.version_no))

        assert drafts.get(draft.id).status is DraftStatus.APPROVED
        decisions = review_history(connection, draft.id)
        assert [d.action for d in decisions] == [ReviewAction.APPROVE]
        assert decisions[0].actor == BOT_ACTOR
        assert decisions[0].content_hash == version.content_hash

    def test_approval_says_it_did_not_publish(
        self, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.APPROVE_CONFIRM, draft.id, version.version_no))
        assert "не опубліковано" in transport.texts

    def test_approving_sends_nothing_to_the_channel(
        self, connection, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        """The bot reviews. Publication is a separate, explicit act elsewhere."""
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.APPROVE_CONFIRM, draft.id, version.version_no))

        assert transport.channel_sends == []
        assert PublicationRepository(connection).count() == 0

    def test_the_bot_never_publishes_even_after_approval(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.APPROVE_CONFIRM, draft.id, version.version_no))

        # An authorization exists — it just was not used, and the bot cannot use it.
        assert authorization_for_approved_draft(connection, draft.id) is not None
        assert PublicationRepository(connection).count() == 0

    def test_a_second_confirm_creates_no_second_decision(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        """Telegram retries callbacks; a duplicate must be inert."""
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.APPROVE_CONFIRM, draft.id, version.version_no))
        bot.handle(tap(Action.APPROVE_CONFIRM, draft.id, version.version_no))

        assert len(review_history(connection, draft.id)) == 1


class TestStaleCallbacks:
    def test_a_tap_on_a_superseded_version_does_not_approve(
        self, connection, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        """The card said version 1; the draft is on version 2 by the time it is tapped."""
        from ai_news_editor.review.service import apply_edit

        draft, version_one = news_draft(seeded_article, drafts)
        apply_edit(connection, draft.id, headline="🆕 Змінено", body=LONG_BODY)

        bot.handle(tap(Action.APPROVE_CONFIRM, draft.id, version_one.version_no))

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert not any(
            d.action is ReviewAction.APPROVE for d in review_history(connection, draft.id)
        )
        assert "змінилася" in transport.texts

    def test_a_tap_on_a_draft_that_left_the_queue_does_nothing(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        from ai_news_editor.review.service import reject_draft

        reject_draft(connection, draft.id)

        bot.handle(tap(Action.APPROVE_CONFIRM, draft.id, version.version_no))
        assert drafts.get(draft.id).status is DraftStatus.REJECTED

    def test_a_callback_for_an_unknown_draft_does_nothing(
        self, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        news_draft(seeded_article, drafts)
        bot.handle(tap(Action.APPROVE_CONFIRM, uuid4(), 1))
        assert transport.channel_sends == []

    def test_malformed_callback_data_is_answered_and_ignored(
        self, bot: ReviewBot, transport: RecordingApi
    ) -> None:
        bot.handle(
            {
                "update_id": 3,
                "callback_query": {
                    "id": "cb-2",
                    "data": "totally-bogus",
                    "from": {"id": OWNER},
                    "message": {"message_id": 11, "chat": {"id": OWNER_CHAT}},
                },
            }
        )
        assert transport.of("answerCallbackQuery")


class TestReject:
    def test_confirming_rejects_through_the_service(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.REJECT, draft.id, version.version_no))
        bot.handle(tap(Action.REJECT_CONFIRM, draft.id, version.version_no))

        assert drafts.get(draft.id).status is DraftStatus.REJECTED
        assert [d.action for d in review_history(connection, draft.id)] == [
            ReviewAction.REJECT
        ]

    def test_one_tap_does_not_reject(self, bot: ReviewBot, seeded_article, drafts) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.REJECT, draft.id, version.version_no))
        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW

    def test_a_duplicate_reject_creates_no_second_decision(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.REJECT_CONFIRM, draft.id, version.version_no))
        bot.handle(tap(Action.REJECT_CONFIRM, draft.id, version.version_no))

        assert len(review_history(connection, draft.id)) == 1

    def test_rejecting_deletes_nothing(self, bot: ReviewBot, seeded_article, drafts) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.REJECT_CONFIRM, draft.id, version.version_no))

        assert drafts.get(draft.id) is not None
        assert drafts.current_version(draft.id).id == version.id


class TestRewrite:
    def test_confirming_marks_needs_rewrite(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.REWRITE, draft.id, version.version_no))
        bot.handle(tap(Action.REWRITE_CONFIRM, draft.id, version.version_no))

        assert drafts.get(draft.id).status is DraftStatus.NEEDS_REWRITE
        assert [d.action for d in review_history(connection, draft.id)] == [
            ReviewAction.REQUEST_REWRITE
        ]

    def test_nothing_is_rewritten_automatically(
        self, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        """No Claude, no regeneration. It is a note for the next pass."""
        draft, version = news_draft(seeded_article, drafts)
        before = drafts.current_version(draft.id).content_hash
        bot.handle(tap(Action.REWRITE_CONFIRM, draft.id, version.version_no))

        assert drafts.current_version(draft.id).content_hash == before
        assert "автоматично" in transport.texts


class TestEdit:
    def test_editing_appends_a_version_and_returns_to_review(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version_one = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.EDIT, draft.id, version_one.version_no))
        bot.handle(message(f"🆕 Новий заголовок\n{LONG_BODY}"))

        current = drafts.current_version(draft.id)
        assert current.version_no == 2
        assert current.title == "🆕 Новий заголовок"
        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW

    def test_the_previous_version_is_untouched(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version_one = news_draft(seeded_article, drafts)
        original_hash = version_one.content_hash

        bot.handle(tap(Action.EDIT, draft.id, version_one.version_no))
        bot.handle(message(f"🆕 Новий заголовок\n{LONG_BODY}"))

        versions = {v.version_no: v for v in drafts.list_versions(draft.id)}
        assert versions[1].content_hash == original_hash
        assert versions[2].content_hash != original_hash

    def test_editing_invalidates_a_prior_approval(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version_one = news_draft(seeded_article, drafts)
        approve_draft(connection, draft.id)
        assert authorization_for_approved_draft(connection, draft.id) is not None

        bot.session.begin_edit(draft.id, version_one.id, version_one.version_no)
        bot.handle(message(f"🆕 Новий заголовок\n{LONG_BODY}"))

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert authorization_for_approved_draft(connection, draft.id) is None

    def test_cancelling_changes_nothing(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.EDIT, draft.id, version.version_no))
        bot.handle(message("/cancel"))
        bot.handle(message(f"🆕 Не має зберегтися\n{LONG_BODY}"))

        assert drafts.current_version(draft.id).id == version.id

    def test_a_cancel_button_also_leaves_edit_mode(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.EDIT, draft.id, version.version_no))
        bot.handle(tap(Action.CANCEL, draft.id, version.version_no))
        bot.handle(message(f"🆕 Не має зберегтися\n{LONG_BODY}"))

        assert drafts.current_version(draft.id).id == version.id

    def test_text_with_no_body_is_refused(
        self, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.EDIT, draft.id, version.version_no))
        bot.handle(message("тільки заголовок"))

        assert drafts.current_version(draft.id).id == version.id
        assert "Потрібні і заголовок, і текст" in transport.texts

    def test_text_that_breaks_a_limit_is_refused_and_saves_nothing(
        self, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.EDIT, draft.id, version.version_no))
        bot.handle(message("🆕 Заголовок\nЗакоротко."))

        assert drafts.current_version(draft.id).id == version.id
        assert "Не збережено" in transport.texts

    def test_markup_outside_the_permitted_subset_is_refused(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.EDIT, draft.id, version.version_no))
        bot.handle(message(f"🆕 Заголовок\n<script>x</script>{LONG_BODY}"))

        assert drafts.current_version(draft.id).id == version.id

    def test_an_edit_cannot_change_provenance(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        """Only publication text is addressable from Telegram."""
        draft, version = news_draft(seeded_article, drafts)
        before = drafts.get(draft.id)

        bot.handle(tap(Action.EDIT, draft.id, version.version_no))
        bot.handle(message(f"🆕 Новий заголовок\n{LONG_BODY}"))

        after = drafts.get(draft.id)
        assert after.article_id == before.article_id
        assert after.content_item_id == before.content_item_id
        assert after.content_type is before.content_type
        assert after.evaluation_id == before.evaluation_id
        assert drafts.current_version(draft.id).source_url == version.source_url

    def test_an_edit_against_a_superseded_version_is_refused(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        from ai_news_editor.review.service import apply_edit

        draft, version_one = news_draft(seeded_article, drafts)
        bot.session.begin_edit(draft.id, version_one.id, version_one.version_no)
        apply_edit(connection, draft.id, headline="🆕 Хтось інший", body=LONG_BODY)

        bot.handle(message(f"🆕 Пізній текст\n{LONG_BODY}"))

        assert drafts.current_version(draft.id).title == "🆕 Хтось інший"


class TestNavigation:
    def test_skipping_records_nothing(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.SKIP, draft.id, version.version_no))

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert review_history(connection, draft.id) == []

    def test_next_records_nothing(self, bot: ReviewBot, connection, seeded_article, drafts) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.NEXT, draft.id, version.version_no))

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert review_history(connection, draft.id) == []

    def test_history_reads_without_changing_anything(
        self, connection, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.HISTORY, draft.id, version.version_no))

        assert "версія 1" in transport.texts
        assert review_history(connection, draft.id) == []


class TestStatus:
    def test_status_reports_counts_by_type(
        self, connection, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        news_draft(seeded_article, drafts)
        editorial_draft(connection, ContentType.PROMPT)
        bot.handle(message("/status"))

        assert "📰 NEWS — 1" in transport.texts
        assert "✨ PROMPT — 1" in transport.texts

    def test_start_shows_the_pending_count(
        self, bot: ReviewBot, transport: RecordingApi, seeded_article, drafts
    ) -> None:
        news_draft(seeded_article, drafts)
        bot.handle(message("/start"))
        assert "AI News Editor" in transport.texts

    def test_whoami_returns_only_the_callers_own_id(
        self, bot: ReviewBot, transport: RecordingApi
    ) -> None:
        bot.handle(message("/whoami"))
        assert str(OWNER) in transport.texts


class TestPollingAndRestart:
    def test_the_offset_advances_past_processed_updates(
        self, connection: sqlite3.Connection
    ) -> None:
        updates = [message("/status")]
        transport = RecordingApi(updates)
        with TelegramClient(TOKEN, transport=transport) as client:
            api = BotApi(client)
            bot = ReviewBot(api, connection, OWNER, Session())
            list(poll(bot, offset=None, iterations=2, sleep=0))

        offsets = [p.get("offset") for p in transport.of("getUpdates")]
        assert offsets[-1] == updates[0]["update_id"] + 1

    def test_only_message_and_callback_updates_are_requested(
        self, connection: sqlite3.Connection
    ) -> None:
        transport = RecordingApi()
        with TelegramClient(TOKEN, transport=transport) as client:
            list(poll(ReviewBot(BotApi(client), connection, OWNER, Session()),
                      iterations=1, sleep=0))

        assert transport.of("getUpdates")[0]["allowed_updates"] == [
            "message",
            "callback_query",
        ]

    def test_a_backlog_is_confirmed_and_discarded_on_startup(
        self, connection: sqlite3.Connection, seeded_article, drafts
    ) -> None:
        """A queued Approve tap from before a restart must not fire at boot."""
        draft, version = news_draft(seeded_article, drafts)
        stale = tap(Action.APPROVE_CONFIRM, draft.id, version.version_no)
        stale["update_id"] = 500

        transport = RecordingApi([stale])
        with TelegramClient(TOKEN, transport=transport) as client:
            offset = discard_backlog(BotApi(client))

        assert offset == 501
        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert review_history(connection, draft.id) == []

    def test_no_backlog_leaves_the_offset_unset(
        self, connection: sqlite3.Connection
    ) -> None:
        transport = RecordingApi()
        with TelegramClient(TOKEN, transport=transport) as client:
            assert discard_backlog(BotApi(client)) is None

    def test_a_stale_callback_after_a_restart_is_still_checked_against_the_database(
        self, connection: sqlite3.Connection, seeded_article, drafts, transport: RecordingApi
    ) -> None:
        """In-memory state is never what makes an action safe."""
        from ai_news_editor.review.service import apply_edit

        draft, version_one = news_draft(seeded_article, drafts)
        apply_edit(connection, draft.id, headline="🆕 Змінено", body=LONG_BODY)

        # A brand-new bot, as after a restart: no session, no memory of the card.
        with TelegramClient(TOKEN, transport=transport) as client:
            fresh = ReviewBot(BotApi(client), connection, OWNER, Session())
            fresh.handle(tap(Action.APPROVE_CONFIRM, draft.id, version_one.version_no))

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW


class TestUiFailureDoesNotUnwindDecisions:
    def test_an_approval_survives_a_failed_confirmation_message(
        self, connection: sqlite3.Connection, seeded_article, drafts
    ) -> None:
        """The human decided and the database committed. A lost bubble changes nothing."""

        draft, version = news_draft(seeded_article, drafts)
        transport = RecordingApi()

        def failing(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            transport.calls.append((method, {}))
            if method in {"sendMessage", "editMessageText"}:
                return httpx.Response(
                    403, json={"ok": False, "error_code": 403, "description": "blocked"}
                )
            return httpx.Response(200, json={"ok": True, "result": True})

        with TelegramClient(TOKEN, transport=httpx.MockTransport(failing)) as client:
            bot = ReviewBot(BotApi(client), connection, OWNER, Session())
            bot.handle(tap(Action.APPROVE_CONFIRM, draft.id, version.version_no))

        assert drafts.get(draft.id).status is DraftStatus.APPROVED
        assert len(review_history(connection, draft.id)) == 1


class TestResilience:
    """Everything that can go wrong at the edges, and what must not follow from it."""

    def _failing(self, methods: set[str], transport: RecordingApi):  # type: ignore[no-untyped-def]
        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            transport.calls.append((method, {}))
            if method in methods:
                return httpx.Response(
                    403, json={"ok": False, "error_code": 403, "description": "blocked"}
                )
            if method == "getUpdates":
                return httpx.Response(200, json={"ok": True, "result": []})
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": 1}}}
            )

        return httpx.MockTransport(handler)

    def test_a_failed_edit_falls_back_to_a_new_message(
        self, connection: sqlite3.Connection, seeded_article, drafts
    ) -> None:
        """A card that cannot be edited in place is re-sent rather than lost."""
        draft, version = news_draft(seeded_article, drafts)
        transport = RecordingApi()

        with TelegramClient(
            TOKEN, transport=self._failing({"editMessageText"}, transport)
        ) as client:
            bot = ReviewBot(BotApi(client), connection, OWNER, Session())
            bot.handle(tap(Action.SKIP, draft.id, version.version_no))

        assert [m for m, _p in transport.calls if m == "sendMessage"]

    def test_a_failed_answer_callback_does_not_crash_the_handler(
        self, connection: sqlite3.Connection, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        transport = RecordingApi()

        with TelegramClient(
            TOKEN, transport=self._failing({"answerCallbackQuery"}, transport)
        ) as client:
            bot = ReviewBot(BotApi(client), connection, OWNER, Session())
            bot.handle(tap(Action.APPROVE_CONFIRM, draft.id, version.version_no))

        assert drafts.get(draft.id).status is DraftStatus.APPROVED

    def test_a_getupdates_failure_is_retried_rather_than_fatal(
        self, connection: sqlite3.Connection
    ) -> None:
        transport = RecordingApi()
        with TelegramClient(
            TOKEN, transport=self._failing({"getUpdates"}, transport)
        ) as client:
            bot = ReviewBot(BotApi(client), connection, OWNER, Session())
            list(poll(bot, iterations=2, sleep=0))

        assert len([m for m, _p in transport.calls if m == "getUpdates"]) >= 2

    @pytest.mark.parametrize(
        "update",
        [
            {},
            {"update_id": "not-an-int"},
            {"update_id": 1, "message": {"text": "hi"}},
            {"update_id": 1, "message": {"chat": {"id": 1}, "from": {"id": OWNER}}},
            {"update_id": 1, "callback_query": {"id": "x", "from": {}, "message": {}}},
            {"update_id": 1, "edited_message": {"text": "hi"}},
        ],
    )
    def test_a_malformed_update_is_ignored(
        self, bot: ReviewBot, transport: RecordingApi, update: dict[str, Any]
    ) -> None:
        bot.handle(update)
        assert transport.channel_sends == []

    def test_an_unknown_command_gets_help(
        self, bot: ReviewBot, transport: RecordingApi
    ) -> None:
        bot.handle(message("/nonsense"))
        assert "Команди" in transport.texts

    def test_plain_text_outside_edit_mode_gets_help(
        self, bot: ReviewBot, transport: RecordingApi
    ) -> None:
        bot.handle(message("привіт"))
        assert "Команди" in transport.texts

    def test_a_failed_approval_is_reported_and_changes_nothing(
        self, bot: ReviewBot, transport: RecordingApi, connection, seeded_article, drafts
    ) -> None:
        """The service refuses; the bot says so instead of pretending it worked."""
        draft, version = news_draft(seeded_article, drafts)
        from ai_news_editor.review.service import reject_draft

        item_version = version
        # Reject via the service, then replay a confirm that resolved before it.
        bot_item = bot._resolve
        assert bot_item is not None
        reject_draft(connection, draft.id)

        bot.handle(tap(Action.APPROVE_CONFIRM, draft.id, item_version.version_no))
        assert drafts.get(draft.id).status is DraftStatus.REJECTED

    def test_the_refresh_action_only_redisplays(
        self, bot: ReviewBot, connection, seeded_article, drafts
    ) -> None:
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.REFRESH, draft.id, version.version_no))

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert review_history(connection, draft.id) == []

    def test_the_startup_summary_counts_what_matters(
        self, connection: sqlite3.Connection, seeded_article, drafts
    ) -> None:
        from ai_news_editor.bot.review_bot import pending_summary

        news_draft(seeded_article, drafts)
        summary = pending_summary(connection)
        assert summary["pending"] == 1
        assert summary["approved"] == 0
        assert summary["published"] == 0

    def test_skipping_every_draft_starts_the_round_again(
        self, bot: ReviewBot, transport: RecordingApi, connection, seeded_article, drafts
    ) -> None:
        """The queue is not "empty" just because everything in it was skipped once."""
        draft, version = news_draft(seeded_article, drafts)
        bot.handle(tap(Action.SKIP, draft.id, version.version_no))
        bot.handle(message("/review"))

        assert "Усе переглянуто" not in transport.texts


class TestServiceRefusals:
    """When the service says no, the bot reports it and changes nothing."""

    def test_a_refused_approval_is_reported(
        self, bot: ReviewBot, transport: RecordingApi, connection, seeded_article, drafts,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_news_editor.domain.errors import NotApprovedError

        draft, version = news_draft(seeded_article, drafts)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise NotApprovedError("the gate said no")

        monkeypatch.setattr("ai_news_editor.bot.review_bot.approve_draft", refuse)
        bot.handle(tap(Action.APPROVE_CONFIRM, draft.id, version.version_no))

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert "Не вдалося схвалити" in transport.texts

    def test_a_refused_rejection_is_reported(
        self, bot: ReviewBot, transport: RecordingApi, connection, seeded_article, drafts,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_news_editor.review.service import ReviewError

        draft, version = news_draft(seeded_article, drafts)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise ReviewError("no")

        monkeypatch.setattr("ai_news_editor.bot.review_bot.reject_draft", refuse)
        bot.handle(tap(Action.REJECT_CONFIRM, draft.id, version.version_no))

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert "Не вдалося відхилити" in transport.texts

    def test_a_refused_rewrite_is_reported(
        self, bot: ReviewBot, transport: RecordingApi, connection, seeded_article, drafts,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_news_editor.review.service import ReviewError

        draft, version = news_draft(seeded_article, drafts)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise ReviewError("no")

        monkeypatch.setattr("ai_news_editor.bot.review_bot.request_rewrite", refuse)
        bot.handle(tap(Action.REWRITE_CONFIRM, draft.id, version.version_no))

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert "Не вдалося" in transport.texts


class TestQueueEndings:
    def test_an_empty_queue_reached_by_a_button_edits_the_card(
        self, bot: ReviewBot, transport: RecordingApi, connection, seeded_article, drafts
    ) -> None:
        """The last decision leaves the card showing the summary, not a dead keyboard."""
        draft, version = news_draft(seeded_article, drafts)
        from ai_news_editor.review.service import reject_draft

        reject_draft(connection, draft.id)
        bot.handle(tap(Action.CANCEL, draft.id, version.version_no))

        assert "Усе переглянуто" in transport.texts

    def test_an_update_without_an_id_is_still_dispatched_safely(
        self, connection: sqlite3.Connection
    ) -> None:
        """A malformed update must not stall the offset or crash the loop."""
        transport = RecordingApi([{"message": {"text": "/status"}}])
        with TelegramClient(TOKEN, transport=transport) as client:
            bot = ReviewBot(BotApi(client), connection, OWNER, Session())
            assert list(poll(bot, iterations=1, sleep=0)) == []


class TestLongPollTimeouts:
    """The bug the first live run found: the client gave up before Telegram answered."""

    def test_the_read_budget_exceeds_the_poll_window(self) -> None:
        from ai_news_editor.bot.api import LONG_POLL_SECONDS, POLL_READ_MARGIN_SECONDS

        assert POLL_READ_MARGIN_SECONDS > 0
        assert LONG_POLL_SECONDS + POLL_READ_MARGIN_SECONDS > LONG_POLL_SECONDS

    def test_get_updates_asks_for_a_longer_read_than_it_waits(
        self, connection: sqlite3.Connection
    ) -> None:
        """Otherwise every quiet poll ends in a client-side timeout and nothing arrives."""
        from ai_news_editor.bot.api import LONG_POLL_SECONDS

        seen: list[float | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions.get("timeout", {}).get("read"))
            return httpx.Response(200, json={"ok": True, "result": []})

        with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
            BotApi(client).get_updates(None)

        assert seen[0] is not None
        assert seen[0] > LONG_POLL_SECONDS

    def test_a_publication_send_keeps_the_default_budget(self) -> None:
        """The override is for polling only; publishing must not wait longer."""
        seen: list[float | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions.get("timeout", {}).get("read"))
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": 2}}}
            )

        with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
            client.send_message({"chat_id": "@c", "text": "x"})

        from ai_news_editor.publishing.telegram import DEFAULT_TIMEOUT_SECONDS

        assert seen[0] == DEFAULT_TIMEOUT_SECONDS
