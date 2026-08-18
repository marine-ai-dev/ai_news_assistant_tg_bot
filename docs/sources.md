# 📡 Source registry

`config/sources.yaml` is the single source of truth for where this project reads news
from. This document explains the tier philosophy, what the v2 registry metadata means,
and how to re-verify it.

## Tiers

Three trust tiers (`trust_tier`, `TrustTier` in `domain/enums.py`), unchanged since
Phase 1 — v2 only broadens what fills them:

| Tier | `trust_tier` | Meaning |
|---|---|---|
| **A — Primary / Official** | `OFFICIAL` | The vendor's own words. The only tier the live automation pipeline draws candidates from today (`automation/pipeline.py` filters to `OFFICIAL`). |
| **B — Discovery / Journalism** | `REPUTABLE_SECONDARY` | Reputable tech press. Covers what vendors won't publish about themselves — controversies, comparisons, viral stories — and offers a second, editorially independent read on the same announcements. Not read by the automation pipeline yet. |
| **C — Community** | `COMMUNITY_SIGNAL` | Attention, never evidence. Always `signal_only: true` — the normalizer refuses to turn these into articles; they can only produce `community_signals` rows. |

**Editorial policy for a future phase** (not implemented in this step): if a Tier B
story reports an announcement that a Tier A primary source also covers, prefer the
primary source for the factual post. The registry now carries enough metadata
(`source_family`, `priority`) to support that; nothing today acts on it.

## v2 registry metadata

Added in Step 2, read by no live code path yet — this is groundwork for a future
editorial classifier, diversity pass and media policy, validated now so it's
trustworthy whenever that phase arrives:

- **`priority`** (`SourcePriority`): `PRIMARY_HIGH` / `PRIMARY_NORMAL` for Tier A,
  `DISCOVERY` for Tier B, `COMMUNITY` for Tier C. A source's tier and priority are
  validated to agree — a `COMMUNITY_SIGNAL` source can never carry a primary priority,
  and priority `COMMUNITY` is reserved for `COMMUNITY_SIGNAL` sources. This is the
  guard against Tier C accidentally outranking Tier A/B.
- **`content_types`** (`ContentCapability`, at least one): what a source may
  eventually feed into — `NEWS`, `AI_TOOL`, `FREE_DEAL`, `AI_LIFEHACK`,
  `PROMPT_WORKFLOW`, `EXPLAINER`, `RESEARCH`, `WEEKLY_DIGEST_INPUT`. A source saying
  `PROMPT_WORKFLOW` does not mean anything extracts prompts from it today.
- **`media_policy`** (`MediaPolicy`): how conservatively a source's media may be used,
  once media handling exists. Defaults to `NO_MEDIA` for every source added in this
  step — reuse permission is never inferred from "it's on an official page." See below.
- **`fulltext_policy`** (`FulltextPolicy`): `NORMAL_ATTEMPT` (default) or
  `DISCOVERY_ONLY` for signal-only sources, which never reach
  `sources.fulltext.fetch_fulltext` at all.
- **`source_family`**: groups sibling feeds from one company for a future diversity
  pass — e.g. Google AI Blog + Google DeepMind + Google Research share `"Google"`.
  Never inferred; GitHub is deliberately its own family rather than folded into
  Microsoft, since that's how a reader experiences the two products.
- **`domains`** / `canonical_domains`: the hostname(s) a source's canonical URLs use.
  Most sources leave `domains` unset and get one derived automatically from `url`,
  normalized the same way `automation.pipeline._domain_of` normalizes a candidate URL
  at runtime (strips a `www.` prefix, lowercases) — registry metadata and the runtime
  domain-cooldown fix from the previous step always agree about what "the domain" is.
- **`disabled_reason`**: required exactly when `enabled: false`, forbidden otherwise.
  A disabled entry documents itself instead of looking like an oversight.

None of this is mirrored into the persisted `sources` database table — `Source` and
`SourceRepository` are unchanged from Phase 1. This metadata lives only in
`SourceDefinition` (`sources/config.py`), read fresh from YAML by whatever later phase
needs it. That is what keeps this step migration-free.

## Media policy — why so conservative

`NO_MEDIA` is the default for every source in this registry, including official vendor
blogs. Appearing on a company's own page is not a license to re-host its images —
reuse permission has to be explicit and checkable, never assumed. `EXPLICIT_REUSE_ALLOWED`
is reserved for a source whose licensing terms this project can actually point to; none
qualify yet. Step 4 will decide, source by source, whether to use Telegram's own link
preview, a first-party (owner-generated) image, or nothing at all — this step only
prepares the field.

## Enabled sources

### Tier A — Primary / Official (15)

| Source | Family | Priority | Content |
|---|---|---|---|
| OpenAI News | OpenAI | HIGH | NEWS, AI_TOOL |
| Google — The Keyword (AI) | Google | HIGH | NEWS, AI_TOOL |
| Google DeepMind Blog | Google | HIGH | NEWS, RESEARCH |
| Google Research Blog | Google | NORMAL | RESEARCH |
| Hugging Face Blog | Hugging Face | HIGH | NEWS, AI_TOOL, RESEARCH |
| Microsoft 365 Blog | Microsoft | NORMAL | NEWS, AI_TOOL |
| Microsoft AI News | Microsoft | HIGH | NEWS |
| Microsoft Research Blog | Microsoft | NORMAL | RESEARCH |
| GitHub Changelog | GitHub | NORMAL | NEWS, AI_TOOL |
| NVIDIA Blog | NVIDIA | NORMAL | NEWS, RESEARCH |
| AWS Machine Learning Blog | Amazon | NORMAL | NEWS, AI_TOOL, RESEARCH |
| Apple Machine Learning Research | Apple | NORMAL | RESEARCH |
| Mistral AI News | Mistral AI | HIGH | NEWS, AI_TOOL |
| Anthropic News | Anthropic | HIGH | NEWS, AI_TOOL |
| Notion Releases | Notion | NORMAL | NEWS, AI_TOOL |

### Tier B — Discovery / Journalism (5)

| Source | Content |
|---|---|
| TechCrunch — AI | NEWS |
| The Verge — AI | NEWS |
| Ars Technica — AI | NEWS, EXPLAINER |
| WIRED — AI | NEWS, EXPLAINER |
| MIT Technology Review — AI | NEWS, RESEARCH, EXPLAINER |

### Tier C — Community (3)

| Source | Content |
|---|---|
| Hacker News (AI stories) | AI_LIFEHACK, PROMPT_WORKFLOW |
| Lobsters (ai tag) | AI_LIFEHACK, PROMPT_WORKFLOW |
| Product Hunt — AI | AI_TOOL, FREE_DEAL, AI_LIFEHACK |

**23 enabled**, all independently verified reachable and parseable on 2026-08-18 (see
`ai-news sources doctor` below).

## Disabled / future sources (10)

Kept in the registry rather than deleted, so the research isn't lost and a future
retry doesn't start from zero. Each carries its own `disabled_reason`; summary:

| Source | Tier | Why disabled |
|---|---|---|
| Meta AI Blog | A | No discoverable RSS/Atom; client-rendered page. |
| Cohere Blog | A | `/blog/rss.xml`, `/rss.xml`, `?format=rss` all fail — Next.js app, no real feed. |
| Stability AI News | A | `/news/rss.xml` redirects to an HTML page, not XML. |
| xAI News | A | `https://x.ai/news` returns HTTP 403 to this fetcher. |
| Perplexity Blog | A | Returns HTTP 403 to this fetcher. |
| Adobe Firefly Blog | A | No discoverable RSS at any attempted path. |
| VentureBeat — AI | B | Feed URLs return a bot-detection challenge page, not XML. |
| Reuters — Technology / AI | B | HTTP 401 — Reuters retired its free public feeds. |
| Reddit (r/artificial etc.) | C | HTTP 429 rate-limiting, reproduced on repeated retries. |
| X / Threads | C | No free, lawful, stable access without authentication. |

None of these were worked around with scraping, anti-bot circumvention or browser
impersonation — see each entry's `disabled_reason` in `config/sources.yaml` for the
exact endpoints tried.

## Verifying the registry: `ai-news sources doctor`

```bash
ai-news sources            # list every configured source (enabled and disabled)
ai-news sources doctor     # smoke-test every ENABLED source's discovery endpoint
ai-news sources doctor --include-disabled   # also re-check disabled sources
```

`doctor` calls the exact adapter `collect` would use, once per source, and reports
whether the endpoint resolved and at least one item parsed. It writes nothing to the
database and makes no Gemini or Telegram call. Exit code is non-zero if any *enabled*
source fails — CI-friendly, and safe to run as often as you like.
