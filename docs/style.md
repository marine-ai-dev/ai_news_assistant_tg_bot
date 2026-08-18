# 🎙️ Editorial style

Step 5 (AI News Agent v2) turns the Step 3 taxonomy and Step 4 media pipeline into
rendered Telegram posts. This document defines the channel's own visual and editorial
identity — the rules `rendering/` actually enforces, not aspirational prose.

## Internal direction

Not published verbatim anywhere; the sentence every rendering decision below serves:

> AI без шуму: що сталося, навіщо це знати і як це можна використати.

Seven qualities, each with a concrete rule in code, not just a value statement:

- **Source-first** — every post ends with a clickable, canonical source link
  (`rendering/render.py`'s `_source_line`); nothing is published without one.
- **Practical** — AI_TOOL/FREE_DEAL/AI_LIFEHACK/PROMPT_WORKFLOW templates always
  answer "what can the reader actually do with this."
- **Concise** — `rendering/style.py`'s `TARGET_BODY_CHARS_MIN/MAX` (500-900) flag an
  ordinary post that has drifted into a wall of text, without ever cutting a fact to
  hit the number (`RESEARCH`/`EXPLAINER`/`WEEKLY_DIGEST` are explicitly wider).
- **Clear** — plain Ukrainian, one idea per paragraph, one emoji per paragraph
  *purpose* (never per sentence).
- **Low-hype** — `HYPE_PHRASES` is a real, checked list ("революційний", "змінить
  світ", "неймовірний прорив", "game-changing", ...); the renderer flags any post that
  contains one.
- **Human** — AI_LIFEHACK is written as somebody's report, never as an established
  fact about what AI does; see below.
- **Visually scannable** — bold headline, blank-line-separated blocks, an optional
  `🔆 Детальніше` bullet list, never a mechanical wall of bullets.

## Not a clone

This channel may be paced like other successful AI-news Telegram channels — short,
frequent, practical posts — but nothing here is copied from one. No competitor's
wording, sentence structure, or branded rubric name appears anywhere in this codebase,
and nothing in `editorial/policy.py`'s generation prompts or `rendering/`'s templates
references a competitor by name. If you are extending either, keep it that way: the
place a competitor's channel may legitimately be mentioned is developer documentation
discussing pacing as *inspiration*, never as an instruction Gemini receives ("write
like X") and never as text a reader could recognize as reproduced.

## Global post grammar

Not every category shares one template, but every category shares this shape:

```
CATEGORY EMOJI + HEADLINE  (bold, own line)

BODY BLOCKS  (one emoji per paragraph purpose, blank line between)

[optional category-specific block: prompt, deal kind, research framing]

[optional 🔆 Детальніше bullets]

🔗 Джерело: link
```

## Category emoji (`rendering/style.py::CATEGORY_EMOJI`)

One canonical emoji per category, centralized in one table, never scattered across
templates:

| Category | Emoji |
|---|---|
| NEWS | 🚀 |
| AI_TOOL | 🛠 |
| FREE_DEAL | 🎁 |
| AI_LIFEHACK | 💡 |
| PROMPT_WORKFLOW | 🧪 |
| EXPLAINER | 🧠 |
| RESEARCH | 🔬 |
| WEEKLY_DIGEST | 📚 |

## Source line

One consistent format, always a Markdown hyperlink, never a bare URL:

```
🔗 [Джерело: Google](https://blog.google/...)
```

## The four category-specific guarantees

Requested in a prompt is not enough — each of these is checked in
`rendering/render.py`, using the corresponding `editorial/safety.py` validator, before
a post of that category can render at all:

- **AI_LIFEHACK** always reads as somebody's report ("👤 Один користувач..."), always
  carries the caveat line, and is rejected outright if its evidence is not
  `USER_REPORTED`/`COMMUNITY_DISCUSSION` — a Tier A source cannot supply a lifehack.
- **PROMPT_WORKFLOW** picks its label ("Оригінальний промпт" / "Адаптована версія
  промпту" / "Ідея workflow") from `PromptOrigin`, and only `SOURCE_VERBATIM` text may
  appear inside a quote block — an adapted or derived prompt is never quoted.
- **FREE_DEAL** labels the offer from `FreeDealKind` (`FREE` / `FREE_TIER` /
  `FREE_TRIAL` / `OPEN_SOURCE` / `PROMOTION` / `DISCOUNT`) — a trial can never read as
  "free forever," a tier never reads as "everything is free," open-source is never
  conflated with a free hosted service.
- **RESEARCH** labels its claim from `ResearchClaimFraming` ("За даними дослідження" /
  "За заявою компанії" / "Незалежно підтверджено") and is rejected if it claims
  independent verification the material does not actually state happened.

## Example: NEWS (rendered)

```
*🚀 ExampleCorp додала новий AI-режим у Таблиці*

✨ ExampleCorp представила режим автозаповнення на основі AI.

💡 Це прибирає рутинне копіювання формул вручну.

🌍 Функція доступна для всіх користувачів з сьогодні.

🔆 Детальніше:

• Працює у веб-версії
• Підтримує 12 мов
• Безкоштовно для всіх

🔗 [Джерело: ExampleCorp](https://blog.example.invalid/sheets-ai)
```

## Example: AI_LIFEHACK (rendered)

```
*💡 Лайфхак: чернетки листів за 2 хвилини*

👤 Користувач Hacker News поділився власним підходом до чернеток.

🛠 Диктує основну думку, а AI оформлює структуру листа.

✨ За його словами, це заощадило близько години на тиждень.

⚠️ Це досвід конкретного користувача, а не гарантований результат.

🔗 [Джерело: Hacker News](https://news.ycombinator.invalid/item?id=1)
```

Every category's rendered example is available live, offline, with
`ai-news editorial preview --all-categories` — see `rendering/fixtures.py`.

## Media + text composition

Media is optional and never blocks a valid text post (see docs/media.md's own
principle, unchanged by Step 5). `rendering/plan.py` decides one of five shapes:
`TEXT_ONLY`, `PHOTO_WITH_FULL_CAPTION`, `VIDEO_WITH_FULL_CAPTION`,
`PHOTO_SHORT_CAPTION_THEN_TEXT`, `VIDEO_SHORT_CAPTION_THEN_TEXT` — the last two used
only when the full rendered post does not fit Telegram's caption limit, and the short
caption is always derived deterministically from the same record as the full post
(`rendering/render.py::render_short_summary`), never an independently generated
second summary that could drift from it.

## Gemini's role

Gemini never decides bold, links, emoji placement, or `parse_mode`. It answers the
editorial questions `rendering.content.EditorialContent` asks — headline, body block
purposes and text, category-specific fields — as plain strings and enum values, via
`automation/generation_v2.py`. `rendering/render.py` is the only code that turns that
into Telegram markup, escaping every string on the way in.
