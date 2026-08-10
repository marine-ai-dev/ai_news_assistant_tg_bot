# AI News Editor Agent

A human-in-the-loop editorial pipeline for a Ukrainian Telegram channel about AI.

**Status: Phase 5 of 7 — draft writing.** Collects from eight sources, normalizes and
deduplicates deterministically, then exports candidates for editorial review and imports ranked
decisions, then turns shortlisted stories into Ukrainian draft posts. Drafts stop at
PENDING_REVIEW — there is no approval interface and no Telegram integration yet. See
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the full design and the remaining phases.

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

`collect` accepts `--source <id>` (repeatable) to read one feed, and `--dry-run` to fetch and
parse while writing nothing at all. It exits `0` when every source succeeded, `1` when some
failed, and `2` on a configuration or database problem.

`process` accepts `--limit <n>` and `--source <id>`. Both commands are idempotent: running
them repeatedly converges instead of accumulating.

`python app.py <command>` works identically without installing the package.

Commands for later phases (`evaluate`, `draft`, `review`, `publish`) are intentionally absent
rather than present as stubs.

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
post is a separate, explicit human step that does not exist yet.

Python assembles the final post from the headline, body and source line, and computes
the content hash. A writer supplies parts; it never supplies the text a human will
eventually approve.

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
  observability/  structured logging, secret redaction
  cli/            Typer entry points
tests/
  unit/           domain logic, parsing, HTTP boundary, configuration
  contract/       the suite every source adapter must satisfy
  integration/    database, repositories, collection pipeline, CLI
  safety/         approval invariants and integration boundaries
```
