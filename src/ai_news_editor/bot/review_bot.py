"""The private review bot: a front end, and nothing more.

Every editorial decision in this module is made by calling :mod:`review.service` or
:func:`publishing.gate.approve_draft`. Nothing here writes a draft status, builds a
``ReviewDecision``, touches a ``DraftVersion`` or constructs a ``PublishAuthorization``.
A handler's job is to work out what the human meant and ask the service layer for it.

That is not tidiness. The rules about what may be approved, when an approval stops
applying and what a version change means are hard, they are already right, and a second
implementation of them behind a Telegram button is how they would drift apart.

Three properties this module is responsible for:

**Authorization first.** The owner check runs before the draft is loaded, on every
update, message and callback alike. An unauthorized user gets four words and no data —
not a redacted card, not a status count, nothing.

**Callbacks are evidence of a tap, not of a state.** ``callback_data`` came back from a
client and is treated as a claim: this draft, this version. Both are re-read and
re-checked. A tap on a card rendered before an edit does not approve the text that
replaced it.

**A failed UI message never unwinds a committed decision.** If the service approves a
draft and Telegram then refuses the confirmation message, the approval stands and the
send failure is logged. Rolling back a human's decision because a chat bubble did not
arrive would be far worse than a missing bubble.
"""

from __future__ import annotations

import signal
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_news_editor.bot import render
from ai_news_editor.bot.api import BotApi, IncomingCallback, IncomingMessage, parse_update
from ai_news_editor.bot.callbacks import Action, Callback, CallbackError, decode
from ai_news_editor.bot.session import Session
from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import ContentType, DraftStatus
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.publishing.gate import approve_draft
from ai_news_editor.review.service import (
    ReviewError,
    ReviewItem,
    apply_edit,
    reject_draft,
    request_rewrite,
    review_history,
    review_queue,
    status_counts,
)
from ai_news_editor.scheduling import queue as queue_service
from ai_news_editor.scheduling.clock import (
    CHANNEL_TIMEZONE,
    TimeError,
    daypart,
    describe,
    parse_local,
    to_local,
)
from ai_news_editor.storage.repositories import DraftRepository
from ai_news_editor.storage.repositories.publication_queue import PublicationQueueRepository

logger = get_logger(__name__)

#: Recorded against every decision this front end makes, so the audit trail says where
#: a decision came from without pretending it was made by a different person.
BOT_ACTOR = "owner:telegram"

MAX_NOTE_LENGTH = 500


@dataclass
class ReviewBot:
    """Turns Telegram updates into review-service calls."""

    api: BotApi
    connection: sqlite3.Connection
    owner_id: int
    session: Session
    #: Where a scheduled post would go, and where its files live. Both are needed to
    #: answer "can this be scheduled?" honestly rather than optimistically.
    channel: str | None = None
    media_root: Path = Path("media")

    # -- entry points --------------------------------------------------------

    def handle(self, update: dict[str, Any]) -> None:
        """Process one raw update. Never raises for ordinary problems."""
        parsed = parse_update(update)
        if parsed is None:
            return

        # Authorization before anything else touches a draft.
        if parsed.user_id != self.owner_id:
            self._refuse(parsed)
            return

        try:
            if isinstance(parsed, IncomingCallback):
                self._on_callback(parsed)
            else:
                self._on_message(parsed)
        except Exception:  # pragma: no cover - last resort, keeps the loop alive
            logger.exception("review bot failed to handle an update")

    def _refuse(self, parsed: IncomingMessage | IncomingCallback) -> None:
        """Say nothing useful. No draft data, no counts, no confirmation of what exists."""
        logger.warning("unauthorized telegram user", extra={"user_id": parsed.user_id})
        if isinstance(parsed, IncomingCallback):
            self.api.answer_callback(parsed.callback_id, render.denied())
        else:
            self.api.send_message(parsed.chat_id, render.denied())

    # -- messages ------------------------------------------------------------

    def _on_message(self, message: IncomingMessage) -> None:
        text = message.text.strip()

        if text.startswith("/"):
            command = text.split()[0].lstrip("/").split("@")[0].lower()
            self._on_command(message, command)
            return

        if self.session.awaiting_custom_time is not None:
            self._read_custom_time(message, text)
            return

        intent = self.session.active_edit()
        if intent is not None:
            self._apply_edit_text(message, text)
            return

        self.api.send_message(message.chat_id, render.help_text())

    def _on_command(self, message: IncomingMessage, command: str) -> None:
        if command == "start":
            self.api.send_message(
                message.chat_id, render.welcome(status_counts(self.connection))
            )
        elif command in {"review", "pending"}:
            self.session.end_edit()
            self._show_next(message.chat_id)
        elif command in {"approved", "schedule"}:
            self.session.end_schedule()
            self._show_approved(message.chat_id)
        elif command == "queue":
            self._show_queue(message.chat_id)
        elif command == "status":
            self.api.send_message(message.chat_id, self._status_text())
        elif command == "whoami":
            self.api.send_message(message.chat_id, f"Ваш Telegram ID: `{message.user_id}`")
        elif command == "cancel":
            self.session.end_edit()
            self.api.send_message(message.chat_id, "Скасовано. Нічого не змінено.")
        else:
            self.api.send_message(message.chat_id, render.help_text())

    def _status_text(self) -> str:
        counts = status_counts(self.connection)
        pending: dict[ContentType, int] = {}
        for item in review_queue(self.connection):
            pending[item.draft.content_type] = pending.get(item.draft.content_type, 0) + 1
        return render.status_report(counts, pending)

    # -- callbacks -----------------------------------------------------------

    def _on_callback(self, callback: IncomingCallback) -> None:
        try:
            parsed = decode(callback.data)
        except CallbackError as exc:
            logger.warning("unusable callback data", extra={"error": str(exc)})
            self.api.answer_callback(callback.callback_id, "Кнопка застаріла.")
            return

        if parsed.action is Action.CANCEL:
            self.session.end_edit()
            self.api.answer_callback(callback.callback_id, "Скасовано.")
            self._show_next(callback.chat_id, message_id=callback.message_id)
            return

        if parsed.action is Action.NEXT:
            self.api.answer_callback(callback.callback_id)
            self._show_next(callback.chat_id)
            return

        schedule_handler = {
            Action.SCHEDULE: self._offer_schedule,
            Action.SCHEDULE_MORNING: self._pick_daypart,
            Action.SCHEDULE_AFTERNOON: self._pick_daypart,
            Action.SCHEDULE_EVENING: self._pick_daypart,
            Action.SCHEDULE_CUSTOM: self._ask_custom_time,
            Action.SCHEDULE_CONFIRM: self._do_schedule,
            Action.QUEUE_SHOW: self._show_queue_callback,
            Action.QUEUE_CANCEL: self._ask_cancel_schedule,
            Action.QUEUE_CANCEL_CONFIRM: self._do_cancel_schedule,
            Action.QUEUE_RESCHEDULE: self._offer_reschedule,
        }.get(parsed.action)
        if schedule_handler is not None:
            schedule_handler(callback, parsed)
            return

        item = self._resolve(parsed)
        if item is None:
            # The draft moved on, was decided elsewhere, or never matched. Say so and
            # show what is actually current rather than acting on a stale claim.
            self.api.answer_callback(
                callback.callback_id, "Чернетка змінилася. Показую поточну версію."
            )
            self._show_next(callback.chat_id)
            return

        handler = {
            Action.APPROVE: self._ask_approve,
            Action.APPROVE_CONFIRM: self._do_approve,
            Action.REJECT: self._ask_reject,
            Action.REJECT_CONFIRM: self._do_reject,
            Action.REWRITE: self._ask_rewrite,
            Action.REWRITE_CONFIRM: self._do_rewrite,
            Action.EDIT: self._begin_edit,
            Action.SKIP: self._do_skip,
            Action.HISTORY: self._show_history,
            Action.REFRESH: self._refresh,
        }[parsed.action]
        handler(callback, item)

    def _resolve(self, parsed: Callback) -> ReviewItem | None:
        """Find the draft a callback claims, and confirm it still looks like that.

        Returns None when the draft is not in the queue any more or has moved to a
        different version. Both mean the same thing to a caller: do not act, re-display.
        """
        for item in review_queue(self.connection):
            if str(item.draft.id).startswith(parsed.draft_prefix):
                if item.version.version_no != parsed.version_no:
                    logger.info(
                        "stale callback ignored",
                        extra={
                            "draft_id": str(item.draft.id),
                            "shown_version": parsed.version_no,
                            "current_version": item.version.version_no,
                        },
                    )
                    return None
                return item
        return None

    # -- actions -------------------------------------------------------------

    def _ask_approve(self, callback: IncomingCallback, item: ReviewItem) -> None:
        self.api.answer_callback(callback.callback_id)
        self.api.edit_message(
            callback.chat_id,
            callback.message_id,
            render.approve_confirmation(item),
            keyboard=render.confirm_keyboard(item, Action.APPROVE_CONFIRM, "✅ Так, схвалити"),
        )

    def _do_approve(self, callback: IncomingCallback, item: ReviewItem) -> None:
        """The only path to an approval, and it is the gate's own function.

        ``expected_version_id`` is what makes a tap on a stale card fail rather than
        approve text nobody read.
        """
        try:
            approve_draft(
                self.connection,
                item.draft.id,
                actor=BOT_ACTOR,
                expected_version_id=item.version.id,
            )
        except AiNewsError as exc:
            self.api.answer_callback(callback.callback_id, "Не схвалено.", alert=True)
            self.api.send_message(callback.chat_id, f"Не вдалося схвалити: {exc}")
            return

        self.api.answer_callback(callback.callback_id, "Схвалено")
        self.api.edit_message(
            callback.chat_id,
            callback.message_id,
            f"✅ *Схвалено* — версія {item.version.version_no}\n\n"
            "_Допис ще не опубліковано. Публікація — окрема дія._",
        )
        self._show_next(callback.chat_id)

    def _ask_reject(self, callback: IncomingCallback, item: ReviewItem) -> None:
        self.api.answer_callback(callback.callback_id)
        self.api.edit_message(
            callback.chat_id,
            callback.message_id,
            render.reject_confirmation(item),
            keyboard=render.confirm_keyboard(item, Action.REJECT_CONFIRM, "❌ Так, відхилити"),
        )

    def _do_reject(self, callback: IncomingCallback, item: ReviewItem) -> None:
        try:
            reject_draft(
                self.connection,
                item.draft.id,
                actor=BOT_ACTOR,
                expected_version_id=item.version.id,
            )
        except ReviewError as exc:
            self.api.answer_callback(callback.callback_id, "Не відхилено.", alert=True)
            self.api.send_message(callback.chat_id, f"Не вдалося відхилити: {exc}")
            return

        self.api.answer_callback(callback.callback_id, "Відхилено")
        self.api.edit_message(callback.chat_id, callback.message_id, "❌ *Відхилено*")
        self._show_next(callback.chat_id)

    def _ask_rewrite(self, callback: IncomingCallback, item: ReviewItem) -> None:
        self.api.answer_callback(callback.callback_id)
        self.api.edit_message(
            callback.chat_id,
            callback.message_id,
            render.rewrite_confirmation(item),
            keyboard=render.confirm_keyboard(
                item, Action.REWRITE_CONFIRM, "📝 Так, переписати"
            ),
        )

    def _do_rewrite(self, callback: IncomingCallback, item: ReviewItem) -> None:
        try:
            request_rewrite(
                self.connection,
                item.draft.id,
                actor=BOT_ACTOR,
                expected_version_id=item.version.id,
            )
        except ReviewError as exc:
            self.api.answer_callback(callback.callback_id, "Не змінено.", alert=True)
            self.api.send_message(callback.chat_id, f"Не вдалося: {exc}")
            return

        self.api.answer_callback(callback.callback_id, "Повернуто на переписування")
        self.api.edit_message(
            callback.chat_id,
            callback.message_id,
            "📝 *Переписати*\n\n_Нічого не переписується автоматично — це позначка для "
            "наступного проходу._",
        )
        self._show_next(callback.chat_id)

    def _begin_edit(self, callback: IncomingCallback, item: ReviewItem) -> None:
        self.session.begin_edit(item.draft.id, item.version.id, item.version.version_no)
        self.api.answer_callback(callback.callback_id)
        self.api.edit_message(
            callback.chat_id,
            callback.message_id,
            render.edit_instructions(item),
            keyboard=render.cancel_keyboard(item),
        )

    def _apply_edit_text(self, message: IncomingMessage, text: str) -> None:
        """Turn the owner's next message into version N+1, through the review service.

        Only the publication text changes. The draft's identity, origin, content type,
        source and evaluation are not addressable from here — ``apply_edit`` takes a
        headline and a body, and nothing else.
        """
        intent = self.session.active_edit()
        if intent is None:  # pragma: no cover - guarded by the caller
            return

        headline, _, body = text.partition("\n")
        if not headline.strip() or not body.strip():
            self.api.send_message(
                message.chat_id,
                "Потрібні і заголовок, і текст: перший рядок — заголовок, далі — допис.\n"
                "/cancel — вийти без змін.",
            )
            return

        try:
            _draft, version = apply_edit(
                self.connection,
                intent.draft_id,
                headline=headline.strip(),
                body=body.strip(),
                actor=BOT_ACTOR,
                expected_version_id=intent.version_id,
            )
        except ReviewError as exc:
            # Nothing was written. The owner stays in edit mode so they can fix it.
            self.api.send_message(
                message.chat_id, f"Не збережено: {exc}\n\n/cancel — вийти без змін."
            )
            return

        self.session.end_edit()
        self.api.send_message(
            message.chat_id,
            f"✏️ Збережено як версію *{version.version_no}*. Чернетка знову на розгляді"
            + (
                ", попереднє схвалення анульовано."
                if intent.version_no >= 1
                else "."
            ),
        )
        self._show_next(message.chat_id)

    def _do_skip(self, callback: IncomingCallback, item: ReviewItem) -> None:
        """Navigation only: nothing recorded, nothing changed."""
        self.session.skip(item.draft.id)
        self.api.answer_callback(callback.callback_id, "Пропущено")
        self._show_next(callback.chat_id, message_id=callback.message_id)

    def _show_history(self, callback: IncomingCallback, item: ReviewItem) -> None:
        drafts = DraftRepository(self.connection)
        entries = [
            f"версія {v.version_no} — {v.created_at:%Y-%m-%d %H:%M} · {v.created_by}"
            for v in drafts.list_versions(item.draft.id)
        ]
        entries += [
            f"{d.action.value} — {d.created_at:%Y-%m-%d %H:%M} · {d.actor}"
            + (f" · {d.note}" if d.note else "")
            for d in review_history(self.connection, item.draft.id)
        ]
        self.api.answer_callback(callback.callback_id)
        self.api.send_message(callback.chat_id, render.history(entries))

    def _refresh(self, callback: IncomingCallback, item: ReviewItem) -> None:
        self.api.answer_callback(callback.callback_id)
        self._show_next(callback.chat_id, message_id=callback.message_id)


    # -- scheduling (Phase 9) ------------------------------------------------
    #
    # Approved content only, and scheduling is never combined with approving. The
    # cards below are reached from /approved and /queue, which show drafts that a
    # human has already decided about — the review queue itself has no schedule
    # button, because nothing in it is approved yet.

    def _approved_drafts(self) -> list[Any]:
        drafts = DraftRepository(self.connection)
        return drafts.list_by_status(DraftStatus.APPROVED, limit=25)

    def _find_approved(self, prefix: str, version_no: int) -> tuple[Any, Any] | None:
        """Locate an approved draft from a callback, and confirm the card was current.

        Same rule as the review path: a callback is evidence that a button was tapped,
        not evidence about the state of anything. A tap on a card rendered before an
        edit must not schedule the text that replaced it.
        """
        drafts = DraftRepository(self.connection)
        for draft in self._approved_drafts():
            if str(draft.id).startswith(prefix):
                version = drafts.current_version(draft.id)
                if version.version_no != version_no:
                    logger.info(
                        "stale schedule callback ignored",
                        extra={"draft_id": str(draft.id), "shown_version": version_no},
                    )
                    return None
                return draft, version
        return None

    def _schedule_unavailable(self, callback: IncomingCallback) -> bool:
        if self.channel:
            return False
        self.api.answer_callback(callback.callback_id, "Канал не налаштовано.")
        return True

    def _show_approved(self, chat_id: int) -> None:
        """Approved posts, each offered for scheduling — or already scheduled."""
        approved = self._approved_drafts()
        if not approved:
            self.api.send_message(
                chat_id,
                "Немає схвалених дописів. Спочатку /review — і схваліть те, що готове.",
            )
            return

        drafts = DraftRepository(self.connection)
        queue = PublicationQueueRepository(self.connection)
        for draft in approved[:10]:
            version = drafts.current_version(draft.id)
            existing = queue.active_for_version(version.id, self.channel or "")
            when = (
                describe(existing.scheduled_for, existing.display_timezone)
                if existing is not None
                else None
            )
            self.api.send_message(
                chat_id,
                render.approved_card(draft, version, scheduled_for=when),
                keyboard=render.schedule_keyboard(
                    draft, version.version_no, scheduled=existing is not None
                ),
            )

    def _show_queue(self, chat_id: int) -> None:
        queue = PublicationQueueRepository(self.connection)
        drafts = DraftRepository(self.connection)
        rows: list[tuple[str, str, str]] = []
        for item in queue.list_upcoming(limit=20):
            local = to_local(item.scheduled_for, item.display_timezone)
            try:
                version = drafts.get_version(item.draft_version_id)
                draft = drafts.get(item.draft_id)
                icon = render.TYPE_LABELS[draft.content_type].split()[0]
                title = version.title
            except AiNewsError:  # pragma: no cover - a queued version always exists
                icon, title = "•", "(недоступно)"
            rows.append((f"{local:%d %b · %H:%M}", icon, title))
        self.api.send_message(chat_id, render.queue_view(rows))

    def _show_queue_callback(self, callback: IncomingCallback, parsed: Callback) -> None:
        self.api.answer_callback(callback.callback_id)
        self._show_queue(callback.chat_id)

    def _offer_schedule(self, callback: IncomingCallback, parsed: Callback) -> None:
        if self._schedule_unavailable(callback):
            return
        found = self._find_approved(parsed.draft_prefix, parsed.version_no)
        if found is None:
            self.api.answer_callback(callback.callback_id, "Чернетка змінилася.")
            return
        draft, version = found
        self.api.answer_callback(callback.callback_id)
        self.api.edit_message(
            callback.chat_id,
            callback.message_id,
            render.approved_card(draft, version),
            keyboard=render.schedule_keyboard(draft, version.version_no),
        )

    def _offer_reschedule(self, callback: IncomingCallback, parsed: Callback) -> None:
        """Same picker as the first time. Rescheduling replaces the time, never adds one."""
        self._offer_schedule(callback, parsed)

    def _pick_daypart(self, callback: IncomingCallback, parsed: Callback) -> None:
        """A preset chooses a time and asks. It never schedules on its own."""
        if self._schedule_unavailable(callback):
            return
        found = self._find_approved(parsed.draft_prefix, parsed.version_no)
        if found is None:
            self.api.answer_callback(callback.callback_id, "Чернетка змінилася.")
            return
        draft, version = found
        name = {
            Action.SCHEDULE_MORNING: "morning",
            Action.SCHEDULE_AFTERNOON: "afternoon",
            Action.SCHEDULE_EVENING: "evening",
        }[parsed.action]
        try:
            when = daypart(name, now=now_utc())
        except TimeError as exc:
            # Twice a year a preset lands on a local time that does not exist or
            # happens twice. Refused rather than nudged, like any other such time.
            self.api.answer_callback(callback.callback_id, "Оберіть інший час.")
            self.api.send_message(callback.chat_id, str(exc))
            return
        self._propose(callback, draft, version, when)

    def _ask_custom_time(self, callback: IncomingCallback, parsed: Callback) -> None:
        if self._schedule_unavailable(callback):
            return
        found = self._find_approved(parsed.draft_prefix, parsed.version_no)
        if found is None:
            self.api.answer_callback(callback.callback_id, "Чернетка змінилася.")
            return
        draft, version = found
        self.session.ask_for_time(draft.id, version.version_no)
        self.api.answer_callback(callback.callback_id)
        self.api.send_message(callback.chat_id, render.ask_for_time(version.title))

    def _read_custom_time(self, message: IncomingMessage, text: str) -> None:
        """Read a typed date, show what it was understood as, and ask.

        Never schedules directly. What the owner typed and what the bot understood are
        two different things, and the gap between them is where a post goes out on the
        wrong day.
        """
        pending = self.session.awaiting_custom_time
        if pending is None:  # pragma: no cover - guarded by the caller
            return
        draft_id, version_no = pending
        found = self._find_approved(str(draft_id)[:8], version_no)
        if found is None:
            self.session.end_schedule()
            self.api.send_message(message.chat_id, "Чернетка змінилася. Розклад не створено.")
            return
        draft, version = found

        try:
            when = parse_local(text, now=now_utc())
        except TimeError as exc:
            self.api.send_message(message.chat_id, f"{exc}\n\nСпробуйте ще раз: `13.08 14:30`")
            return

        self.session.begin_schedule(
            draft.id, version.id, version.version_no, when, CHANNEL_TIMEZONE
        )
        warnings = [w.message for w in queue_service.warnings_for(self.connection, when)]
        self.api.send_message(
            message.chat_id,
            render.schedule_confirmation(version.title, describe(when), warnings),
            keyboard=render.schedule_confirm_keyboard(draft, version.version_no),
        )

    def _propose(
        self, callback: IncomingCallback, draft: Any, version: Any, when: Any
    ) -> None:
        """Hold the chosen time and ask for a second, deliberate tap."""
        self.session.begin_schedule(
            draft.id, version.id, version.version_no, when, CHANNEL_TIMEZONE
        )
        warnings = [w.message for w in queue_service.warnings_for(self.connection, when)]
        self.api.answer_callback(callback.callback_id)
        self.api.edit_message(
            callback.chat_id,
            callback.message_id,
            render.schedule_confirmation(version.title, describe(when), warnings),
            keyboard=render.schedule_confirm_keyboard(draft, version.version_no),
        )

    def _do_schedule(self, callback: IncomingCallback, parsed: Callback) -> None:
        """The only path that creates a queue item from Telegram.

        The time comes from the session, not from the callback: a timestamp that
        travelled to a client and back is a time this bot did not choose. Everything
        else is re-verified by the queue service, which is the same code the CLI calls.
        """
        if self._schedule_unavailable(callback):
            return
        intent = self.session.active_schedule()
        if intent is None:
            self.api.answer_callback(callback.callback_id, "Час не обрано. Почніть спочатку.")
            return

        found = self._find_approved(parsed.draft_prefix, parsed.version_no)
        if found is None or found[1].id != intent.version_id:
            self.session.end_schedule()
            self.api.answer_callback(callback.callback_id, "Чернетка змінилася.")
            return
        draft, version = found

        try:
            item, warnings = queue_service.schedule(
                self.connection,
                draft.id,
                intent.when,
                channel=self.channel or "",
                media_root=self.media_root,
                actor=BOT_ACTOR,
                timezone_name=intent.timezone_name,
                allow_collision=True,
            )
        except (queue_service.QueueError, AiNewsError) as exc:
            self.session.end_schedule()
            self.api.answer_callback(callback.callback_id, "Не вдалося запланувати.")
            self.api.send_message(callback.chat_id, f"❌ {exc}")
            return

        self.session.end_schedule()
        self.api.answer_callback(callback.callback_id, "Заплановано")
        note = "".join(f"\n⚠️ {w.message}" for w in warnings)
        self.api.edit_message(
            callback.chat_id,
            callback.message_id,
            render.queue_item_card(
                describe(item.scheduled_for, item.display_timezone),
                version.title,
                item.status.value,
                None,
            )
            + note,
            keyboard=render.schedule_keyboard(draft, version.version_no, scheduled=True),
        )

    def _ask_cancel_schedule(self, callback: IncomingCallback, parsed: Callback) -> None:
        found = self._find_approved(parsed.draft_prefix, parsed.version_no)
        if found is None:
            self.api.answer_callback(callback.callback_id, "Чернетка змінилася.")
            return
        draft, version = found
        item = PublicationQueueRepository(self.connection).active_for_version(
            version.id, self.channel or ""
        )
        if item is None:
            self.api.answer_callback(callback.callback_id, "Цей допис не заплановано.")
            return
        self.api.answer_callback(callback.callback_id)
        self.api.edit_message(
            callback.chat_id,
            callback.message_id,
            render.cancel_schedule_confirmation(
                describe(item.scheduled_for, item.display_timezone), version.title
            ),
            keyboard=render.cancel_schedule_keyboard(draft, version.version_no),
        )

    def _do_cancel_schedule(self, callback: IncomingCallback, parsed: Callback) -> None:
        """Withdraw a schedule. The approval is untouched — this is 'not then', not 'no'."""
        found = self._find_approved(parsed.draft_prefix, parsed.version_no)
        if found is None:
            self.api.answer_callback(callback.callback_id, "Чернетка змінилася.")
            return
        draft, version = found
        repo = PublicationQueueRepository(self.connection)
        item = repo.active_for_version(version.id, self.channel or "")
        if item is None:
            self.api.answer_callback(callback.callback_id, "Цей допис не заплановано.")
            return
        try:
            queue_service.cancel(self.connection, item.id, actor=BOT_ACTOR)
        except (queue_service.QueueError, AiNewsError) as exc:
            self.api.answer_callback(callback.callback_id, "Не вдалося зняти.")
            self.api.send_message(callback.chat_id, f"❌ {exc}")
            return
        self.api.answer_callback(callback.callback_id, "Знято з розкладу")
        self.api.edit_message(
            callback.chat_id,
            callback.message_id,
            render.approved_card(draft, version),
            keyboard=render.schedule_keyboard(draft, version.version_no),
        )

    # -- display -------------------------------------------------------------

    def _show_next(self, chat_id: int, *, message_id: int | None = None) -> None:
        """Show the next draft awaiting review, or say the queue is empty.

        Reuses ``review_queue`` — the CLI's ordering, best editorial score first. There
        is deliberately not a second queue policy for the phone.
        """
        queue = review_queue(self.connection)
        remaining = [item for item in queue if item.draft.id not in self.session.skipped]
        if not remaining:
            # Everything left was skipped in this run: start the round again rather
            # than claiming the queue is empty when it is not.
            self.session.reset_navigation()
            remaining = queue

        if not remaining:
            text = render.queue_empty(status_counts(self.connection))
            if message_id is not None:
                self.api.edit_message(chat_id, message_id, text)
            else:
                self.api.send_message(chat_id, text)
            return

        item = remaining[0]
        text = render.review_card(item, position=1, total=len(remaining))
        keyboard = render.review_keyboard(item)
        if message_id is not None and self.api.edit_message(
            chat_id, message_id, text, keyboard=keyboard
        ):
            return
        self.api.send_message(chat_id, text, keyboard=keyboard)


@contextmanager
def stop_on_signal() -> Iterator[threading.Event]:
    """SIGINT and SIGTERM end the poll loop between updates, never mid-update.

    A service manager restarts this process by sending SIGTERM, so without this the bot
    is killed at whatever line it happens to be on — quite possibly between recording a
    human's decision and telling them it worked. The decision would stand and the
    confirmation would never arrive.

    Stopping between updates costs at most one long-poll interval and leaves Telegram's
    offset unconfirmed, which is the safe direction: an unconfirmed update is delivered
    again, and every handler re-reads the draft before acting on it.
    """
    stop = threading.Event()
    previous: dict[int, object] = {}

    def handle(signum: int, _frame: object) -> None:
        logger.info("review bot stopping", extra={"signal": signum})
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not the main thread: nothing to install, and the caller's own shutdown
        # handling still applies.
        with suppress(ValueError):  # pragma: no cover
            previous[sig] = signal.signal(sig, handle)
    try:
        yield stop
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]


def poll(
    bot: ReviewBot,
    *,
    offset: int | None = None,
    iterations: int | None = None,
    sleep: float = 1.0,
    stop: threading.Event | None = None,
) -> Iterator[int]:
    """Long-poll for updates and dispatch them. Yields each processed update id.

    ``offset`` is Telegram's own confirmation mechanism: passing one greater than the
    highest ``update_id`` seen tells the server those updates are handled and must not
    be delivered again.

    ``iterations`` bounds the loop so tests can run it; ``None`` means run until
    interrupted. ``stop`` ends the loop cleanly at the next boundary — see
    :func:`stop_on_signal`.
    """
    processed = 0
    while iterations is None or processed < iterations:
        if stop is not None and stop.is_set():
            return
        try:
            updates = bot.api.get_updates(offset)
        except AiNewsError as exc:
            logger.warning("getUpdates failed, retrying", extra={"error": str(exc)})
            time.sleep(sleep)
            processed += 1
            continue

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = update_id + 1
            bot.handle(update)
            if isinstance(update_id, int):
                yield update_id
            # Between updates, not inside one: a decision already committed must not be
            # interrupted before its confirmation is sent.
            if stop is not None and stop.is_set():
                return

        processed += 1


def discard_backlog(api: BotApi) -> int | None:
    """Confirm everything Telegram is holding, without acting on any of it.

    A bot that has been offline may have a queue of updates waiting, including button
    taps from a card whose draft has since changed. Replaying those on startup would
    apply intentions the human formed against a state that no longer exists, so the
    backlog is confirmed and dropped.

    Nothing is lost that matters: a decision the owner still wants is one tap away, and
    the alternative — a stale Approve firing at boot — is exactly what must not happen.

    Returns the offset to start from, or None when there is no backlog.
    """
    updates = api.get_updates(offset=-1, timeout=0, limit=1)
    if not updates:
        return None
    last = updates[-1].get("update_id")
    if not isinstance(last, int):  # pragma: no cover - defensive
        return None
    api.get_updates(offset=last + 1, timeout=0, limit=1)
    logger.info("discarded telegram backlog", extra={"through_update_id": last})
    return last + 1


def pending_summary(connection: sqlite3.Connection) -> dict[str, int]:
    """Counts for the CLI to print when the bot starts."""
    counts = status_counts(connection)
    return {
        "pending": counts.get(DraftStatus.PENDING_REVIEW.value, 0),
        "approved": counts.get(DraftStatus.APPROVED.value, 0),
        "published": counts.get(DraftStatus.PUBLISHED.value, 0),
    }
