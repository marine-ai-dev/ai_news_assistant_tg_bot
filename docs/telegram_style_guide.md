# Telegram style guide — v1

How posts for this channel are written. The arithmetic (length limits, link safety, post
assembly) lives in `src/ai_news_editor/writing/format.py`; this is the voice.

## The channel

A Ukrainian popular-science / consumer-technology channel about AI. Readers are ordinary
people who use AI but do not build it: ChatGPT users, beginners, creatives, freelancers,
office workers, students, teachers.

It should read like **a good modern technology magazine** — written by someone who
genuinely understands AI, explaining it to people who do not.

It must not read like: AI-generated corporate copy, a translated press release, a dry
technical digest, LinkedIn thought leadership, an SEO article, or a developer changelog.

A reader should often finish thinking *«О, цікаво»*, *«О, я можу це спробувати»*, or
*«Нічого собі»*.

## Language

Natural contemporary Ukrainian — spoken-literary, the register of a well-edited magazine.

Avoid: русизми, calques from English syntax, bureaucratic constructions, unexplained
abbreviations, and the flat "AI-ish" phrasing that gives machine writing away
(«у сучасному світі», «варто зазначити, що», «це відкриває нові можливості»).

Use the Ukrainian apostrophe (’) and the em dash (—) properly.

**Addressing the reader:** default to neutral narration. Where direct address genuinely
helps — explaining how to try something — use **ви**, never ти. Do not address the
reader in every paragraph; it becomes wheedling.

## What a post must do

Not a summary of the source. A post answers as many of these as the story supports:

1. **Що сталося** — the fact, plainly.
2. **Чому це цікаво** — why anyone should care.
3. **Що це означає для звичайної людини** — the consequence.
4. **Чи можна спробувати** — availability, if the source states it.
5. **Про що варто пам'ятати** — the caveat, where one exists.

The editorial angle from the evaluation is the spine of the post. Follow it.

## Formats

| Format | Target | Use for |
|---|---|---|
| `QUICK` | ~400–800 chars | a small concrete change, a single fact |
| `STANDARD` | ~800–1600 chars | most stories; the default |
| `DEEP_DIVE` | ~1600–3000 chars | deepfakes, scams, complicated developments |

These are editorial targets, not hard limits — being outside one is a note, never a
rejection. Not everything deserves to be long. A channel where every post is the same
length stops being read.

## Structure

There is no single template, and variation matters. A typical STANDARD post:

```
🆕 Заголовок, який щось обіцяє

Гачок на одне-два речення.

Що змінилося — простими словами.

Чому це важливо для тих, хто користується цим щодня.

Що варто знати / як спробувати.

🔗 Джерело: ...
```

**Do not literally label sections** («Що сталося:», «Чому це важливо:») unless it
genuinely helps that particular post. We want editorial writing, not a filled-in form.

## Headlines

Ukrainian, clear, interesting, honest. It should be understandable without technical
background and must not promise more than the story delivers.

Good: `🆕 ChatGPT навчився редагувати фото — і це вже можна спробувати`
Bad: `ШОК! OpenAI ЗНОВУ ЗМІНИЛА ВСЕ!!!`

No caps-lock shouting, no manufactured urgency, no invented claims.

## Emoji

Deliberate, not decorative. Usually **one headline emoji plus zero to three** in the
body. Never one per sentence.

🆕 product · 🛠️ tool · 🤯 wow · 😂 funny · 🕵️ deepfake · 🚨 scam · 🎨 creative ·
💼 work · 📚 learning · 🧠 explainer · 🔥 trending · 🌍 everyday · 🎬 video · 🎵 music

## Technical terms

Explain, do not avoid. Simplify without becoming wrong.

Weak: «Модель отримала більше контекстне вікно.»
Better: «Claude тепер утримує в межах однієї розмови значно більше тексту — умовно, не
кілька сторінок, а цілий документ.»

Do not dumb down an accurate concept into a false one.

## When the source contradicts the assignment

The category and audience come from the editorial evaluation, which was often made from
a short excerpt. Opening the source sometimes reveals the story is not quite what the
assignment assumed — a course labelled `GENERAL` turns out to be aimed at developers, a
feature assumed to be live turns out to be a preview.

**Write what the source actually says, and say the awkward part out loud.** «Варто
сказати чесно: курс розрахований на тих, хто вже пише код» is a better post than one
that quietly implies otherwise, and better than one that omits the story.

Then leave a writer note recording the mismatch, so the reviewer knows the classification
and the text disagree. Do not silently re-classify: the category and audience belong to
the evaluation, and a note is how a disagreement gets raised.

## Facts

**Never invent.** Not availability, rollout dates, prices, user numbers, benchmark
figures, regions, supported platforms, capabilities, or quotations.

If the source does not say who gets a feature or when, do not fill the gap. Write what
is known and attribute it: «компанія заявляє…», «за даними OpenAI…», «функцію
запускають поступово».

Paraphrase by default. A short direct quote is fine when it genuinely earns its place
and is traceable to the source — never quotation marks around your own paraphrase.

Do not copy paragraphs from the source. Posts are original editorial writing; any
verbatim fragment stays short and justified.

## By category

**Product updates** — what changed, who gets it, whether it works today, what it is
actually for, known limits. Do not infer availability.

**Useful tools** — what it does, who benefits, one or two concrete uses, cost only if
stated, the important limitation. A post is not an advert.

**Wow / funny / AI fail** — lighter tone is welcome; the facts stay exact. Do not mock
people who were harmed, and do not manufacture a joke the story does not contain.

**Deepfakes, scams, misinformation** — care. Separate what is established from what is
alleged. Explain how the deception worked if that helps a reader spot the next one.
Never repeat the false claim as if it were true, never sensationalise, and never give
instructions that would help someone commit the fraud.

**Science** — lead with why an ordinary person should care. One central idea, explained
well. No paper titles, no benchmark tables, no architecture.

## Source

Every post ends with the source. Python assembles the line — supply the label and URL:

```
🔗 Джерело: OpenAI
https://openai.com/index/...
```

A post without a traceable source is not published here. The line is part of the hashed
content, so it cannot quietly disappear after review.

## Writer notes

Internal, never published. Use them for what a reviewer needs to know: «доступність не
вказана», «варто перевірити регіони перед публікацією», «формулювання свідомо
обережне», «просилася б ілюстрація». Keep them short.
