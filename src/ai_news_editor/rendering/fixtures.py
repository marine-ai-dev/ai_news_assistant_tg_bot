"""Offline, synthetic sample content — one per category — Step 5 section 46.

No network, no Gemini, no database: every field below is invented for the shape of
the example, not copied from any real published story. Used by
``ai-news editorial preview --all-categories`` to show what each category's renderer
produces without needing a live candidate. Every URL is ``*.invalid``.
"""

from __future__ import annotations

from ai_news_editor.domain.enums import (
    EditorialCategory,
    EditorialEvidence,
    FreeDealKind,
    PromptOrigin,
)
from ai_news_editor.editorial.safety import ResearchClaimFraming
from ai_news_editor.rendering.content import BodyBlock, DigestItem, EditorialContent

SAMPLE_CONTENT: dict[EditorialCategory, EditorialContent] = {
    EditorialCategory.NEWS: EditorialContent(
        category=EditorialCategory.NEWS,
        evidence=EditorialEvidence.PRIMARY_SOURCE,
        headline="ExampleCorp додала новий AI-режим у Таблиці",
        body=(
            BodyBlock(
                purpose="what_happened",
                text="ExampleCorp представила режим автозаповнення на основі AI.",
            ),
            BodyBlock(
                purpose="why_it_matters",
                text="Це прибирає рутинне копіювання формул вручну.",
            ),
            BodyBlock(
                purpose="availability",
                text="Функція доступна для всіх користувачів з сьогодні.",
            ),
        ),
        detail_bullets=("Працює у веб-версії", "Підтримує 12 мов"),
        source_label="ExampleCorp",
        source_url="https://blog.example.invalid/sheets-ai",
    ),
    EditorialCategory.AI_TOOL: EditorialContent(
        category=EditorialCategory.AI_TOOL,
        evidence=EditorialEvidence.OFFICIAL_PRODUCT_PAGE,
        headline="Notely — інструмент для нотаток на основі AI",
        body=(
            BodyBlock(
                purpose="what_it_is",
                text="Notely перетворює голосові нотатки на структурований текст.",
            ),
            BodyBlock(
                purpose="who_its_for",
                text="Підійде для команд, які записують багато зустрічей.",
            ),
            BodyBlock(
                purpose="what_you_can_do",
                text="Можна експортувати підсумок одразу у Slack.",
            ),
        ),
        source_label="Notely",
        source_url="https://notely.example.invalid",
    ),
    EditorialCategory.FREE_DEAL: EditorialContent(
        category=EditorialCategory.FREE_DEAL,
        evidence=EditorialEvidence.OFFICIAL_PRODUCT_PAGE,
        headline="ExampleCorp Studio: безкоштовний тариф без ліміту часу",
        body=(
            BodyBlock(
                purpose="what_happened",
                text="ExampleCorp відкрила безкоштовний тариф Studio.",
            ),
            BodyBlock(
                purpose="conditions",
                text="До 100 генерацій на місяць, без картки.",
            ),
        ),
        free_deal_kind=FreeDealKind.FREE_TIER,
        source_label="ExampleCorp",
        source_url="https://example.invalid/studio-pricing",
    ),
    EditorialCategory.AI_LIFEHACK: EditorialContent(
        category=EditorialCategory.AI_LIFEHACK,
        evidence=EditorialEvidence.USER_REPORTED,
        headline="Лайфхак: чернетки листів за 2 хвилини",
        body=(
            BodyBlock(
                purpose="anecdote",
                text="Користувач Hacker News поділився власним підходом до чернеток.",
            ),
            BodyBlock(
                purpose="workflow",
                text="Диктує основну думку, а AI оформлює структуру листа.",
            ),
            BodyBlock(
                purpose="anecdote_result",
                text="За його словами, це заощадило близько години на тиждень.",
            ),
        ),
        source_label="Hacker News",
        source_url="https://news.ycombinator.invalid/item?id=1",
    ),
    EditorialCategory.PROMPT_WORKFLOW: EditorialContent(
        category=EditorialCategory.PROMPT_WORKFLOW,
        evidence=EditorialEvidence.COMMUNITY_DISCUSSION,
        headline="Workflow: стислий підсумок довгих документів",
        body=(BodyBlock(purpose="task", text="Отримати короткий підсумок довгого звіту."),),
        prompt_origin=PromptOrigin.SOURCE_ADAPTED,
        prompt_text="Ти — редактор. Стисни текст нижче до 5 речень, зберігаючи ключові цифри.",
        source_label="Reddit",
        source_url="https://reddit.example.invalid/r/example/comments/1",
    ),
    EditorialCategory.EXPLAINER: EditorialContent(
        category=EditorialCategory.EXPLAINER,
        evidence=EditorialEvidence.PRIMARY_SOURCE,
        headline="Простими словами: що таке RAG",
        body=(
            BodyBlock(
                purpose="what_it_is",
                text="RAG — це коли модель шукає факти в базі перед відповіддю.",
            ),
            BodyBlock(
                purpose="why_useful",
                text="Це зменшує кількість вигаданих відповідей.",
            ),
            BodyBlock(
                purpose="where_used",
                text="Використовується в чат-ботах підтримки та пошуку.",
            ),
        ),
        source_label="ExampleCorp",
        source_url="https://blog.example.invalid/what-is-rag",
    ),
    EditorialCategory.RESEARCH: EditorialContent(
        category=EditorialCategory.RESEARCH,
        evidence=EditorialEvidence.RESEARCH_PAPER,
        headline="Нове дослідження: довший контекст без втрати точності",
        body=(
            BodyBlock(
                purpose="what_was_tested",
                text="Дослідники перевірили модель на текстах довжиною 200 сторінок.",
            ),
            BodyBlock(
                purpose="what_was_found",
                text="Точність відповідей залишилась стабільною на всій довжині.",
            ),
            BodyBlock(
                purpose="limitation",
                text="Тест охоплював лише англомовні тексти.",
            ),
        ),
        research_framing=ResearchClaimFraming.PAPER_RESULT,
        source_label="ExampleCorp Research",
        source_url="https://research.example.invalid/papers/long-context",
    ),
    EditorialCategory.WEEKLY_DIGEST: EditorialContent(
        category=EditorialCategory.WEEKLY_DIGEST,
        evidence=EditorialEvidence.REPUTABLE_SECONDARY,
        headline="AI-тиждень: 3 речі, які варто знати",
        body=(BodyBlock(purpose="what_happened", text="Огляд тижня."),),
        digest_items=(
            DigestItem(
                headline="Новий режим у Таблицях", summary="ExampleCorp додала автозаповнення."
            ),
            DigestItem(
                headline="Notely оновили експорт",
                summary="Тепер підсумки можна надсилати у Slack.",
                source_label="Notely",
                source_url="https://notely.example.invalid",
            ),
            DigestItem(headline="Безкоштовний тариф Studio", summary="До 100 генерацій на місяць."),
        ),
        source_label="Огляд тижня",
        source_url="https://blog.example.invalid/weekly",
    ),
}


__all__ = ["SAMPLE_CONTENT"]
