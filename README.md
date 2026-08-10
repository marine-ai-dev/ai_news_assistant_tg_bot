# AI News Editor Agent

A human-in-the-loop editorial pipeline for a Ukrainian Telegram channel about AI.

**Status: Phase 2 of 7 — RSS/Atom ingestion.** The pipeline collects real items from five
configured feeds and stores them with full provenance. There is no editorial filtering, no
LLM integration and no Telegram integration yet. See
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the full design and the remaining phases.

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

`collect` accepts `--source <id>` (repeatable) to read one feed, and `--dry-run` to fetch and
parse while writing nothing at all. It exits `0` when every source succeeded, `1` when some
failed, and `2` on a configuration or database problem.

`python app.py <command>` works identically without installing the package.

Commands for later phases (`evaluate`, `draft`, `review`, `publish`) are intentionally absent
rather than present as stubs.

## Sources

Feeds are configured in [config/sources.yaml](config/sources.yaml) — human-editable, committed,
and containing no secrets. Each entry declares an `adapter` (only `rss` exists so far), a
`trust_tier`, and an `editorial_role` explaining why the source is in the mix.

Ingestion is faithful to the source: text is stored exactly as the feed supplied it, missing
fields stay missing rather than being invented, and every stored item traces back to its feed
entry. Relevance filtering happens in later phases, not here.

Re-running `collect` does not create duplicates. Identity is `(source_id, external_id)` using
the feed's own guid, falling back to a deterministic hash of the entry link. Conditional GET
(`ETag` / `Last-Modified`) is used when a server supports it, but correctness never depends on
it — two of the five configured feeds send no validators at all.

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
