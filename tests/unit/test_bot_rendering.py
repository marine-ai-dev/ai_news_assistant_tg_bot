"""Cards, labels and callback data. No database, no Telegram, no decisions."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from ai_news_editor.bot import render
from ai_news_editor.bot.callbacks import (
    MAX_CALLBACK_BYTES,
    Action,
    CallbackError,
    decode,
    encode,
)
from ai_news_editor.bot.session import EDIT_TIMEOUT_SECONDS, Session
from ai_news_editor.domain.enums import AudienceTier, ContentType, DraftStatus

DRAFT_ID = UUID("ff6e1a19-f7bf-48a3-b687-c213364b7e07")


class TestCallbackData:
    def test_a_callback_round_trips(self) -> None:
        parsed = decode(encode(Action.APPROVE_CONFIRM, DRAFT_ID, 3))
        assert parsed.action is Action.APPROVE_CONFIRM
        assert parsed.version_no == 3
        assert str(DRAFT_ID).startswith(parsed.draft_prefix)

    @pytest.mark.parametrize("action", list(Action))
    def test_every_action_fits_the_byte_budget(self, action: Action) -> None:
        """callback_data is small. Every string this bot emits has to fit."""
        data = encode(action, DRAFT_ID, 999)
        assert len(data.encode("utf-8")) <= MAX_CALLBACK_BYTES

    def test_callback_data_carries_nothing_sensitive(self) -> None:
        """It travelled to a client and back. Only an action and a reference belong."""
        data = encode(Action.APPROVE, DRAFT_ID, 1)
        assert "http" not in data
        assert len(data.split(":")) == 3

    @pytest.mark.parametrize(
        "bad",
        ["", None, "nonsense", "a:b", "a:x:y:z", "zz:ff6e1a19:1", "a:ff6e1a19:zero",
         "a:ff6e1a19:0", "a::1", "a:../../etc:1", "a:ff6e1a19abcdef:1"],
    )
    def test_unusable_callback_data_is_refused(self, bad: str | None) -> None:
        with pytest.raises(CallbackError):
            decode(bad)

    def test_a_forged_prefix_is_still_only_a_claim(self) -> None:
        """Parsing succeeds; the dispatcher then has to find it in the database."""
        parsed = decode("a:deadbeef:1")
        assert parsed.draft_prefix == "deadbeef"


class TestLabels:
    @pytest.mark.parametrize("content_type", list(ContentType))
    def test_every_content_type_has_a_label(self, content_type: ContentType) -> None:
        assert content_type.value in render.TYPE_LABELS[content_type]

    @pytest.mark.parametrize("audience", list(AudienceTier))
    def test_every_audience_has_a_label(self, audience: AudienceTier) -> None:
        assert audience.value in render.AUDIENCE_LABELS[audience]

    @pytest.mark.parametrize("status", list(DraftStatus))
    def test_every_status_has_a_label(self, status: DraftStatus) -> None:
        assert render.STATUS_LABELS[status]

    def test_newcomer_is_visually_distinct(self) -> None:
        labels = {render.AUDIENCE_LABELS[a] for a in AudienceTier}
        assert len(labels) == len(AudienceTier)


class TestScreens:
    def test_the_welcome_screen_leads_with_what_is_waiting(self) -> None:
        text = render.welcome({"PENDING_REVIEW": 9, "APPROVED": 1, "PUBLISHED": 1})
        assert "9" in text
        assert "/review" in text

    def test_the_status_report_breaks_pending_down_by_type(self) -> None:
        text = render.status_report(
            {"PENDING_REVIEW": 8},
            {ContentType.NEWS: 3, ContentType.PROMPT: 3, ContentType.EXPLAINER: 2},
        )
        assert "📰 NEWS — 3" in text
        assert "🧠 EXPLAINER — 2" in text

    def test_the_empty_queue_screen_reports_outcomes(self) -> None:
        text = render.queue_empty({"APPROVED": 2, "REJECTED": 1, "PUBLISHED": 1})
        assert "Усе переглянуто" in text

    def test_help_says_approval_does_not_publish(self) -> None:
        assert "не публікує" in render.help_text()

    def test_the_denial_says_nothing_about_anything(self) -> None:
        text = render.denied()
        for leak in ("draft", "чернетк", "PENDING", "id", "http"):
            assert leak.lower() not in text.lower()


class TestSession:
    def test_an_edit_intent_is_remembered(self) -> None:
        session = Session()
        version_id = uuid4()
        session.begin_edit(DRAFT_ID, version_id, 1)

        intent = session.active_edit()
        assert intent is not None
        assert intent.version_id == version_id

    def test_an_edit_can_be_cancelled(self) -> None:
        session = Session()
        session.begin_edit(DRAFT_ID, uuid4(), 1)
        session.end_edit()
        assert session.active_edit() is None

    def test_a_stale_edit_intent_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A message typed an hour later is not the edit that was intended."""
        session = Session()
        session.begin_edit(DRAFT_ID, uuid4(), 1)
        intent = session.editing
        assert intent is not None

        monkeypatch.setattr(
            "time.monotonic", lambda: intent.started_at + EDIT_TIMEOUT_SECONDS + 1
        )
        assert session.active_edit() is None

    def test_skipping_is_remembered_only_for_navigation(self) -> None:
        session = Session()
        session.skip(DRAFT_ID)
        assert DRAFT_ID in session.skipped

        session.reset_navigation()
        assert session.skipped == set()

    def test_no_state_here_authorizes_anything(self) -> None:
        """The session holds intent. Nothing in it can stand in for a database check."""
        from dataclasses import fields

        names = {f.name for f in fields(Session)}
        assert names == {"editing", "skipped"}


class TestCardDetail:
    """The parts of a card that only appear for some drafts."""

    def _item(self, **overrides: object):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        version = SimpleNamespace(
            version_no=overrides.get("version_no", 1),
            audience=AudienceTier.NEWCOMER,
            category=SimpleNamespace(value="EVERYDAY_AI"),
            post_format=overrides.get("post_format"),
            writer_notes=overrides.get("writer_notes", ()),
        )
        draft = SimpleNamespace(
            id=DRAFT_ID, content_type=overrides.get("content_type", ContentType.NEWS)
        )
        return SimpleNamespace(
            draft=draft,
            version=version,
            article=overrides.get("article"),
            content_item=overrides.get("content_item"),
            evaluation=None,
            score=overrides.get("score"),
            subject=overrides.get("subject"),
            rendered_post="Текст допису.",
        )

    def test_the_editorial_score_appears_when_there_is_one(self) -> None:
        text = render.review_card(self._item(score=73.2), position=1, total=3)
        assert "оцінка 73" in text

    def test_writer_notes_are_shown_and_marked_as_internal(self) -> None:
        text = render.review_card(
            self._item(writer_notes=("перевірити доступність",)), position=1, total=1
        )
        assert "не публікуються" in text
        assert "перевірити доступність" in text

    def test_the_format_and_length_are_shown_when_a_format_is_declared(self) -> None:
        from types import SimpleNamespace

        text = render.review_card(
            self._item(post_format=SimpleNamespace(value="QUICK")), position=1, total=1
        )
        assert "QUICK" in text

    def test_references_are_shown_for_editorial_content(self) -> None:
        from types import SimpleNamespace

        item = self._item(
            content_type=ContentType.EXPLAINER,
            subject="Промпт",
            content_item=SimpleNamespace(
                evidence=None,
                evidence_status=None,
                references=(
                    SimpleNamespace(label="OpenAI Help", url="https://help.openai.com/x"),
                ),
            ),
        )
        text = render.review_card(item, position=1, total=1)
        assert "Перевірено за" in text
        assert "OpenAI Help" in text

    def test_a_next_keyboard_is_empty_without_an_item(self) -> None:
        assert render.next_keyboard(None) == {"inline_keyboard": []}

    def test_a_next_keyboard_offers_one_button_with_an_item(self) -> None:
        keyboard = render.next_keyboard(self._item())
        assert len(keyboard["inline_keyboard"]) == 1

    def test_empty_history_says_so(self) -> None:
        assert "поки немає" in render.history([])

    def test_history_lists_what_it_is_given(self) -> None:
        assert "версія 1" in render.history(["версія 1 — створено"])
