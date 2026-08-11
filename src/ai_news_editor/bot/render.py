"""What the owner sees in Telegram.

Pure formatting: every function here takes domain objects and returns strings and
keyboards. No database, no decisions, nothing that can change state. That separation is
what lets the rendering be tested exhaustively without a Telegram or a database, and it
is why the dispatcher can stay small enough to read in one sitting.

The review card has two halves, visibly separated: the post exactly as it would be sent,
and the internal information that never ships — writer notes, score, version, origin. A
reviewer has to be able to tell at a glance which is which, because the entire job is
judging the first part.
"""

from __future__ import annotations

from typing import Any

from ai_news_editor.bot.callbacks import Action, encode
from ai_news_editor.domain.enums import AudienceTier, ContentType, DraftStatus
from ai_news_editor.review.service import ReviewItem

#: One label per content type. The emoji does the work at a glance; the word removes any
#: doubt about which is which.
TYPE_LABELS: dict[ContentType, str] = {
    ContentType.NEWS: "📰 NEWS",
    ContentType.PROMPT: "✨ PROMPT",
    ContentType.EXPLAINER: "🧠 EXPLAINER",
    ContentType.TESTED_USE_CASE: "🛠 TESTED_USE_CASE",
    ContentType.RESOURCE: "📚 RESOURCE",
}

AUDIENCE_LABELS: dict[AudienceTier, str] = {
    AudienceTier.NEWCOMER: "🌱 NEWCOMER",
    AudienceTier.BEGINNER: "🙂 BEGINNER",
    AudienceTier.GENERAL: "👤 GENERAL",
    AudienceTier.TECH_CURIOUS: "🧩 TECH_CURIOUS",
}

STATUS_LABELS: dict[DraftStatus, str] = {
    DraftStatus.PENDING_REVIEW: "на розгляді",
    DraftStatus.APPROVED: "схвалено",
    DraftStatus.REJECTED: "відхилено",
    DraftStatus.NEEDS_REWRITE: "переписати",
    DraftStatus.PUBLISHING: "публікується",
    DraftStatus.PUBLISHED: "опубліковано",
    DraftStatus.PUBLISH_FAILED: "помилка публікації",
    DraftStatus.DRAFTED: "чернетка",
}

_SEPARATOR = "─" * 24


def welcome(counts: dict[str, int]) -> str:
    """The /start screen. Concise on purpose — it is a control panel, not a greeting."""
    lines = [
        "*AI News Editor*",
        "",
        f"На розгляді: *{counts.get(DraftStatus.PENDING_REVIEW.value, 0)}*",
        f"Схвалено: {counts.get(DraftStatus.APPROVED.value, 0)}",
        f"Опубліковано: {counts.get(DraftStatus.PUBLISHED.value, 0)}",
        "",
        "/review — читати чернетки",
        "/status — стан черги",
    ]
    return "\n".join(lines)


def help_text() -> str:
    return "\n".join(
        [
            "*Команди*",
            "",
            "/review — наступна чернетка на розгляді",
            "/pending — скільки чекає",
            "/status — стан усіх чернеток",
            "/whoami — ваш Telegram ID",
            "/cancel — вийти з режиму редагування",
            "",
            "Схвалення тут не публікує допис. Публікація — окрема дія в терміналі.",
        ]
    )


def status_report(counts: dict[str, int], pending_by_type: dict[ContentType, int]) -> str:
    """Counts by status, and what the pending queue is made of."""
    lines = ["*Стан чернеток*", ""]
    for status in (
        DraftStatus.PENDING_REVIEW,
        DraftStatus.APPROVED,
        DraftStatus.NEEDS_REWRITE,
        DraftStatus.REJECTED,
        DraftStatus.PUBLISHED,
    ):
        lines.append(f"{STATUS_LABELS[status]}: {counts.get(status.value, 0)}")

    if pending_by_type:
        lines += ["", "*На розгляді за типом*"]
        for content_type, count in pending_by_type.items():
            lines.append(f"{TYPE_LABELS[content_type]} — {count}")
    return "\n".join(lines)


def queue_empty(counts: dict[str, int]) -> str:
    return "\n".join(
        [
            "Усе переглянуто 🎉",
            "",
            f"Схвалено: {counts.get(DraftStatus.APPROVED.value, 0)}",
            f"Переписати: {counts.get(DraftStatus.NEEDS_REWRITE.value, 0)}",
            f"Відхилено: {counts.get(DraftStatus.REJECTED.value, 0)}",
            f"Опубліковано: {counts.get(DraftStatus.PUBLISHED.value, 0)}",
        ]
    )


def review_card(item: ReviewItem, *, position: int, total: int) -> str:
    """One draft, ready to judge.

    Metadata first so the reader knows what kind of thing they are about to read and
    who it is for; then the post; then the internal notes, clearly below a line.
    """
    draft = item.draft
    version = item.version

    header = [
        f"*{position} / {total}*  ·  {TYPE_LABELS[draft.content_type]}",
        f"{AUDIENCE_LABELS[version.audience]}  ·  `{version.category.value}`",
    ]

    if draft.content_type is ContentType.NEWS and item.article is not None:
        header.append(f"Джерело: {item.article.source_id}")
        header.append(item.article.canonical_url)
    elif item.content_item is not None:
        label = "Тема" if draft.content_type is ContentType.PROMPT else "Поняття"
        header.append(f"{label}: {item.subject}")
        evidence = item.content_item.evidence
        if evidence is not None:
            # A prompt post is a report of someone else's test. The reviewer's job is
            # to judge that source, so it goes above the fold, not in a footnote.
            header.append(f"Перевіряв: {evidence.tested_by}")
            header.append(f"Інструмент: {evidence.tool_used}"
                          + (f" ({evidence.model_version})" if evidence.model_version else ""))
            header.append(f"Що тестували: {evidence.what_was_tested}")
            header.append(f"Результат: {evidence.observed_result}")
            if evidence.requires:
                header.append(f"Потрібно: {', '.join(evidence.requires)}")
            if evidence.limitations:
                header.append(f"Обмеження: {'; '.join(evidence.limitations)}")
            header.append(f"Джерело: {evidence.source_url}")
        else:
            header.append("Матеріал каналу — зовнішнього джерела немає")

    if item.content_item is not None and item.content_item.series_label:
        header.append(f"Серія: {item.content_item.series_label}")

    body = [*header, "", _SEPARATOR, "", item.rendered_post, "", _SEPARATOR, ""]

    # Everything below is part of what is being approved. A human tapping Approve on a
    # post whose prompt lives in a comment is approving the comment too, so it is shown
    # in full rather than summarised.
    version = item.version
    if version.comment_text:
        body += [
            f"*КОМЕНТАР ДО ПУБЛІКАЦІЇ* ({version.prompt_placement.value})",
            "",
            version.comment_text,
            "",
            _SEPARATOR,
            "",
        ]
    if version.media:
        body.append("*МЕДІА*")
        for asset in version.media:
            line = f"• {asset.role.value} · {asset.origin.value} — {asset.description}"
            if asset.tool_used:
                line += f" (зроблено: {asset.tool_used})"
            body.append(line)
        body += ["", _SEPARATOR, ""]
    if version.resource is not None:
        spec = version.resource
        body += [
            "*РЕСУРС*",
            f"• {spec.resource_type.value} — {spec.title}",
            f"• {spec.description}",
            "", _SEPARATOR, "",
        ]

    internal = [f"_версія {version.version_no}_"]
    if item.score is not None:
        internal.append(f"_оцінка {item.score:.0f}_")
    if version.post_format is not None:
        internal.append(f"_{len(item.rendered_post)} символів · {version.post_format.value}_")
    body.append("  ".join(internal))

    if version.writer_notes:
        body.append("")
        body.append("_Нотатки (не публікуються):_")
        body.extend(f"_• {note}_" for note in version.writer_notes)

    if item.content_item is not None and item.content_item.evidence_status is not None:
        body.append("")
        body.append(f"_доказовість: {item.content_item.evidence_status.value}_")

    if item.content_item is not None and item.content_item.references:
        body.append("")
        body.append("_Перевірено за:_")
        body.extend(f"_• {r.label} — {r.url}_" for r in item.content_item.references)

    return "\n".join(body)


def review_keyboard(item: ReviewItem) -> dict[str, Any]:
    """The main action row plus navigation."""
    draft_id = item.draft.id
    version_no = item.version.version_no
    return {
        "inline_keyboard": [
            [
                _button("✅ Схвалити", Action.APPROVE, draft_id, version_no),
                _button("✏️ Редагувати", Action.EDIT, draft_id, version_no),
            ],
            [
                _button("📝 Переписати", Action.REWRITE, draft_id, version_no),
                _button("❌ Відхилити", Action.REJECT, draft_id, version_no),
            ],
            [
                _button("⏭ Пропустити", Action.SKIP, draft_id, version_no),
                _button("📜 Історія", Action.HISTORY, draft_id, version_no),
            ],
        ]
    }


def confirm_keyboard(item: ReviewItem, action: Action, label: str) -> dict[str, Any]:
    """A second, deliberate tap. Never one-tap for anything that changes a status."""
    return {
        "inline_keyboard": [
            [_button(label, action, item.draft.id, item.version.version_no)],
            [_button("↩ Скасувати", Action.CANCEL, item.draft.id, item.version.version_no)],
        ]
    }


def cancel_keyboard(item: ReviewItem) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [_button("↩ Скасувати", Action.CANCEL, item.draft.id, item.version.version_no)]
        ]
    }


def next_keyboard(item: ReviewItem | None = None) -> dict[str, Any]:
    """Offered after a decision, so the queue keeps moving without typing /review."""
    draft_id = item.draft.id if item else None
    version_no = item.version.version_no if item else 1
    if draft_id is None:
        return {"inline_keyboard": []}
    return {"inline_keyboard": [[_button("➡ Далі", Action.NEXT, draft_id, version_no)]]}


def approve_confirmation(item: ReviewItem) -> str:
    return "\n".join(
        [
            f"Схвалити *версію {item.version.version_no}* цієї чернетки?",
            "",
            "Схвалення стосується саме цього тексту. Якщо його потім змінити, "
            "схвалення анулюється.",
            "",
            "_Схвалення не публікує допис._",
        ]
    )


def reject_confirmation(item: ReviewItem) -> str:
    return "\n".join(
        [
            "Відхилити цю чернетку?",
            "",
            "Нічого не видаляється — чернетка лишається в історії разом із рішенням.",
        ]
    )


def rewrite_confirmation(item: ReviewItem) -> str:
    return "\n".join(
        [
            "Повернути на переписування?",
            "",
            "Після підтвердження можна одним повідомленням написати, що саме змінити.",
        ]
    )


def edit_instructions(item: ReviewItem) -> str:
    return "\n".join(
        [
            "*Редагування*",
            "",
            "Надішліть новий текст одним повідомленням: перший рядок — заголовок, "
            "далі — сам допис.",
            "",
            "Стане версією "
            f"*{item.version.version_no + 1}*. Попередня версія лишається незмінною, "
            "а чернетка знову піде на розгляд.",
            "",
            "/cancel — вийти без змін.",
        ]
    )


def history(entries: list[str]) -> str:
    if not entries:
        return "Історії поки немає."
    return "\n".join(["*Історія*", "", *entries])


def denied() -> str:
    """What an unauthorized user gets. Deliberately says nothing at all."""
    return "Цей бот приватний."


def _button(text: str, action: Action, draft_id: Any, version_no: int) -> dict[str, str]:
    return {"text": text, "callback_data": encode(action, draft_id, version_no)}
