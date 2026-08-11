# AI News Editor Agent

A human-in-the-loop editorial pipeline for a Ukrainian Telegram channel about AI.

**Status: core MVP live-tested; content model v2 and a private Telegram review bot.** Collects from eight sources, normalizes and
deduplicates deterministically, exports candidates for editorial review and imports ranked
decisions, turns shortlisted stories into Ukrainian draft posts, puts them in front of a human
who approves, edits, rejects or sends them back — and publishes an approved post to a Telegram
channel after a second explicit confirmation. See
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the full design and what was deliberately
left for later.

## Who the channel is for

Ordinary people, not the industry. That includes people who barely use AI: someone who
has opened ChatGPT once, does not know what an API is, has never heard of Notion, and is
not sure whether ChatGPT, Claude and Gemini are different things.

Two independent dimensions describe every post.

**Content type** — what kind of thing it is:

| Type | What it is | Origin |
|---|---|---|
| `NEWS` | Something happened and somebody reported it | An `Article` from a source |
| `PROMPT` | A **tested** workflow somebody demonstrated, retold for our readers | A source, always |
| `EXPLAINER` | One concept, explained without jargon | Written here |

**Audience** — how much the post may assume, lowest first:

`NEWCOMER` → `BEGINNER` → `GENERAL` → `TECH_CURIOUS`

`NEWCOMER` assumes nothing. Audience is a judgement about *who a story matters to*, not
about how simply it could be written — a developer API change is `TECH_CURIOUS` however
gently it is worded.

Editorial guidance for the mix, deliberately **not** enforced anywhere in code: roughly
40–50% of posts at `NEWCOMER`/`BEGINNER`; roughly 30% news, 20% product updates, 20%
prompts, 15% explainers, 10% viral, 5% deeper science. There is no scheduler and no
quota — see [docs/telegram_style_guide.md](docs/telegram_style_guide.md).

**A prompt must rest on a demonstration.** Not an idea that sounds useful — something
somebody actually ran and wrote up. Each one records who tested it, with what tool, what
they asked, what happened and what the limits were; a prompt without that is not
publishable, and a human approving it does not change that. See
[Prompts and explainers](#prompts-and-explainers).

**Editorial-original content gets no shortcut.** Written in-house means *more* exposed
to invented facts, not less. Everything enters the same Draft → human review → approval
gate → publish path as news, and safety tests assert it for every content type.

**There is no LLM API in this project.** The editorial judgement is made by a Claude Code
session reading an exported batch and writing back structured decisions. Python owns
collection, storage, validation and ranking; the JSON schema between them is the seam, so an
automated evaluator could take the same contract later without touching the database.

## The rule this project is built around

Nothing reaches Telegram unless a human explicitly approved **the exact draft version**
being published. That is enforced structurally, not by convention:

- draft content lives in immutable, append-only `draft_versions`; editing appends a version
- a `PublishAuthorization` can only be produced by the approval gate, and names one content hash
- appending content to an `APPROVED` draft returns it to `PENDING_REVIEW` at the storage layer
- the lifecycle state machine rejects every transition that would skip review
- an authorization is never stored; it is rebuilt from the recorded approval on every send
- the Telegram client cannot approve anything, and is never reached for a draft that fails the gate

See `src/ai_news_editor/domain/authorization.py` and `tests/safety/`.

## Requirements

Python 3.11+. No other system dependencies; storage is a local SQLite file.

## Setup

```bash
/opt/homebrew/bin/python3.11 -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

Configuration is optional — the defaults work. To override anything, copy `.env.example`
to `.env`. `.env` is git-ignored and must never be committed.

## Initialize the database

```bash
.venv/bin/ai-news db init
```

Safe to run repeatedly; it applies only pending migrations.

## Commands

| Command | What it does |
|---|---|
| `ai-news version` | Print the application version |
| `ai-news doctor` | Check local health: Python, config, data directory, schema state. Makes no network calls |
| `ai-news db init` | Create the database and apply all migrations |
| `ai-news db migrate` | Apply pending migrations (same operation, clearer name for updates) |
| `ai-news db status` | Show applied and pending migrations, plus row counts |
| `ai-news sources` | List configured sources and what each is for |
| `ai-news collect` | Fetch every enabled source and store new items |
| `ai-news process` | Normalize, deduplicate and screen collected items |
| `ai-news status` | Show the pipeline funnel from raw items to evaluation candidates |
| `ai-news editorial export` | Write a batch of candidates for editorial review |
| `ai-news editorial validate` | Check a reviewed batch without writing anything |
| `ai-news editorial import` | Store editorial decisions (all-or-nothing, idempotent) |
| `ai-news editorial shortlist` | Show the ranked shortlist and any held stories |
| `ai-news editorial status` | Show editorial progress, including stale evaluations |
| `ai-news draft export` | Write assignments for shortlisted stories |
| `ai-news draft validate` | Check finished drafts without writing anything |
| `ai-news draft import` | Store Draft + DraftVersion records (PENDING_REVIEW) |
| `ai-news draft list` / `show` | Read drafts back |
| `ai-news content template` | Write an empty prompt/explainer batch to fill in |
| `ai-news content validate` | Check a batch, with jargon warnings, writing nothing |
| `ai-news content import` | Store content items + drafts (PENDING_REVIEW) |
| `ai-news content list` | Read stored prompts and explainers |
| `ai-news review` | Open the review queue: approve, edit, reject, request a rewrite |
| `ai-news review status` | Show how many drafts sit in each state |
| `ai-news review history <draft-id>` | Show every version and every human decision on a draft |
| `ai-news telegram doctor` | Check the bot, the destination and posting rights. Sends nothing |
| `ai-news telegram whoami` | Print your Telegram user id, for the review bot |
| `ai-news telegram review-bot` | Run the private review bot (long polling, local) |
| `ai-news publish <draft-id>` | Publish one approved draft, after typing `PUBLISH` |
| `ai-news publication approved` | List drafts that are approved and therefore publishable |
| `ai-news publication list` | The publication log: successes, failures, unresolved attempts |

`collect` accepts `--source <id>` (repeatable) to read one feed, and `--dry-run` to fetch and
parse while writing nothing at all. It exits `0` when every source succeeded, `1` when some
failed, and `2` on a configuration or database problem.

`process` accepts `--limit <n>` and `--source <id>`. Both commands are idempotent: running
them repeatedly converges instead of accumulating.

`python app.py <command>` works identically without installing the package.

## Sources and trust tiers

Sources are configured in [config/sources.yaml](config/sources.yaml) — human-editable,
committed, and containing no secrets. Each entry declares an `adapter`, a `trust_tier`, and an
`editorial_role` explaining why the source is in the mix.

Three adapters exist: `rss` for feeds, `html_changelog` for vendors that publish no feed
(driven by CSS selectors in config, never per-site code), and `hn_signal` for Hacker News.

| Trust tier | What it means |
|---|---|
| `OFFICIAL` | The vendor's own announcement |
| `REPUTABLE_SECONDARY` | Established media reporting on it |
| `COMMUNITY_SIGNAL` | People are discussing it |

**Collection is not verification.** A trust tier records *where a claim came from*, never
whether it is true. Community signals are stored in their own table, can never become article
content, and are refused by the normalizer outright — a Hacker News thread is evidence of
attention and nothing else. Deciding what to believe is a later phase, and it will always end
at a human.

## Editorial review

```bash
ai-news editorial export --limit 15
```

A Claude Code session reads the batch, applies [docs/editorial_rubric.md](docs/editorial_rubric.md),
researches anything sensitive or unclear, and writes a reviewed JSON file. Then:

```bash
ai-news editorial import editorial_work/<batch-id>.reviewed.json
```

Nine dimensions are scored 0–100. **Python computes the ranking**, not the evaluator: the
composite comes from validated components using fixed weights, so ordering is deterministic
whoever did the judging.

`credibility` and `general_ai_relevance` are gates rather than weights. A story cannot make up
for being unreliable or off-topic by being entertaining — shortlisting needs credibility ≥ 70,
and a sensitive story (deepfake, scam, accusation) cannot be shortlisted without actual
verification. A good story with thin evidence becomes `HOLD_FOR_VERIFICATION` rather than being
thrown away.

Import is strict and all-or-nothing: one bad review and nothing is written. Re-importing the
same file adds nothing. Evaluations are append-only history, bound by fingerprint to the exact
content reviewed — if an article is later renormalized, its old evaluation is reported as stale
instead of quietly standing in for a current one.

Article text in a batch is untrusted data. A story telling the reviewer to score it 100 and
publish it is just a string in a field: the reviewed schema has no vocabulary for approving or
publishing anything, and a safety test enforces that.

## Draft writing

```bash
ai-news draft export --limit 5
```

A Claude Code session reads the assignments, applies
[docs/telegram_style_guide.md](docs/telegram_style_guide.md), checks the source where the
excerpt is thin, and writes Ukrainian posts. Then:

```bash
ai-news draft import writing_work/<batch-id>.drafts.json
```

Only stories with a **current SHORTLIST evaluation** can be written. Rejected stories
produce nothing, and stories `HOLD_FOR_VERIFICATION` stay blocked until the verification
is actually resolved — writing them anyway would route around the only check between an
unverified claim and a post. A stale evaluation forces re-evaluation first.

Posts come in three formats — `QUICK`, `STANDARD`, `DEEP_DIVE` — with editorial length
targets. Being outside a target is reported; nothing is ever silently cropped.

**Draft ≠ approved. Draft ≠ published.** Every imported draft lands in `PENDING_REVIEW`.
There is no code path from importing a draft to `APPROVED` or `PUBLISHED`, the writing
schema has no field that could request one, and safety tests enforce both. Approving a
post is a separate, explicit human step — the next section.

Python assembles the final post from the headline, body and source line, and computes
the content hash. A writer supplies parts; it never supplies the text a human will
eventually approve.

## Human review

```bash
ai-news review
```

One draft at a time, best editorial score first, showing the post exactly as it would be
sent plus the internal writer notes that never are. Then:

| Key | Action | Effect |
|---|---|---|
| `A` | Approve | `PENDING_REVIEW → APPROVED`, after typing `APPROVE` in full |
| `E` | Edit | Opens `$EDITOR`; saving appends version N+1 and stays in review |
| `R` | Reject | `PENDING_REVIEW → REJECTED`, terminal, nothing is deleted |
| `W` | Needs rewrite | `PENDING_REVIEW → NEEDS_REWRITE` with a note to work from |
| `S` / `N` | Skip / Next | Navigation only — records nothing, changes nothing |
| `Q` | Quit | Stops; everything unreviewed stays exactly as it was |

Approval takes the literal word `APPROVE`. Not `y`, not `yes`, not Enter — a single
accidental keystroke must not put a post in front of an audience.

`--draft <id>` reviews one draft; `--category <name>` reviews one category. There is no
`--yes`, no `--approve-all`, no `--auto-approve`, and no threshold that approves by score.
Those flags are absent because the code behind them does not exist; a safety test asserts
no such option is ever registered.

**`APPROVED` does not mean published.** It means a human read that exact version and said
yes. Approval mints a `PublishAuthorization` naming one draft, one version and one content
hash — and there is no publisher for it to reach yet.

Editing an approved draft invalidates that approval automatically: the new text is a new
version with a new hash, the storage layer returns the draft to `PENDING_REVIEW`, and the
old authorization stops verifying. Nobody has to remember to revoke anything.

```bash
ai-news review history <draft-id>
```

Every version and every recorded decision, oldest first, with whether a valid publication
authorization currently exists.

## Publishing to Telegram

`APPROVED` means a human approved that exact version. Publishing is a **second** decision,
asked separately, because it is the one strangers can see.

### 1. Create the bot

Message [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, and follow the
prompts. It replies with a token that looks like `123456789:AA...`. **That token is a
password for your channel** — anyone holding it can post. It goes in `.env` and nowhere else.

### 2. Add the bot to a channel

Start with a **private test channel**, not the real one. Create a channel, open
*Administrators → Add Administrator*, add your bot, and grant **only** *Post Messages*. It
needs nothing else — no delete, no ban, no member management, no message editing.

A bot cannot see a chat it has not been added to, so this step has to happen before the
next one will work.

### 3. Configure

```bash
cp .env.example .env
```

Set `AI_NEWS_TELEGRAM_BOT_TOKEN` and `AI_NEWS_TELEGRAM_CHANNEL` (a public `@username` or a
numeric chat id). `.env` is git-ignored. Never commit a real token; if one is ever exposed,
revoke it with `/revoke` in BotFather.

### 4. Check the setup without sending anything

```bash
ai-news telegram doctor
```

Calls `getMe`, `getChat` and `getChatMember` and reports the bot identity, the resolved
destination and whether it has posting rights. It never posts a test message. Where the API
cannot answer — `can_post_messages` is not reported for every chat type — it says `UNKNOWN`
rather than claiming a pass.

### 5. Approve something

```bash
ai-news review
```

Then check what is publishable:

```bash
ai-news publication approved
```

### 6. Dry run

```bash
ai-news publish <draft-id> --dry-run
```

Verifies the approval, rebuilds the authorization, renders the exact payload and prints it
with the destination. Makes zero Telegram requests and records nothing.

### 7. Publish

```bash
ai-news publish <draft-id>
```

One draft id, named explicitly — this command never goes looking for something to publish.
It shows the destination and the exact text, then asks:

```
Type PUBLISH to publish:
```

Enter publishes nothing. `y` publishes nothing. `yes` publishes nothing. Only the literal
word `PUBLISH` sends the message.

### 8. Check the record

```bash
ai-news publication list
```

Every attempt: the successes with their Telegram message id, the failures, and any attempt
whose outcome was never learned.

### What happens if something goes wrong

**A definite failure** — bad request, no permission, server error — records a `FAILED`
attempt and returns the draft to `APPROVED`. Retrying does not need a second approval; the
approval was never in question, only the network.

**A lost response** is the interesting one. If the request was sent and the reply never
arrived, the post may or may not exist, and nothing can tell the difference locally.
Telegram's `sendMessage` has no idempotency key, so a retry could produce a duplicate post.
The attempt is therefore recorded as `UNCERTAIN`, the draft stays in `PUBLISHING`, and the
next run refuses to send. **Look at the channel and resolve it yourself** — that is a
judgement about the outside world, and the application does not get to guess.

**A duplicate run** — the same command twice, by accident — sends nothing the second time.
A unique index allows one successful publication per version per destination, and the draft
is already `PUBLISHED`, which is not a publishable state.

SQLite and Telegram cannot share a transaction. This design does not pretend otherwise: it
narrows the window, records every attempt, and stops rather than guessing.

## How processing works

`collect` stores faithful `RawItem` records; `process` derives `Article` candidates from them.
Raw items are never mutated, so every article traces back to the bytes that produced it.

Normalization is strictly mechanical — decode entities, strip markup, collapse whitespace,
canonicalize the URL, compute fingerprints. Nothing is summarized, translated or invented; a
missing publication date stays missing.

Deduplication runs in layers, and every decision records *why*: identical canonical URL,
identical content fingerprint, identical title from the same source, then SimHash within a
bounded Hamming distance. Cross-source resemblance is recorded as a *possible* duplicate and
deliberately not acted on — secondary reporting is worth keeping for corroboration later.

The prefilter is deliberately narrow. It removes empty entries, navigation stubs, job posts and
earnings notices, each with a machine-readable reason. It never filters on technicality: a
deeply technical release can matter enormously to ordinary readers, and judging that is the
LLM editor's job in a later phase, not a keyword list's.

## Tests

```bash
.venv/bin/pytest
```

```bash
.venv/bin/ruff check .
```

Tests are deterministic, use temporary databases, and make no network calls.
`tests/safety/` covers the approval invariants and must never be skipped.

## Layout

```
src/ai_news_editor/
  domain/         entities, lifecycles, approval gate — depends on nothing
  storage/        SQLite connection, SQL migrations, per-entity repositories
  sources/        source adapters: HTTP boundary, RSS/Atom, config, registry
  pipeline/       orchestration — the only layer combining adapters with storage
  content/        prompts and explainers: the batch contract, import, jargon warnings
  review/         review actions and $EDITOR integration, independent of any front end
  publishing/     the approval gate, the Telegram client, and the send-once orchestration
  observability/  structured logging, secret redaction
  cli/            Typer entry points
tests/
  unit/           domain logic, parsing, HTTP boundary, configuration
  contract/       the suite every source adapter must satisfy
  integration/    database, repositories, collection pipeline, CLI
  safety/         approval invariants and integration boundaries
```

## Prompts and explainers

Not everything worth publishing is news. Two formats are written in-house.

```bash
ai-news content template
```

Writes an empty batch. A Claude Code session fills it in following
[docs/telegram_style_guide.md](docs/telegram_style_guide.md).

**For a prompt, research comes first.** Search for a workflow somebody demonstrated,
open the source, confirm the evidence is real, and only then write. The batch requires
the source URL, who tested it, the tool they used, what they asked, what they observed
and any limitations — all of it, or the import fails. Nothing may be inferred to fill a
gap: a source that says "ChatGPT" is recorded as "ChatGPT", never as a model version we
guessed.

For an explainer: one concept, an example from real life, and why it matters. Explainers
stay editorial-original — they explain rather than report, so they carry references
where facts need them rather than a tested demonstration. Then:

```bash
ai-news content validate content_work/<batch>.content.json
```

Checks structure and flags jargon that a `NEWCOMER` post uses without an apparent
explanation. That check is a reading aid with a crude heuristic — it misses things and
occasionally flags a term you did explain. It never blocks an import.

```bash
ai-news content import content_work/<batch>.content.json
```

Creates a `ContentItem` (the editorial substance) plus a Draft in `PENDING_REVIEW`.

**No fake provenance.** An editorial-original draft has `article_id = NULL` and an
origin of `EDITORIAL_ORIGINAL`; the database refuses a prompt that carries an article and
refuses a news draft that has none.

A prompt post names the demonstration it reports and links to it. An explainer carries no
`🔗 Джерело` line, because there is no source to name — its factual references are stored
separately, each recording *what claim it supports*, and are never dressed up as a source
article.

Prompts written before this rule existed are marked `LEGACY_UNVERIFIED` and can never be
published. They were not deleted, not rewritten, and not given a source after the fact —
the honest label for content whose provenance cannot be reconstructed without inventing
it.

## Reviewing from your phone

The same review workflow as `ai-news review`, behind buttons in a private Telegram chat.
It is the same bot that publishes — one BotFather bot, two jobs — and it is for you
alone: every update is checked against one configured account before a draft is loaded.

### Setup, once

```bash
.venv/bin/ai-news telegram whoami
```

Message your bot anything; the command prints your Telegram user id and exits. Put it in
`.env` as `AI_NEWS_TELEGRAM_OWNER_USER_ID`. It stays out of git — it identifies a real
account, and nobody else's id should ever be in there.

### Running it

```bash
.venv/bin/ai-news telegram review-bot
```

Long polling: no webhook, no public URL, no hosting. The bot works for as long as this
process is running on this Mac, and stops when you close the terminal. Nothing is lost
when it stops — every decision was committed when you made it.

### Using it

`/review` shows the next draft awaiting review, best editorial score first — the same
queue and the same ordering the terminal uses.

| Button | What happens |
|---|---|
| ✅ Схвалити | Asks to confirm, then approves **that exact version** |
| ✏️ Редагувати | Send replacement text as one message: first line headline, rest body |
| 📝 Переписати | Marks `NEEDS_REWRITE`. Nothing is regenerated automatically |
| ❌ Відхилити | Asks to confirm, then rejects. Nothing is deleted |
| ⏭ Пропустити | Navigation only — records nothing |
| 📜 Історія | Versions and decisions so far |

Approve and reject both take **two taps**. A single mis-tap on a phone must not decide
anything, which is the same reason the terminal makes you type `APPROVE`.

If the draft changed between the card being drawn and the button being tapped — you
edited it elsewhere, or it was already decided — the tap is refused and the current
version is shown instead. The approval is bound to the version you actually read.

### The review bot does not publish

Approving in Telegram sets `APPROVED` and stops. Publication stays a separate, explicit
act:

```bash
.venv/bin/ai-news publish <draft-id>
```

That is deliberate. Approving is an editorial judgement; publishing is the irreversible
one, and putting both behind adjacent buttons on a phone is how the wrong one gets
tapped. Safety tests assert the bot cannot reach a publisher, cannot build a channel
payload, and cannot construct a `PublishAuthorization`.
