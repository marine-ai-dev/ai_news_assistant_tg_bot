# AI News Editor Agent — Implementation Plan

**Status:** Planning (Prompt 0). No application code written yet.
**Document owner:** project author
**Last updated:** 2026-08-09

---

## 1. Product goal

Build a local-first, human-supervised editorial pipeline that discovers AI news from the open
internet, evaluates it against an explicitly *popular-science* editorial rubric, writes a
Ukrainian-language Telegram post draft, and publishes it to a Telegram channel **only after a
human being explicitly approves it**.

The system is an *editorial assistant*, not an autopublisher. It is optimised to save the editor
time on discovery, triage and first-draft writing, while keeping final judgement human.

One-sentence framing for the portfolio README:

> A human-in-the-loop AI editorial pipeline: ingests ~10 AI news sources, ranks stories by reader
> interest rather than technical importance, drafts Ukrainian Telegram posts with an LLM, and
> enforces a hard application-level approval gate before anything reaches the channel.

---

## 2. Target audience (of the channel, not the code)

Ordinary AI users: beginners, creatives, office workers, freelancers, students, curious people.
They are assumed to have **no** familiarity with APIs, model architectures, benchmarks or ML
terminology.

The channel reads like a modern popular-science / consumer-technology magazine about AI, not like
an ML research digest.

Audience difficulty tiers used internally:

| Tier | Meaning | Target share of published posts |
|---|---|---|
| `BEGINNER` | Requires zero AI background | ~40% |
| `GENERAL` | Comfortable using AI apps, no technical vocabulary | ~45% |
| `TECH_CURIOUS` | Enjoys "how it works" explanations, still not an engineer | ~15% |

A story that can only be understood by an ML engineer is out of scope by definition.

---

## 3. Editorial principles

### 3.1 The core inversion

Conventional AI feeds rank by technical importance. We explicitly do not.

```
"New CUDA kernel optimisation"        technical=95  reader_interest=15  → reject
"ChatGPT ships a feature you can use  technical=40  reader_interest=95  → strong candidate
 today"
```

`reader_interest` and `usefulness` dominate the composite score. `technical_importance` is
recorded (it is useful signal, and useful for the portfolio write-up) but carries a small weight.

### 3.2 Scoring dimensions

All scored `0–100` by the LLM editor, with a short textual justification per dimension.

| Dimension | Question it answers |
|---|---|
| `credibility` | How confident are we this is true, given source tier and corroboration? |
| `ai_relevance` | Is this genuinely about AI, or AI-adjacent noise? |
| `reader_interest` | Would a non-technical reader stop scrolling? |
| `usefulness` | Can the reader *do* something with this today? |
| `novelty` | Is this new, or a rehash of last month? |
| `wow_factor` | Surprise, delight, "I didn't know that was possible" |
| `virality` | Is this already spreading / likely to be shared? |
| `accessibility` | Can it be explained without jargon? |
| `everyday_impact` | Does it touch normal life, work, study, creativity? |
| `technical_importance` | Recorded for context; deliberately low weight |

### 3.3 Composite score

```
composite = Σ (weight_i × score_i)
```

Default weights (live in `config/editorial.yaml`, tunable without code changes):

```yaml
weights:
  reader_interest:      0.22
  usefulness:           0.18
  accessibility:        0.12
  everyday_impact:      0.12
  novelty:              0.10
  wow_factor:           0.08
  virality:             0.08
  ai_relevance:         0.06
  technical_importance: 0.04
```

`credibility` is deliberately **not** a weight. It is a **gate**:

- `credibility < 60` → cannot be shortlisted, regardless of composite score.
- Categories flagged `sensitive` (deepfakes, scams, accusations, controversies, viral claims)
  require corroboration from **≥ 2 independent sources**, or at least one `OFFICIAL` source,
  otherwise the draft is marked `needs_verification` and the review CLI shows a warning banner.

This keeps "spicy but false" stories out structurally rather than by prompt politeness.

### 3.4 Categories

```
PRODUCT_UPDATE      🆕   USEFUL_TOOL       🛠   WOW               🤯
AI_FAIL             😂   DEEPFAKE_WATCH    🕵   SCAM_MISINFO      🚨
CREATIVE_AI         🎨   AI_FOR_WORK       💼   AI_FOR_LEARNING   📚
EVERYDAY_AI         🌍   TRENDING          🔥   EXPLAINED_SIMPLY  🧠
SCIENCE_LITE        🔬   AI_DRAMA          🍿
```

`DEEPFAKE_WATCH`, `SCAM_MISINFO`, `AI_DRAMA` are marked `sensitive: true` in config and inherit
the stricter corroboration rule above.

### 3.5 Hard editorial exclusions (pre-LLM heuristic filter)

Cheap regex/keyword screening before any LLM call, to protect budget:

- arXiv paper announcements with no consumer angle
- benchmark/leaderboard/SOTA-percentage posts
- CUDA / kernels / inference-infrastructure / quantisation internals
- GitHub commit and release-note churn for developer libraries
- funding rounds and cap-table news with no product consequence *(soft demotion, not a hard drop)*

Each exclusion is a rule with an id, so rejections are explainable in the CLI
(`filtered_by: rule.benchmark_noise`).

---

## 4. Functional requirements

### MVP (V1)

| # | Requirement |
|---|---|
| F1 | Collect items from configured sources via pluggable adapters |
| F2 | Persist every collected item with full provenance, immutably |
| F3 | Normalise items into a canonical article shape |
| F4 | Deduplicate by URL, exact content hash, and near-duplicate title similarity |
| F5 | Apply cheap heuristic prefilter before spending LLM tokens |
| F6 | Score surviving items against the editorial rubric using an LLM |
| F7 | Shortlist top-N items per run by composite score, subject to the credibility gate |
| F8 | Generate a Ukrainian Telegram draft for each shortlisted item |
| F9 | Present shortlisted drafts in an interactive CLI review loop |
| F10 | Support approve / edit / rewrite / reject / skip actions with an audit trail |
| F11 | Publish **only** approved drafts to Telegram, one time each, idempotently |
| F12 | Record publication metadata (message id, chat id, timestamp, draft id) |
| F13 | Trace any published post back to its original source URL and raw payload |
| F14 | `doctor` command that validates config and secrets without printing them |

### Explicit non-requirements for V1

Auto-publishing, web UI, scheduling daemon, image generation, multi-channel, analytics,
comment moderation, A/B testing of headlines.

---

## 5. Non-functional requirements

| Area | Target |
|---|---|
| Runtime | Python 3.11+ (3.11 confirmed available on this machine via Homebrew) |
| Deployment | Single machine, local-first. No Docker/K8s/cloud in MVP |
| Storage | Single SQLite file, WAL mode, checked-in migrations |
| Performance | A full `collect → evaluate → draft` run over ~200 raw items completes in < 5 min |
| Cost | Per-run LLM spend cap enforced in code (default $0.50), run aborts cleanly when exceeded |
| Resilience | One failing source or one malformed item never aborts the run |
| Politeness | Conditional GET (ETag / Last-Modified), per-source rate limiting, honest User-Agent, robots.txt respected for HTML adapters |
| Observability | Structured JSON logs with a `run_id` correlation id; per-phase counters |
| Security | Secrets only via environment; log redaction filter; nothing secret in SQLite |
| Testability | Unit tests run fully offline; network access blocked in the test suite by default |
| Reproducibility | Deterministic behaviour given a fixed fake LLM and fixed fixtures |

---

## 6. Proposed architecture

### 6.1 Shape

A **layered modular monolith** with a one-way dependency rule:

```
cli  →  pipeline  →  {sources, llm, editorial, publishing}  →  storage  →  domain
                              ↑                                              ↑
                          config ─────────────────────────────────────────────
```

- `domain` depends on nothing (pure models + enums + errors).
- `storage` depends only on `domain`.
- Adapters (`sources`, `llm`, `publishing`) depend on `domain` and `config`, **never** on
  `storage` — they are pure I/O boundaries returning domain objects. This is what makes them
  trivially testable with fixtures.
- `pipeline` orchestrates: it is the only layer allowed to combine adapters with repositories.
- `cli` is a thin presentation layer over `pipeline`. No business logic lives here.

Rationale: the interesting engineering in this project is *boundaries and safety*, not
distribution. Microservices, queues, agent frameworks and a vector DB would add operational
surface without adding capability at this scale. Each is listed in the V2/out-of-scope sections
with the trigger that would justify it.

### 6.2 Key boundaries

| Boundary | Contract | Why it exists |
|---|---|---|
| `SourceAdapter` | `fetch(ctx) -> Iterable[RawItem]` | Add a source without touching the pipeline |
| `LLMClient` | `complete(LLMRequest) -> LLMResponse` | Swap provider; run tests without network or spend |
| `Publisher` | `publish(draft, authorization) -> PublicationReceipt` | Telegram is one implementation; the gate is enforced in the *type signature* |
| `Repository` | per-entity, hand-written SQL | Keeps SQL out of pipeline code, allows in-memory DB in tests |

### 6.3 Technology choices

| Choice | Decision | Rationale |
|---|---|---|
| Language | Python 3.11+ | Requested; 3.11 verified present locally |
| DB | SQLite via stdlib `sqlite3`, hand-written SQL migrations | Schema is ~8 tables; an ORM would obscure the interesting constraint work. Repository pattern keeps it swappable |
| Models | Pydantic v2 | Validation at boundaries (feeds, LLM JSON) is the actual hard part; dataclasses would mean hand-rolling that |
| HTTP | `httpx` | Timeouts, retries, HTTP/2, sync + async if ever needed |
| Feed parsing | `feedparser` | Handles the RSS/Atom zoo; battle-tested |
| HTML extraction | `selectolax` (+ `readability-lxml` if needed) | Fast, small, CSS selectors for changelog adapters |
| CLI | `typer` + `rich` | The review loop *is* the product surface; readable output matters |
| Config | YAML (sources, editorial) + `pydantic-settings` (secrets from env) | Non-secret config is reviewable in git; secrets never are |
| Logging | stdlib `logging` + JSON formatter (`structlog` optional) | No heavy dependency needed for correlation ids |
| Tests | `pytest` + `pytest-cov` + `respx` (httpx mocking) | Offline-first |

Total runtime dependency count target: **≤ 10**. Every dependency must earn its place.

---

## 7. Data flow

```
                    ┌──────────────────────────────────────┐
                    │ config/sources.yaml (+ trust tiers)  │
                    └──────────────────┬───────────────────┘
                                       ▼
  Internet ──▶ SourceAdapter.fetch() ──▶ RawItem (immutable, full provenance)
                     │                        │
              per-source isolation            ▼
              ETag / rate limit        [ raw_items table ]
                                              │
                                              ▼
                                     Normalisation
                            (canonical URL, clean text, lang, dates)
                                              │
                                              ▼
                                      Deduplication
                        L0 canonical URL → L1 content hash → L2 simhash
                                              │
                                    ┌─────────┴─────────┐
                                DUPLICATE           unique
                                                        │
                                                        ▼
                                          Heuristic prefilter (free)
                                                        │
                                              ┌─────────┴─────────┐
                                          SCREENED_OUT        survivor
                                                                  │
                                                                  ▼
                                                 Stage A: LLM screener (cheap, batched)
                                                                  │
                                                                  ▼
                                                 Stage B: LLM editor (full rubric)
                                                                  │
                                                                  ▼
                                              credibility gate + composite ranking
                                                                  │
                                                                  ▼
                                                          SHORTLISTED (top-K)
                                                                  │
                                                                  ▼
                                                 Stage C: LLM writer (Ukrainian draft)
                                                                  │
                                                                  ▼
                                                   [ drafts ] status=PENDING_REVIEW
                                                                  │
                        ╔═════════════════════════════════════════▼═══════════════════════════════╗
                        ║              HUMAN REVIEW  (app.py review)  — HARD GATE                  ║
                        ║   approve / edit / rewrite / reject / skip   → review_decisions audit    ║
                        ╚═════════════════════════════════════════╤═══════════════════════════════╝
                                                                  │ APPROVED only
                                                                  ▼
                                              PublishAuthorization (unforgeable token)
                                                                  │
                                                                  ▼
                                           Publisher.publish() → Telegram Bot API
                                                                  │
                                                                  ▼
                                              [ publications ]  UNIQUE(draft_id)
```

---

## 8. Proposed directory tree

```
Telegram_AI_news/
├── app.py                          # thin entry point → ai_news_editor.cli:app
├── pyproject.toml
├── README.md                       # written in Phase 7 (portfolio front door)
├── IMPLEMENTATION_PLAN.md          # this file
├── .env.example                    # committed
├── .env                            # gitignored, never committed
├── .gitignore
│
├── config/
│   ├── sources.yaml                # source instances, trust tiers, schedules
│   └── editorial.yaml              # weights, thresholds, categories, filter rules
│
├── ai_news_editor/
│   ├── __init__.py
│   ├── settings.py                 # pydantic-settings: env + paths + feature flags
│   │
│   ├── domain/
│   │   ├── models.py               # RawItem, Article, Evaluation, Draft, Publication…
│   │   ├── enums.py                # ArticleStatus, DraftStatus, TrustTier, Category…
│   │   ├── authorization.py        # PublishAuthorization (the gate token)
│   │   └── errors.py               # typed exception hierarchy
│   │
│   ├── storage/
│   │   ├── db.py                   # connection factory, WAL, pragmas, migration runner
│   │   ├── migrations/
│   │   │   ├── 001_initial.sql
│   │   │   └── ...
│   │   └── repositories/
│   │       ├── raw_items.py
│   │       ├── articles.py
│   │       ├── evaluations.py
│   │       ├── drafts.py
│   │       ├── publications.py
│   │       ├── review_decisions.py
│   │       ├── source_state.py     # ETag / cursor / last_success
│   │       └── llm_calls.py        # token + cost accounting
│   │
│   ├── sources/
│   │   ├── base.py                 # SourceAdapter protocol, FetchContext, SourceRunReport
│   │   ├── http.py                 # shared httpx client: timeouts, retries, UA, rate limit
│   │   ├── registry.py             # kind → adapter class
│   │   ├── rss.py                  # kind: "rss"
│   │   ├── html_changelog.py       # kind: "html_changelog"
│   │   └── hn_algolia.py           # kind: "hn_signal"  (signal-only)
│   │
│   ├── llm/
│   │   ├── base.py                 # LLMClient protocol, LLMRequest/LLMResponse, Usage
│   │   ├── registry.py             # provider name → client factory
│   │   ├── providers/
│   │   │   ├── openai_compatible.py
│   │   │   └── anthropic.py
│   │   ├── fake.py                 # deterministic client for tests + offline dev
│   │   ├── structured.py           # JSON-schema call + one repair retry + validation
│   │   ├── budget.py               # per-run spend cap
│   │   └── prompts/
│   │       ├── screener.v1.md
│   │       ├── editor.v1.md
│   │       ├── writer_uk.v1.md
│   │       └── style_guide_uk.md
│   │
│   ├── editorial/
│   │   ├── categories.py
│   │   ├── prefilter.py            # free heuristic rules with ids
│   │   ├── rubric.py               # dimension definitions, schema for LLM output
│   │   └── scoring.py              # composite, credibility gate, ranking, shortlist
│   │
│   ├── pipeline/
│   │   ├── collect.py
│   │   ├── normalize.py
│   │   ├── dedupe.py               # canonical URL, content hash, simhash
│   │   ├── evaluate.py
│   │   ├── draft.py
│   │   └── run_report.py           # per-phase counters, run_id
│   │
│   ├── publishing/
│   │   ├── base.py                 # Publisher protocol
│   │   ├── gate.py                 # approve(), the ONLY producer of PublishAuthorization
│   │   ├── telegram.py             # Bot API adapter (Phase 7)
│   │   └── formatting.py           # Telegram HTML escaping + length splitting
│   │
│   ├── cli/
│   │   ├── main.py                 # typer app: collect, evaluate, draft, review, publish…
│   │   ├── review_loop.py
│   │   └── render.py               # rich rendering of the review card
│   │
│   └── observability/
│       ├── logging.py              # JSON formatter, run_id, secret redaction filter
│       └── redaction.py
│
├── tests/
│   ├── conftest.py                 # tmp sqlite, fake LLM, network-block autouse fixture
│   ├── fixtures/
│   │   ├── feeds/                  # recorded RSS/Atom XML
│   │   ├── html/                   # recorded changelog HTML
│   │   └── llm/                    # recorded/canned LLM JSON responses
│   ├── unit/
│   ├── contract/
│   │   └── test_source_adapter_contract.py
│   └── safety/
│       └── test_approval_gate.py   # MUST NEVER BE SKIPPED
│
└── data/
    └── ai_news.sqlite3             # gitignored
```

---

## 9. Core domain models

Pydantic v2 models. Field lists are the *minimum*; types are indicative.

### 9.1 `Source` (config-derived, mirrored into DB for provenance)

```
id                 str        # stable slug, e.g. "openai_news"
name               str
kind               SourceKind # rss | html_changelog | hn_signal
url                str
trust_tier         TrustTier  # OFFICIAL | REPUTABLE_SECONDARY | COMMUNITY_SIGNAL | UNVERIFIED
signal_only        bool       # true → may never be cited as factual evidence
enabled            bool
language           str
publisher          str | None # "OpenAI", "TechCrunch"
poll_interval_min  int
config             dict       # adapter-specific (CSS selectors, query, tags…)
```

### 9.2 `RawItem` — immutable, provenance-complete

```
id                 uuid
source_id          str
external_id        str | None   # feed guid, HN objectID
title_original     str
url_original       str
author             str | None
published_at       datetime | None   # tz-aware UTC
fetched_at         datetime          # tz-aware UTC
summary_raw        str | None
content_raw        str | None
payload_raw        str               # verbatim serialised source payload (JSON/XML fragment)
content_type       str
http_etag          str | None
fetch_run_id       str
```

Never updated after insert. This is the audit anchor: any published post can be traced to the
exact bytes we received.

### 9.3 `Article` — normalised, the pipeline's working unit

```
id                 uuid
raw_item_id        uuid          # 1:1 in V1; V2 introduces a Story cluster above this
source_id          str
title              str
canonical_url      str           # tracking params stripped, host normalised
clean_text         str
language_detected  str
published_at       datetime | None
content_hash       str           # sha256(normalised title + body)
simhash            int           # 64-bit, for near-duplicate detection
duplicate_of       uuid | None
status             ArticleStatus
filtered_by        str | None    # prefilter rule id, when SCREENED_OUT
created_at / updated_at
```

### 9.4 `Evaluation` — one row per (article, evaluator version)

```
id                 uuid
article_id         uuid
stage              EvalStage     # SCREENER | EDITOR
scores             dict[str,int] # all rubric dimensions
rationales         dict[str,str]
composite          float
category           Category
audience           AudienceTier
verdict            Verdict       # KEEP | DROP
why_selected       list[str]     # bullet strings surfaced in the review CLI
needs_verification bool
corroboration      list[str]     # supporting URLs, when applicable
prompt_id          str           # "editor.v1"
model              str
llm_call_id        uuid
created_at
```

Evaluations are append-only. Re-running evaluation writes a new row; nothing is overwritten. This
makes rubric/prompt changes measurable after the fact.

### 9.5 `Draft` and `DraftVersion`

> **Revised in Phase 1.** The original single-entity design (one mutable `Draft` with a
> `version` counter) was split in two during implementation. See §26.2 for why.

`Draft` — stable identity and lifecycle:

```
id                 uuid          # stable across every edit
article_id         uuid
evaluation_id      uuid          # added in Phase 4
status             DraftStatus
current_version_id uuid | None   # null only between creation and the first version
created_at / updated_at
```

`DraftVersion` — immutable content snapshot, append-only:

```
id                 uuid
draft_id           uuid
version_no         int           # unique per draft, starts at 1
title              str
body               str           # Telegram HTML, ≤ 3500 chars (enforced from Phase 5)
hashtags           tuple[str]
category           Category
audience           AudienceTier
source_attribution str           # rendered link line
content_hash       str           # computed, not stored as input — binds approval to exact text
unverified_flags   list[str]     # added in Phase 4
created_by         str           # "llm:writer_uk.v1" | "human"
created_at
```

### 9.6 `ReviewDecision` — audit log, append-only

```
id, draft_id, draft_version, draft_content_hash,
action (APPROVE | REJECT | EDIT | REQUEST_REWRITE | SKIP),
actor, note, created_at
```

### 9.7 `Publication`

```
id, draft_id (UNIQUE), telegram_chat_id, telegram_message_id,
published_at, approved_by, approval_decision_id, status (SENT | FAILED),
error (nullable), attempt_count
```

### 9.8 `LLMCall` — cost accounting

```
id, run_id, prompt_id, provider, model,
prompt_tokens, completion_tokens, estimated_cost_usd,
latency_ms, ok, error, created_at
```

---

## 10. Lifecycle model

Status is deliberately split across two entities, because the *publishable unit is a draft, not an
article*, and one article may go through several draft versions.

### 10.1 `ArticleStatus` — pipeline triage

```
COLLECTED ──▶ NORMALIZED ──┬──▶ DUPLICATE          (terminal)
                           ├──▶ SCREENED_OUT       (terminal, cheap filter)
                           └──▶ EVALUATED ──┬──▶ SHORTLISTED ──▶ DRAFTED
                                            └──▶ DISCARDED      (terminal, LLM verdict)
```

### 10.2 `DraftStatus` — the publishing path

```
DRAFTED ──▶ PENDING_REVIEW ──┬──▶ REJECTED            (terminal)
                             ├──▶ NEEDS_REWRITE ──▶ DRAFTED   (new version)
                             └──▶ APPROVED ──▶ PUBLISHING ──┬──▶ PUBLISHED (terminal)
                                                            └──▶ PUBLISH_FAILED ──▶ APPROVED
```

Rules enforced in the repository layer:

- Every transition goes through a single `transition(entity, to_status)` function that validates
  against an explicit allowed-transitions table. Illegal transitions raise
  `IllegalStateTransition`. There is no direct `UPDATE ... SET status` anywhere else.
- `APPROVED → PUBLISHING` is a compare-and-swap:
  `UPDATE drafts SET status='PUBLISHING' WHERE id=? AND status='APPROVED'`; if `rowcount != 1`,
  publishing aborts. This is the concurrency guard against double-publish.
- `PUBLISH_FAILED` returns to `APPROVED` for retry — it never silently reverts to
  `PENDING_REVIEW`, and it never bypasses the gate.

---

## 11. Human approval gate — the safety mechanism

This is the requirement that most shapes the architecture, so it is defended in **six independent
layers**. Any single one is sufficient; together they make accidental auto-publish require
deliberate sabotage rather than a bug.

**L1 — Type-level.** `Publisher.publish()` does not accept a draft id. Its signature is:

```python
def publish(self, draft: Draft, authorization: PublishAuthorization) -> PublicationReceipt: ...
```

`PublishAuthorization` is a frozen model whose `__init__` is private by convention and whose only
legitimate producer is `publishing.gate.approve_draft()`. It carries `draft_id`,
`draft_content_hash`, `approved_by`, `approved_at`, `decision_id`. You cannot call `publish()`
without having gone through the gate — not because a prompt says so, but because you have nothing
to pass as the second argument.

**L2 — Content binding.** `publish()` recomputes `sha256(draft.telegram_text)` and compares it to
`authorization.draft_content_hash`. If the text changed after approval, publishing raises
`ApprovalInvalidated`. This closes the approve-then-mutate hole.

**L3 — State machine.** The draft must be in `APPROVED` and the compare-and-swap to `PUBLISHING`
must affect exactly one row (§10.2).

**L4 — Database constraint.** `publications.draft_id` is `UNIQUE`. Even a logic bug cannot produce
two publication rows for one draft. The insert happens in the same transaction as the
`PUBLISHING → PUBLISHED` transition.

**L5 — Configuration.** `settings.publishing.auto_publish_enabled` exists, defaults to `false`,
and in MVP the settings validator **raises at startup** if it is set to `true`, with the message
"auto-publish is not implemented in V1". The flag exists only so that its absence is explicit and
testable; there is no code path behind it.

**L6 — Human interaction.** `approve_draft()` is only reachable from the interactive review loop
and requires an explicit typed confirmation. There is no `--approve-all`, no `--yes` flag, and no
non-interactive approval path in V1.

**Test suite `tests/safety/test_approval_gate.py` is a first-class deliverable**, added in Phase 6
and never allowed to be skipped or marked xfail. It asserts, at minimum:

1. `publish()` cannot be called without a `PublishAuthorization` (static + runtime).
2. Publishing a `PENDING_REVIEW` / `REJECTED` / `DRAFTED` draft raises.
3. Publishing the same approved draft twice yields exactly one `publications` row.
4. Mutating draft text after approval invalidates the authorization.
5. `auto_publish_enabled: true` fails settings validation.
6. No module outside `publishing/` imports the Telegram client.

---

## 12. Source adapter strategy

### 12.1 Interface

```python
class SourceAdapter(Protocol):
    kind: ClassVar[str]

    def __init__(self, source: Source, http: HttpClient) -> None: ...

    def fetch(self, ctx: FetchContext) -> Iterable[RawItem]: ...
```

- `FetchContext` carries `run_id`, `since` cursor, prior `etag`/`last_modified`, and a logger.
- Adapters are **pure I/O + parse**. They never touch the database, never decide relevance, and
  never mutate state. They yield `RawItem`s and raise typed errors.
- Adapters are registered by `kind` in `sources/registry.py`; instances are declared in
  `config/sources.yaml`. Adding a source is a YAML edit; adding a *type* of source is one new file
  plus a registry line.
- Every adapter must pass the shared contract test suite (`tests/contract/`): yields well-formed
  `RawItem`s from fixtures, is idempotent across two runs, honours `since`, tolerates malformed
  input without raising, and never returns items with a missing `url_original`.

### 12.2 Cross-cutting behaviour (in `sources/http.py`, not in each adapter)

Timeouts, bounded retries with exponential backoff and jitter, per-source rate limiting,
conditional GET via stored ETag/Last-Modified, honest User-Agent with contact URL, robots.txt
check for HTML adapters, response size cap, and per-source error isolation — a source that throws
is recorded in the `SourceRunReport` and the run continues.

### 12.3 Trust model

```
OFFICIAL             vendor's own blog / changelog / newsroom
REPUTABLE_SECONDARY  established tech media with editorial standards
COMMUNITY_SIGNAL     forums/aggregators — indicates attention, never evidence
UNVERIFIED           anything else
```

Enforced rules:

- `signal_only` sources can raise `virality`/`reader_interest` and can *trigger investigation*,
  but may never be the sole basis for a factual claim in a draft.
- Sensitive categories require ≥ 2 independent corroborating sources or 1 `OFFICIAL` source.
- The writer prompt receives the trust tier and must frame uncorroborated items as reported claims
  ("за повідомленням X"), never as fact. The `needs_verification` flag is set by the *editor*
  stage, in code, and rendered as a banner in the review CLI — it is not left to the writer's
  discretion.

---

## 13. Proposed initial source strategy

Deliberately **four source types**, not dozens of sources. I verified availability on
2026-08-09 rather than assuming.

| # | Kind | Purpose | Verification result |
|---|---|---|---|
| 1 | `rss` (OFFICIAL) | Vendor announcements — the backbone of `PRODUCT_UPDATE` | ✅ `openai.com/news/rss.xml` returns valid RSS 2.0; ✅ `blog.google/technology/ai/rss/` valid; ✅ `huggingface.co/blog/feed.xml` valid |
| 2 | `html_changelog` (OFFICIAL) | Vendors with **no** feed — needed because the consumer-product vendors we care about mostly lack RSS | ✅ Gap confirmed: `anthropic.com/news/rss.xml` and `anthropic.com/rss.xml` both 404; `notion.com/releases/feed.xml` 404. CSS-selector-driven adapter is genuinely required, not a nice-to-have |
| 3 | `rss` (REPUTABLE_SECONDARY) | Weird / viral / deepfake / scam / drama stories that vendors never publish about themselves | ✅ `techcrunch.com/category/artificial-intelligence/feed/` valid RSS |
| 4 | `hn_signal` (COMMUNITY_SIGNAL) | Attention signal + discovery of stories the other three missed | ✅ `hn.algolia.com/api/v1/search_by_date` — free, no API key, no auth |

Starting instance count: **8–10 sources**, roughly 4 official RSS, 2 HTML changelogs, 2–3 media
feeds, 1 HN signal query. The exact list belongs in `config/sources.yaml` and is tuned in Phase 2
against real output, not decided now.

### Explicitly deferred, with reasons

| Source | Why not in MVP |
|---|---|
| **Reddit** | Free tier is non-commercial-only, and self-service registration is closed — new OAuth clients need manual approval with a reported 2–4 week turnaround. Cannot be a Phase-2 dependency. Ships as an optional `hn_signal`-style adapter in V2 |
| **X / Twitter** | Never a required dependency, per project constraints. Optional adapter, V2+ |
| **YouTube, Product Hunt, paid news APIs** | Not needed to prove the pipeline; add later if the shortlist is thin |
| **Full-text scraping of paywalled media** | Legal and ethical risk; we summarise and link, never republish |

### Copyright / attribution position

We never reproduce source articles. Drafts are original Ukrainian summaries with a mandatory
attribution line and link back. Quotes, if any, are short and attributed. This is enforced by a
draft validator (length ratio vs source, and a check that the attribution line is present), not
only by prompt.

---

## 14. LLM abstraction strategy

### 14.1 Interface

```python
class LLMClient(Protocol):
    name: str
    def complete(self, request: LLMRequest) -> LLMResponse: ...
```

`LLMRequest`: `system`, `user`, `json_schema | None`, `temperature`, `max_tokens`, `prompt_id`.
`LLMResponse`: `text`, `parsed | None`, `usage(prompt_tokens, completion_tokens)`, `model`,
`latency_ms`, `raw`.

No provider is named at architecture level. `LLM_PROVIDER` + `LLM_MODEL` + `LLM_API_KEY` in env
select an implementation from `llm/registry.py`. Two reference implementations are planned
(`openai_compatible`, `anthropic`), plus `fake.py`.

### 14.2 Structured output discipline

1. Request JSON constrained by a schema derived from the Pydantic rubric model.
2. Validate the response. On validation failure, **one** repair attempt with the validation error
   fed back.
3. On second failure: record the evaluation as `FAILED`, log the raw response, skip the item.
   **Never** fabricate a score, never fall back to a default. A silent default here would corrupt
   the ranking and is a bug class worth designing out explicitly.

### 14.3 Three logical services, one provider, no agent framework

| Service | Prompt | Model class | Called on |
|---|---|---|---|
| Screener | `screener.v1` | cheap/small | all prefilter survivors, batched ~15/call |
| Editor | `editor.v1` | mid/large | screener survivors only |
| Writer | `writer_uk.v1` | mid/large | top-K shortlisted only |

These are three prompt+schema pairs behind one client — not agents, no tool loop, no planner. The
funnel exists for cost control: the expensive model only ever sees a small fraction of items.

### 14.4 Prompt versioning and cost accounting

Prompts are files named `<id>.v<N>.md`; the version is persisted on every evaluation and draft, so
output quality is attributable to a specific prompt revision. Every call writes an `llm_calls` row
with tokens, estimated cost and latency. `budget.py` enforces a per-run USD cap and aborts the run
cleanly (finishing the DB writes for work already done) when it is hit.

**Model IDs and prices are configuration, not code**, and must be re-verified when the project is
picked up — a price table hardcoded today will be wrong within months.

---

## 15. Telegram publishing strategy

**Not implemented before Phase 7.** Design intent:

- Official Bot API over HTTPS via `httpx`. No third-party wrapper library needed for `sendMessage`
  plus a couple of admin calls.
- `parse_mode=HTML`, **not** MarkdownV2 — MarkdownV2 requires escaping 18 characters and is a
  reliable source of publishing bugs. A `formatting.py` module escapes `& < >` and whitelists the
  handful of tags Telegram supports.
- Draft body capped at **3500 chars** against Telegram's 4096 limit, leaving headroom for the
  attribution line and hashtags. Validation happens at draft creation, so the editor never
  approves something that cannot be sent.
- Rate limiting: current guidance is ~30 messages/second overall but only **~20 messages/minute to
  a single chat/channel**. Since a human approves each post, natural pacing keeps us far below
  this; the publisher still implements a conservative limiter and honours `retry_after` on HTTP
  429 (flood wait) with bounded retries.
- `--dry-run` renders the exact payload and target chat without sending. This is the default in
  any non-interactive context.
- On success, insert `publications` in the same transaction as the `PUBLISHING → PUBLISHED`
  transition. On failure, `PUBLISH_FAILED` with the error recorded and `attempt_count`
  incremented; retry is a human-initiated command.
- Bot permissions: the bot needs only "post messages" in the target channel. No admin rights
  beyond that, no message deletion, no user management.
- Channel id comes from `TELEGRAM_CHANNEL_ID` in env; a `doctor` check calls `getMe` and
  `getChat` to confirm connectivity and permissions without posting anything.

---

## 16. Secrets and security strategy

- Secrets live in `.env` (gitignored) or the real environment. `.env.example` is committed with
  empty values and comments. Nothing secret ever goes into `config/*.yaml`, SQLite, or logs.
- `.gitignore` is written in **Phase 1, before the first commit** — covering `.env`, `data/`,
  `*.sqlite3*`, `__pycache__`, `.venv`, `.pytest_cache`.
- `observability/redaction.py` installs a logging filter that redacts anything matching known
  secret shapes (bot-token pattern, `sk-…`, values of any settings field marked `SecretStr`).
- `settings.py` uses `pydantic.SecretStr` so accidental `print(settings)` cannot leak values.
- `app.py doctor` validates presence and *shape* of every secret and reports ✅/❌ per item,
  printing no values.
- Outbound HTTP is restricted to hosts derived from configured sources plus the LLM and Telegram
  endpoints; a stray URL in feed content is never fetched automatically.
- Content from sources is **data, never instruction**. The editor and writer prompts wrap source
  text in delimiters and state that instructions inside it must be ignored — and, more
  importantly, the *structural* protection is that the LLM's output is schema-validated and can
  only ever produce scores and Ukrainian prose. It cannot cause a fetch, a DB write, or a publish.
  Prompt injection in a feed item cannot reach the publishing gate.

---

## 17. Deduplication strategy

Layered, cheapest first, no vector database:

| Layer | Method | Catches |
|---|---|---|
| L0 | Canonical URL: lowercase host, strip `www.`, drop `utm_*`/`fbclid`/`ref`/`gclid`, normalise trailing slash, resolve known shorteners | The same URL arriving from two feeds |
| L1 | `sha256` of normalised (lowercased, whitespace-collapsed, punctuation-stripped) title + body | Verbatim syndication |
| L2 | 64-bit **simhash** over token shingles, Hamming distance ≤ 3, compared against a 14-day window | Reworded headlines, feed-vs-page variants |
| L3 | *(V2)* Embedding or LLM clustering into a `Story` entity | "Same event, five outlets" |

Additionally: a `published_url_index` check prevents re-publishing a story whose canonical URL was
already published, even across runs and even if the dedup window has expired.

Simhash is ~60 lines of pure Python, deterministic, and testable with golden fixtures — a much
better fit at this scale than a vector store, and the tradeoff is worth documenting in the
portfolio README.

---

## 18. Testing strategy

| Layer | What it covers | Notes |
|---|---|---|
| Unit | Normalisation, canonical URLs, simhash, scoring math, state transitions, formatting/escaping | Fast, pure, no I/O |
| Contract | Every `SourceAdapter` against the shared suite | New adapter = automatically tested |
| Integration | Full pipeline over recorded fixtures + `FakeLLMClient` + temp SQLite | End-to-end, fully offline |
| Safety | `tests/safety/test_approval_gate.py` | Never skipped; the project's reason for existing |
| Golden | Dedupe decisions, ranking order, prefilter rule hits | Detects silent regressions from tuning |

Rules:

- `conftest.py` installs an autouse fixture that blocks socket connections. Any test that
  accidentally hits the network **fails**. Network-touching tests are explicitly marked and
  excluded from the default run.
- No test ever calls a real LLM or a real Telegram endpoint.
- Fixtures are real recorded payloads (feed XML, changelog HTML, LLM JSON), stored in
  `tests/fixtures/`, so parser tests reflect reality rather than idealised input.
- Coverage target: ≥ 80% overall, **100% on `publishing/gate.py` and the transition table**.

---

## 19. Logging and error handling strategy

- Structured JSON logs to stdout; human-readable `rich` output is a separate concern in the CLI.
- Every pipeline run generates a `run_id` (UUID) attached to every log line, every `raw_item`,
  every `llm_call`. One id ties a published post to everything that produced it.
- Per-phase counters aggregated into a `RunReport` printed at the end of each command:
  `sources_ok/failed`, `items_fetched`, `duplicates`, `screened_out`, `evaluated`, `shortlisted`,
  `drafted`, `llm_calls`, `estimated_cost_usd`, `wall_time`.
- Typed exception hierarchy rooted at `AiNewsError`, split into `RetryableError` (network,
  429/5xx, LLM timeout) and `FatalError` (config invalid, migration failure, illegal transition).
- Isolation rule: a failure fetching one source, or parsing one item, or evaluating one article,
  is logged with context and skipped. Only configuration and database errors abort a run.
- Exit codes: `0` success, `1` partial success with source failures, `2` fatal.

---

## 20. MVP acceptance criteria

The MVP is done when all of the following are true:

1. `python app.py doctor` validates config, DB and secrets, printing no secret values.
2. `python app.py collect` fetches from ≥ 8 configured sources across ≥ 3 adapter kinds, stores
   raw items with complete provenance, and survives a deliberately broken source.
3. Re-running `collect` immediately fetches ~0 new items (conditional GET + dedupe both work).
4. `python app.py evaluate` scores items through screener + editor and produces a shortlist where
   spot-checking confirms consumer-relevant stories outrank technically-important-but-dry ones.
5. Every rejection is explainable: the CLI can show why any given item was dropped, and by which
   rule or which dimension.
6. `python app.py draft` produces Ukrainian drafts that are ≤ 3500 chars, carry an attribution
   line, and are valid Telegram HTML.
7. `python app.py review` presents the review card (category, interest, credibility, original
   title, source, URL, why-selected bullets, draft) and supports
   approve / edit / rewrite / reject / skip / next.
8. Publishing a non-`APPROVED` draft is **impossible** — demonstrated by the safety test suite,
   not by inspection.
9. An approved draft publishes to a real test channel exactly once; a second attempt is a no-op
   with a clear message.
10. Any published post can be traced back to its raw payload in one query.
11. `pytest` passes fully offline, and the safety suite is green.
12. `README.md` explains the architecture, the editorial thesis, and the approval gate well enough
    for a reviewer who never runs the code.

---

## 21. V2 backlog

Ordered roughly by value:

1. **Story clustering** — a `Story` entity above `Article`, merging multi-outlet coverage of one
   event; unlocks "5 sources agree" corroboration automatically.
2. **Telegram review bot** — approve/reject from the phone via inline keyboard, replacing the
   terminal for day-to-day use. (The gate stays identical; only the UI changes.)
3. **Scheduling** — cron/launchd wrapper plus a digest notification when a shortlist is ready.
4. **Reddit adapter** (once API access is approved) and an **X adapter**, both `signal_only`.
5. **Fact-verification pass** — a dedicated LLM step that seeks corroboration for sensitive claims
   before drafting.
6. **Editorial memory** — track what was published, avoid repeating topics, learn from human
   reject/approve decisions to retune weights.
7. **Media handling** — pull the OG image, publish as photo-with-caption.
8. **Post-publication analytics** — views/forwards back into the interest model.
9. **Embedding-based dedupe** (L3), once simhash demonstrably fails on real data.
10. **Optional auto-publish mode** — for one narrow, high-trust category only, behind an
    explicit flag, with a delay window allowing human cancellation. Still not the default. Only
    after months of measured precision.

## 22. Explicitly out of scope

Web frontend, Streamlit, React, cloud deployment, Docker, Kubernetes, microservices, message
queues, an autonomous multi-agent framework, a vector database, fine-tuning, a self-hosted model,
multi-channel/multi-language publishing, a CMS, and user accounts.

Each would be justified only by a specific trigger (e.g. a vector DB only if simhash provably
misses cross-outlet duplicates at scale). Adding them now would be resume-driven design and would
make the portfolio *weaker*, not stronger.

---

## 23. Known risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | LLM hallucinates a detail in a Ukrainian draft; a false claim reaches the channel | **High** | Human gate; draft shows source alongside; writer prompt forbids facts absent from source text; V2 verification pass |
| R2 | LLM cost drifts up silently | Medium | Per-call accounting, per-run cap, funnel architecture, cheap model for screening |
| R3 | HTML changelog adapters break when a vendor redesigns | Medium | Selector config in YAML, per-source failure isolation, `doctor` reports sources that returned 0 items N runs in a row |
| R4 | Defamation / harm risk on scam, deepfake and accusation stories | **High** | Sensitive categories require corroboration; `needs_verification` banner; human gate; cautious framing enforced by the editor stage, not the writer |
| R5 | Shortlist is too thin (not enough consumer-relevant AI news daily) | Medium | Four source types from day one; media feeds and HN signal broaden coverage; thresholds are config |
| R6 | Ukrainian output quality is mediocre or reads translated | Medium | Dedicated style guide file; human edit action in the review loop; prompt versioning makes improvement measurable |
| R7 | Copyright / republication concerns | Medium | Summarise-and-link only; length-ratio validator; mandatory attribution |
| R8 | Prompt injection via feed content | Low–Medium | Schema-constrained output; LLM cannot trigger actions; delimiters + explicit instruction; the gate is structural |
| R9 | Feeds provide truncated content, weakening evaluation | Medium | Optional full-page fetch for OFFICIAL sources; record `content_completeness` and let the editor account for it |
| R10 | Over-reliance on a single secondary outlet skews the feed | Low | Per-source caps in the shortlist step (max N items per source per run) |
| R11 | Telegram formatting/escaping bug garbles a post | Low | HTML mode, escaping unit tests, `--dry-run`, human sees final text before approval |

---

## 24. Implementation phases

Seven phases. Each is a separate prompt/session. **Do not start Phase 1 without explicit
instruction.** Recommendation: `git init` and the first commit (`.gitignore` + this plan) happen at
the start of Phase 1, before any code. No remote, no push.

---

### Phase 1 — Foundations: config, domain, storage, CLI skeleton

**Goal.** A runnable, empty-but-correct application: settings load, DB migrates, models validate,
CLI responds, logging is structured. Zero network, zero LLM.

**Files.** `pyproject.toml`, `.gitignore`, `.env.example`, `app.py`,
`ai_news_editor/settings.py`, `domain/{models,enums,errors}.py`,
`storage/{db.py,migrations/001_initial.sql,repositories/*}`,
`observability/{logging.py,redaction.py}`, `cli/main.py`, `tests/conftest.py`, `tests/unit/*`.

**Acceptance criteria.**
- `python app.py --help` lists command stubs; `python app.py doctor` reports config/DB status.
- `python app.py db init` creates the SQLite file, applies migrations, is idempotent.
- All entities from §9 exist as validated models with matching tables.
- The status-transition table exists and rejects illegal transitions.
- Logs are JSON with a `run_id`; the redaction filter masks a planted fake token.

**Tests that must pass.** Model validation (incl. tz-aware datetime enforcement); migration
idempotency; every repository CRUD round-trip; legal and illegal transitions; redaction filter;
settings validation fails loudly on missing required config.

**Do NOT implement.** Any source adapter, any HTTP call, any LLM code, anything Telegram, the
review loop.

---

### Phase 2 — Source adapter framework + RSS collection

**Goal.** `python app.py collect` really fetches from real RSS feeds and stores provenance-complete
raw items.

**Files.** `sources/{base,http,registry,rss}.py`, `config/sources.yaml`,
`storage/repositories/source_state.py`, `pipeline/{collect.py,run_report.py}`,
`tests/contract/test_source_adapter_contract.py`, `tests/fixtures/feeds/*`.

**Acceptance criteria.**
- `SourceAdapter` protocol, `FetchContext`, `SourceRunReport` defined as in §12.
- `rss` adapter handles both RSS 2.0 and Atom, missing dates, missing authors, relative URLs.
- Conditional GET works: second run sends `If-None-Match` and stores ~0 new items.
- ≥ 5 real sources configured across OFFICIAL and REPUTABLE_SECONDARY tiers.
- A deliberately broken source (bad URL) is reported as failed; the run still succeeds with exit 1.
- Rate limiting and timeouts are applied per source; User-Agent is honest.

**Tests.** Contract suite passes for `rss`; fixture-based parsing of ≥ 3 real recorded feeds;
idempotency across two runs; malformed XML tolerated; network-blocking fixture is active.

**Do NOT implement.** Normalisation, dedupe, any LLM, HTML adapters, the HN adapter.

---

### Phase 3 — Normalisation, deduplication, prefilter + second-wave adapters

**Goal.** Raw noise becomes a clean, deduplicated, cheaply-filtered candidate set — still with zero
LLM spend.

**Files.** `pipeline/{normalize,dedupe}.py`, `editorial/prefilter.py`,
`sources/{html_changelog,hn_algolia}.py`, `config/editorial.yaml` (filter rules section),
`cli/main.py` (`normalize`, `stats`), golden fixtures.

**Acceptance criteria.**
- Canonical URL, clean text extraction, language detection, content hash, simhash all implemented.
- Three dedupe layers work; duplicates get `duplicate_of` set rather than being deleted.
- Prefilter rules have ids; `SCREENED_OUT` articles record `filtered_by`.
- `html_changelog` adapter works against ≥ 2 real vendor pages with no RSS (Anthropic newsroom and
  one product changelog), driven by YAML CSS selectors.
- `hn_signal` adapter fetches from the Algolia endpoint and marks items `signal_only`.
- `python app.py stats` prints the funnel: fetched → normalised → duplicates → screened → survivors.

**Tests.** Golden tests for canonical URLs and simhash pairs (near-dup ⇒ matched, distinct ⇒ not);
prefilter rule hits on curated positive/negative examples; contract suite passes for both new
adapters; extraction against recorded HTML fixtures.

**Do NOT implement.** Any LLM call, scoring, drafting, review, Telegram.

---

### Phase 4 — LLM abstraction + screener and editor

**Goal.** Candidates are scored against the rubric and ranked, with cost accounted and capped.

**Files.** `llm/{base,registry,structured,budget,fake}.py`, `llm/providers/*`,
`llm/prompts/{screener.v1.md,editor.v1.md}`, `editorial/{rubric,scoring,categories}.py`,
`pipeline/evaluate.py`, `storage/repositories/{evaluations,llm_calls}.py`.

**Acceptance criteria.**
- `LLMClient` protocol with ≥ 1 real provider plus `FakeLLMClient`; provider chosen by env only.
- Structured output validated; exactly one repair retry; second failure records `FAILED` and skips
  — no fabricated defaults anywhere.
- Screener batches items; editor runs only on survivors.
- Composite score, configurable weights, credibility gate, sensitive-category corroboration rule,
  per-source cap on shortlist all implemented.
- Every LLM call writes an `llm_calls` row; exceeding the run budget aborts cleanly with work
  already done persisted.
- `python app.py evaluate` prints the ranked shortlist with per-dimension scores.

**Tests.** Full evaluate pipeline with `FakeLLMClient` (offline); schema-violation → repair →
success path; repair-fails path; scoring math golden tests; credibility gate blocks a high-composite
low-credibility item; budget cap triggers; the "CUDA vs ChatGPT feature" case from §3.1 ranks
correctly with canned scores.

**Do NOT implement.** The Ukrainian writer, drafts, review, Telegram.

---

### Phase 5 — Ukrainian writer and draft generation

**Goal.** Shortlisted articles become publish-ready Ukrainian Telegram drafts — created, validated,
versioned, and stored, but not yet reviewable or publishable.

**Files.** `llm/prompts/{writer_uk.v1.md,style_guide_uk.md}`, `pipeline/draft.py`,
`publishing/formatting.py`, `storage/repositories/drafts.py`, `cli/main.py` (`draft`).

**Acceptance criteria.**
- Writer produces structured output: `title`, `telegram_text`, `hashtags`, `category`, `audience`,
  `source_attribution`, `unverified_flags`.
- Draft validator enforces: ≤ 3500 chars, valid Telegram HTML (whitelisted tags, escaped entities),
  attribution line present, summary/source length ratio within bounds, Ukrainian language detected,
  cautious framing when `needs_verification` is set.
- Validation failure triggers one regeneration attempt, then the draft is stored as `FAILED` and
  reported — never silently published-shaped garbage.
- Drafts are versioned; regenerating creates version N+1 and preserves history.
- `python app.py draft` prints generated drafts to the terminal.

**Tests.** Formatting/escaping unit tests (including adversarial `<script>` and stray `&`);
length-cap enforcement; missing-attribution rejection; versioning; full draft generation offline
with `FakeLLMClient`; golden draft output for a fixed fake response.

**Do NOT implement.** Approval, `PublishAuthorization`, anything Telegram-network.

---

### Phase 6 — Review CLI and the approval gate

**Goal.** The human-in-the-loop surface, and the safety mechanism, complete and provably enforced —
with still **no** ability to reach Telegram.

**Files.** `cli/{review_loop,render}.py`, `publishing/{base,gate}.py`,
`domain/authorization.py`, `storage/repositories/review_decisions.py`,
`tests/safety/test_approval_gate.py`.

**Acceptance criteria.**
- `python app.py review` renders the review card from §20.7 and loops over pending drafts.
- Actions: approve, edit (opens `$EDITOR`, re-validates, bumps version, **clears any prior
  approval**), rewrite (regenerates with optional human note), reject (with reason), skip, next,
  quit. All are recorded in `review_decisions`.
- `approve_draft()` is the only producer of `PublishAuthorization`; it requires explicit typed
  confirmation and binds the draft content hash.
- A `NullPublisher` proves the end-to-end approve→publish path without any network.
- `auto_publish_enabled: true` fails settings validation at startup.
- `needs_verification` drafts render a prominent warning banner before the approve prompt.

**Tests.** The full safety suite from §11 (all six assertions), green and unskippable; review-loop
action tests driven by scripted input; edit-invalidates-approval; audit trail completeness;
`publishing/gate.py` at 100% coverage.

**Do NOT implement.** The Telegram HTTP client. This phase must be demonstrably safe *before*
a real send path exists — that ordering is intentional.

---

### Phase 7 — Telegram publisher, hardening, documentation

**Goal.** Approved drafts reach a real channel, exactly once, and the project is presentable as a
portfolio piece.

**Files.** `publishing/telegram.py`, `cli/main.py` (`publish`, `doctor` extensions),
`storage/repositories/publications.py`, `README.md`, `.env.example` (Telegram vars), CI config
(optional).

**Acceptance criteria.**
- Telegram adapter implements `Publisher`; refuses to send without a valid `PublishAuthorization`.
- `--dry-run` prints the exact payload and target chat, sends nothing, and is the default outside
  the interactive review loop.
- Publishing is transactional: `APPROVED → PUBLISHING` compare-and-swap, send, then
  `publications` insert + `PUBLISHED` in one transaction. A second attempt is a clear no-op.
- HTTP 429 `retry_after` honoured; failures land in `PUBLISH_FAILED` with the error recorded and
  are retryable by a human command.
- `doctor` verifies bot connectivity and channel permissions via `getMe`/`getChat` without posting.
- Full trace query documented: published post → draft → evaluation → article → raw payload → source.
- README covers the editorial thesis, architecture diagram, the approval gate, setup, and honest
  limitations.

**Tests.** Publisher tests with mocked HTTP (`respx`) — success, 429-then-success, 5xx-then-fail,
double-publish no-op; unauthorised-publish raises; the whole safety suite still green; end-to-end
offline integration test from fixture feed to `PUBLISHED` via `NullPublisher`; coverage ≥ 80%.

**Do NOT implement.** Scheduling, auto-publish, a Telegram review bot, media/photo posts,
analytics. Those are V2.

---

## 25. Open questions for the project owner

1. **LLM provider and budget** — which provider, and what monthly ceiling? This sets the default
   in `budget.py` and which provider module gets written first in Phase 4.
2. **Cadence and volume** — how many posts per day is the target? It determines shortlist top-K
   and how aggressive the thresholds should be.
3. **Tone** — how informal should the Ukrainian voice be (emoji density, «ти» vs «ви»)? This is the
   core of the style guide in Phase 5.
4. **Existing channel** — is there a channel already, with posts that could serve as few-shot
   style examples? That would measurably improve Phase 5 output.
5. **Test channel** — a private throwaway channel should exist before Phase 7 so publishing is
   never first tested against the real audience.

None of these block Phase 1.

---

## 26. Phase 1 implementation notes

Recorded after building the foundation. Everything else in this document stands as
written; only the items below changed.

### 26.1 Layout: `src/` instead of a flat package

The package lives at `src/ai_news_editor/` rather than `ai_news_editor/`. A src-layout
means tests import the *installed* package rather than accidentally importing the
working directory, so a packaging mistake fails locally instead of after publication.
`app.py` at the repository root still works without installing anything.

### 26.2 `Draft` split into `Draft` + `DraftVersion`

The plan modelled a draft as one mutable row with a `version` integer. Implementing the
approval gate made the weakness obvious: if the row a decision points at can be
rewritten, "approval binds to the exact reviewed content" is a convention rather than a
guarantee.

The split makes it structural:

* `draft_versions` is immutable and append-only, enforced by SQLite triggers that abort
  any UPDATE or DELETE.
* `content_hash` is a *computed* property of `DraftVersion`, not a stored input, so a
  version whose hash disagrees with its text cannot be constructed.
* Appending a version to an `APPROVED` draft returns it to `PENDING_REVIEW` inside the
  repository, so editing approved content invalidates the approval at the storage layer.
* A stable `Draft.id` survives every edit, which is what the future Telegram review bot
  and publication queue need to reference.

### 26.3 New small modules not in the original tree

* `domain/clock.py` — UTC-only datetime handling; naive datetimes are rejected at the
  model boundary rather than silently assumed to be local time.
* `domain/content.py` — the canonical content fingerprint the approval gate depends on.
* `health.py` — `doctor`'s checks as structured data. The CLI only renders them, which
  keeps business logic out of the presentation layer and makes the checks assertable
  without parsing terminal output.

### 26.4 Migration runner does not use `executescript`

`sqlite3.Cursor.executescript` issues an implicit COMMIT, which would silently defeat
the transaction wrapping each migration. Statements are split with
`sqlite3.complete_statement`, which correctly understands the `BEGIN ... END` body of a
trigger where naive splitting on `;` would not. Migrations also carry a checksum, so
editing one that has already been applied is an error rather than a silent divergence.

### 26.5 Deferred to the phase that uses them

No table, column or setting was created ahead of the phase that consumes it:

| Deferred | Phase |
|---|---|
| `evaluations` table, `Draft.evaluation_id`, `unverified_flags` | 4 |
| `source_state` (ETag / cursor), `llm_calls` | 2 / 4 |
| `publications` table, publication queue, scheduled timestamps | 7+ |
| LLM and Telegram settings | 4 / 7 |

The Phase 1 keys and relationships were checked against these additions: a
`publications` table with `UNIQUE(draft_id)` and a nullable `scheduled_for`, and a queue
ordering column, all attach to the existing stable `Draft.id` without touching the
Article or Draft models.

### 26.6 Phase 2 notes (ingestion)

**Source verification.** All five preferred sources were fetched and confirmed on
2026-08-10 before being configured; none had to be substituted. The Microsoft 365 feed
initially looked like an HTML page because it answers with an interstitial redirect —
the final response is valid RSS 2.0.

**Conditional GET is an optimisation, never a correctness mechanism.** Of the five
configured feeds, only three (Hugging Face, Microsoft 365, TechCrunch) send `ETag` or
`Last-Modified`; OpenAI and Google send neither and return the full body every time.
Idempotency therefore rests on `(source_id, external_id)` identity, with conditional GET
saving bandwidth where servers support it. A live second run confirmed this: three
sources answered 304, two returned full bodies, and zero duplicate items were stored.

**Ingestion identity vs editorial deduplication.** `add_if_absent` uses
`INSERT ... ON CONFLICT DO NOTHING` against a partial unique index. This is *ingestion
idempotency only* — it says nothing about two different outlets covering one story,
which is editorial deduplication and belongs to Phase 3.

**`payload_raw` holds the parsed entry, not the raw XML slice.** feedparser does not
expose per-entry source XML, so the payload is a faithful JSON record of every field it
extracted. This preserves provenance without pretending to store bytes we never had.

**HTTP is confined to one boundary.** `sources/http.py` is the only module that talks to
the network, and a safety test enforces that no other module imports an HTTP client.
The URL guard rejects non-HTTP schemes and literal private/loopback addresses (including
the cloud metadata endpoint) but deliberately does not resolve DNS — `config/sources.yaml`
is operator-controlled and trusted. That assumption is documented in the module.

**A per-fetch item cap was added** (`max_items_per_fetch`, default 50). Two configured
feeds carry their entire archive — OpenAI's is over a thousand entries — and re-reading
all of it every run is neither useful nor polite.

**Migration 002 also added two source columns** (`editorial_role`, `tags_json`) rather
than hiding editorial metadata in the adapter `config` blob. Both are consumed now, by
the `sources` command and by collection reporting.

### 26.7 Phase 3 notes (normalization, dedupe, second-wave adapters)

**The SimHash design in §17 was wrong and was corrected against measurement.** The plan
specified 3-word shingles with a Hamming threshold of 3. Measured on realistic headline
pairs, that combination is unusable — a single changed word rewrites three shingles at
once, so genuine rewordings drifted to distance 18 while unrelated stories sat at 29.
Feature sets were compared directly:

| features | near-duplicates | unrelated | usable? |
|---|---|---|---|
| unigrams | 0–8 | 13–26 | weak separation |
| **unigrams + bigrams** | **0–10** | **22–32** | **shipped** |
| 3-word shingles (planned) | 0–18 | 29–34 | no |

The threshold is therefore **12**, not 3.

**That change killed the banding index.** §17 assumed candidate lookup by simhash bands.
Banding only guarantees it finds a pair when the distance is below the band count, so at
a threshold of 12 it silently misses real duplicates. The band columns were removed
before shipping rather than kept as an index that quietly loses data. Near-duplicate
candidates are bounded by the recency window plus a row cap — at MVP volumes a small
indexed range scan. Exact-match layers (URL, content fingerprint, title fingerprint)
stay fully indexed.

**A publication-date guard was added after a real false positive.** On live data the
pipeline flagged Google's May, June and July "latest AI news" round-ups as duplicates of
each other: the texts differ by one word. They are genuinely different stories. Near
duplicates must now also be contemporaneous — a recurring monthly column is not a
duplicate. Both dates must be known for the guard to apply.

**HTML sources: two shipped, two deferred.** Anthropic Newsroom and Notion Releases are
server-rendered and read cleanly. Canva Newsroom and Perplexity Changelog both return
403 to a plain request; per the "do not fight the website" rule they were deferred, not
worked around. Selectors target semantic elements and CSS-module local names, because
both shipped sites use build-hashed class names that change on every deploy.

**Hacker News is modelled as enrichment, not a source.** It uses the same adapter
contract and lands in `raw_items` for provenance, but the normalizer refuses to derive
articles from any `signal_only` source. Its records become `community_signals` rows,
matched to articles by canonical URL. Point counts are a snapshot from first
observation — ingestion identity prevents re-recording, so scores do not track upward.

**Storage codec for SimHash.** SQLite's `INTEGER` is signed 64-bit while a simhash is
unsigned, so hashes with the top bit set overflowed on insert. `storage/codecs.py`
reinterprets the bits at the boundary; Hamming distance is unaffected.

**`DuplicateCandidate` lives in the domain layer** so that storage and pipeline can both
use it without storage importing pipeline, which would break the dependency rule.

### 26.8 Phase 4 notes — the editorial layer is Claude Code, not an LLM API

**This supersedes §14 and the Phase 4 plan.** The original design put an `LLMClient`
abstraction behind an external provider, with API keys, a token budget and a cost cap.
That is no longer the architecture.

**Before:** Python calls an LLM provider to screen and score candidates.
**Now:** Python exports candidates as JSON; a Claude Code session reads them, applies the
rubric, researches where needed, and writes structured decisions back; Python validates
and imports them.

Why the change:

* no separate LLM API cost, and no API key to hold, rotate or leak
* no local model, so no multi-gigabyte weights or RAM pressure on a laptop
* no provider lock-in — nothing in the codebase depends on any vendor SDK
* the deterministic Python boundaries get *stronger*, not weaker: editorial output is
  untrusted input, validated against schema, enums, gates and database state before it
  is stored

**The replacement boundary is the JSON contract**, not a Python interface. An automated
evaluator could consume the same `EditorialBatch` and emit the same `ReviewedBatch`
without a single change to SQLite, the repositories or the pipeline. That is the whole
point of making the exchange explicit and versioned. `evaluator_type` is already stored
per evaluation so an automated evaluator's output stays distinguishable from a session's.

Consequences for the plan as written:

* `llm/` package, `LLMClient`, provider modules, `budget.py`, prompt files and the
  `llm_calls` table are **not built** and are not planned.
* The three-stage screener/editor/writer funnel collapses to one review pass; cost
  control was its main justification and there is no per-token cost.
* §14.4's prompt versioning becomes `rubric_version`, stored on every evaluation.
* The credibility gate, the weighted composite and the sensitive-category rules survive
  intact — they were always Python's job, and they still are.

**Ranking stays out of the evaluator's hands.** The reviewed document carries component
scores only; `composite_score` is computed by `editorial/rubric.py` on import. Two
evaluations with identical components rank identically whoever produced them.

**Evaluations are append-only and fingerprint-bound**, mirroring the draft-approval
philosophy: a judgement names the exact content it judged, so renormalizing an article
makes its old evaluation visibly stale rather than silently current.

### 26.9 Phase 5 notes (draft writing)

**Same exchange pattern as Phase 4, one stage later.** Assignments out, drafts back,
strictly validated, `style_version` alongside `rubric_version`. The writing layer is a
Claude Code session, not an LLM API — see §26.8.

**Migration 005 was necessary.** The Draft/DraftVersion tables from migration 001 are
not reshaped, but four things Phase 5 genuinely persists had nowhere to live: the
evaluation that authorised the draft (provenance), the post format (drives length
validation and preview), the machine-readable source URL (separate from the rendered
attribution line), and internal writer notes. A fifth column records the style version.

**Writer notes are deliberately outside the content hash.** `compute_content_hash` was
left untouched: it covers title, body, hashtags, category, audience and the rendered
source attribution — exactly what a reviewer reads. An internal note must not be able to
change what a human is approving. The source URL is inside the hash by virtue of being
part of the attribution line.

**Python assembles the post.** The writer supplies a headline, body, source label and
URL; `writing/format.py` renders the final text and Python computes the hash. Ranking
was kept out of the evaluator's hands in Phase 4 for the same reason.

**Length policy: targets warn, hard limits reject.** Being outside a format's editorial
target is reported and stored, never corrected. Only a post under 120 or over 3500
characters is refused, and it is refused rather than cropped — silently truncating
someone's prose is how a post ships missing its caveat.

**Eligibility lives in one function.** `writing/export.eligibility_problem` is consulted
by the exporter *and* the importer, so a draft cannot be smuggled in for a rejected or
held story by hand-writing the JSON. The importer passes `has_draft=False` deliberately:
an already-drafted article is an idempotent skip, not an error.

**A logging bug was found and fixed during this phase.** `extra={"created": ...}` shadows
`LogRecord.created`, which Python's logging refuses to overwrite — it raises at call
time, so the import command crashed only when actually run. The key was renamed and a
test now scans the package for any reserved-name collision.

### 26.10 One deliberate omission

`draft_versions.category` has no `CHECK` constraint, unlike `audience` and every status
column. The category vocabulary is editorial and expected to change as the channel
finds its voice; the domain enum validates it, and a migration per new category would be
friction for no safety gain. Structural values keep their constraints.
