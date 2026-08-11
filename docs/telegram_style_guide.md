# Telegram style guide — v2

How posts for this channel are written. The arithmetic (length limits, link safety, post
assembly) lives in `src/ai_news_editor/writing/format.py`; this is the voice.

## The channel

A Ukrainian popular-science / consumer-technology channel about AI. Readers are ordinary
people who use AI but do not build it: ChatGPT users, beginners, creatives, freelancers,
office workers, students, teachers.

**And, since v2, people who barely use AI at all.** Someone who has opened ChatGPT once
or twice, does not know what an API is, has never heard of Notion or Slack, and is not
sure whether ChatGPT, Claude and Gemini are different things. They are curious. They
have been told AI is important and complicated, and they believe both.

That reader is not less intelligent than the others. They have simply spent their
attention somewhere else.

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

---

# v2: audiences and formats

Everything above still applies. This section adds who a post is *for* and what kind of
thing it is — two questions that were previously conflated.

## Audience levels

An ordered scale. Each level says what the post may assume the reader already knows.

| Level | Assumes | Example reader |
|---|---|---|
| `NEWCOMER` | Nothing. May have opened ChatGPT once. | Someone's mother, a colleague in accounting |
| `BEGINNER` | Uses an AI chat sometimes. Knows what a prompt is. | A student who writes essays with ChatGPT |
| `GENERAL` | Comfortable with consumer AI tools. | Someone who has tried three chatbots |
| `TECH_CURIOUS` | Follows the field. Knows what a model release is. | A designer who reads AI newsletters |

**Not every post should be `NEWCOMER`.** A channel that explains what ChatGPT is every
week is a channel nobody stays subscribed to. Depth is part of the product. The point of
the scale is that the *mix* is deliberate rather than accidental.

The audience is a judgement about **story fit**, not about how hard the prose was to
write. A story about developer infrastructure is not `NEWCOMER` because it *could* be
simplified — it is `TECH_CURIOUS` because that is who it matters to. A new voice mode in
a phone app is `NEWCOMER` because that reader can actually use it tomorrow.

## Writing for NEWCOMER

Assume no knowledge of: Slack, Notion, API, prompt engineering, tokens, context windows,
AI agents, local models, parameters, inference, benchmarks, tech stacks, fine-tuning,
embeddings, GitHub, Python, model weights.

These words are **not banned**. If one is genuinely needed, explain it the first time,
in the same sentence, in ordinary language.

> ❌ «Notion додав тригер для AI-агента.»
>
> ✅ «Notion — сервіс для нотаток і робочих документів — додав функцію, яка вміє сама
> почати виконувати завдання після зустрічі.»

> ❌ «У моделі тепер контекстне вікно на 130 тисяч токенів.»
>
> ✅ «ШІ тепер утримує в пам'яті значно більше з однієї розмови — умовно, не кілька
> сторінок, а цілий документ.»

### The test

> *Could my mother, or a friend who has used ChatGPT twice, follow this?*

If no, the wording is too technical. Not the topic — the wording. Fix the sentence, not
the subject.

`ai-news content validate` flags terms that look unexplained. It is a reading aid with a
crude heuristic: it will miss things and it will occasionally flag a term you explained
in a way it did not recognise. Treat it as a second pair of eyes, not a gate.

### Never talk down

This is the part that matters most, and the easiest to get wrong.

> ❌ «навіть новачок зрозуміє»
> ❌ «це дуже просто, не хвилюйтеся»
> ❌ «якщо ви нічого не розумієте в ШІ»
> ❌ «спойлер: це не страшно 🙂»

Explain clearly and move on. Reassurance about how easy something is implies the reader
was expected to struggle. Write as though explaining to a competent adult who happens
not to know this particular thing — because that is exactly who is reading.

## Content types

Three formats. A format is not a category: a category says what a piece of news is
about, a format says whether it is news at all.

### NEWS

Unchanged from v1. Something happened, somebody else reported it, we have a source link.

### PROMPT — «✨ Спробуйте цей промпт»

Evergreen, practical, immediately usable. Every prompt post must make three things
obvious:

1. **what the reader can do** with it,
2. **what to copy** — the prompt itself, in full, in the post,
3. **how to adapt it** — at least one concrete change to make it theirs.

A workable shape, not a mandatory template:

```
✨ [Useful headline]

Short explanation of what this helps with.

📋 Готовий промпт:
[the prompt, copyable]

💡 Як адаптувати під себе:
• change X
• add Y
```

Vary it. Four identical posts in a row read like a form.

**«Промпт» is itself jargon for a `NEWCOMER`.** The section label can use it — that is
the format's name and readers learn it — but the first line of the body should say what
it means, in passing, without ceremony:

> «Нижче — готовий промпт. Промпт — це просто текст, який ви пишете ШІ, звичайними
> словами.»

Found while writing the first batch: every prompt post headline said «Промпт:» and no
post explained the word. Obvious in hindsight, invisible while writing it.

**No magic prompts.** Never claim a prompt makes an AI "10x better" or unlocks a hidden
mode. A good prompt works for boring, explainable reasons: it gives context, states a
goal, sets useful constraints, and says what shape the answer should take. Say that
instead. Superstition about secret words is the genre this channel is against.

**Do not invent compatibility.** File upload, image input and web browsing are not
available in every tool or every plan. If a prompt is genuinely generic text, «підійде
для більшості текстових AI-чатів» is fine. If it needs a photo, say so.

Topics: `EVERYDAY_LIFE`, `WORK`, `LEARNING`, `CREATIVE`, `TRAVEL`, `SHOPPING`, `FOOD`,
`PERSONAL_ORGANIZATION`, `FUN`.

**Prompt posts are usually `STANDARD`, not `QUICK`.** The prompt itself is often 300–400
characters before a word of framing, so a `QUICK` prompt post almost always overshoots
its target. That is a length *note*, not an error — but choosing the honest format up
front is better than filing the warning every time.

### EXPLAINER — «🧠 ШІ без складних слів»

One concept per post. Not «що таке промпти, агенти, токени й ембединги» — that is four
posts, and nobody finishes the one.

Shape:

1. the concept, in one plain sentence;
2. an example **from the reader's life**, not from another piece of technology;
3. why it is worth knowing;
4. optionally, one thing to try.

> «Промпт — це просто те, що ви пишете ШІ. Не команда й не код — звичайний текст своїми
> словами.»

Then the example. Then one practical takeaway.

Do not open with a dictionary definition («Промпт (від англ. prompt — підказка) є…»).
Do not over-explain: when the reader has it, stop.

**Editorial-original is not permission to invent.** An explainer has no source article,
which makes it *more* important to check claims about privacy, pricing, plan limits,
data retention, what a tool can actually do, and anything legal or safety-related.
Check the authoritative page and record it as a reference. An evergreen prompt about
meal planning needs no research; «що ChatGPT робить з вашими файлами» does.

## Editorial mix

Guidance for planning, not a scheduler and not a quota. Nothing in the code enforces it.

Roughly, by audience: **40–50% `NEWCOMER` + `BEGINNER`**, the rest `GENERAL` and
`TECH_CURIOUS`.

Roughly, by content:

| Share | What |
|---|---|
| 30% | Interesting AI news |
| 20% | Understandable product / user updates |
| 20% | Prompts and practical ideas |
| 15% | Explainers — «ШІ без складних слів» |
| 10% | Viral, strange or funny AI |
| 5% | Deeper science for the tech-curious |

If the channel drifts to 80% news for `GENERAL` readers, it has quietly become the
industry newsletter this project set out not to be.
