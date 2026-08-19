# 🗂️ Editorial categories and content diversity

Step 3 (AI News Agent v2) adds an eight-category editorial taxonomy, an evidence-strength
axis, source-capability enforcement, and a small diversity ranking pass on top of the
Step 2 source registry. This document explains the philosophy and where each piece
lives. Everything below is classification, policy and validation — the live automation
pipeline still writes NEWS only; nothing here changes what it does today (see
[Production status](#production-status)).

## Identity

`@learn_ai_easy` is a Ukrainian-language channel that explains AI to people who don't
build it. The channel may be inspired by the concise, practical pacing of successful AI
news channels — short posts, one idea per post, plain language — but every template,
sentence and piece of wording here is original. Nothing is copied from another
channel's posts, formats, or branding, and no source's own marketing language is
reproduced as if it were this channel's voice (see `SHARED_SAFETY_RULES` in
`editorial/policy.py`, which forbids hype language on every category).

## The eight content types

`EditorialCategory` (`domain/enums.py`) is a post-level classification, distinct from
three other category-like taxonomies already in this codebase: `ContentType` (Phase 1
format — NEWS/PROMPT/EXPLAINER/TESTED_USE_CASE/RESOURCE, DB-constrained on
`drafts.content_type`), `Category` (Phase 4 editorial tone — PRODUCT_UPDATE/WOW/etc.,
unconstrained), and `ContentCapability` (Step 2 source-level metadata — what a source
*may* feed into). `EditorialCategory` is what one specific post actually *is*.

| Category | Purpose | Evidence it typically carries |
|---|---|---|
| `NEWS` | A factual report of something that happened. | `PRIMARY_SOURCE`, `REPUTABLE_SECONDARY` |
| `AI_TOOL` | Introducing a tool or product to a reader who may not know it. | `PRIMARY_SOURCE`, `OFFICIAL_PRODUCT_PAGE`, `REPUTABLE_SECONDARY`, `COMMUNITY_DISCUSSION` |
| `FREE_DEAL` | Something free, a trial, or an open-source release. Fails closed — see below. | `PRIMARY_SOURCE`, `OFFICIAL_PRODUCT_PAGE`, `REPUTABLE_SECONDARY` |
| `AI_LIFEHACK` | A practical tip someone reported using an AI tool for. Anecdote, not fact — see below. | `USER_REPORTED`, `COMMUNITY_DISCUSSION` only |
| `PROMPT_WORKFLOW` | A prompt or workflow a reader can try. Tracks provenance — see below. | `PRIMARY_SOURCE`, `REPUTABLE_SECONDARY`, `COMMUNITY_DISCUSSION`, `USER_REPORTED` |
| `EXPLAINER` | A general concept, not tied to one dated event. | `PRIMARY_SOURCE`, `REPUTABLE_SECONDARY`, `RESEARCH_PAPER` |
| `RESEARCH` | A research result. Distinguishes claim strength — see below. | `PRIMARY_SOURCE`, `RESEARCH_PAPER` |
| `WEEKLY_DIGEST` | An aggregation of items already individually vetted. Never re-classifies them. | any (each item keeps its own) |

Every category's full generation contract — purpose, and the rules unique to it, on
top of the rules every category shares — lives in `editorial/policy.py`
(`CATEGORY_PROMPTS`). The shared rules (`SHARED_SAFETY_RULES`) are the same discipline
`automation.provider`'s NEWS-only prompt already enforces today, generalized to all
eight: use only the supplied material, never invent a fact or a quote, never change a
URL, no hype language, reject rather than fabricate when the material is too thin.

## Evidence strength

`EditorialEvidence` (`domain/enums.py`) answers a different question than the category
does: not *what kind of post is this*, but *how strong is what backs it*.

- `PRIMARY_SOURCE` — the vendor's own words.
- `OFFICIAL_PRODUCT_PAGE` — the product's own page, not necessarily an announcement.
- `REPUTABLE_SECONDARY` — established tech press, editorially independent of the
  vendor.
- `RESEARCH_PAPER` — an actual paper, distinct from a company's press release about it.
- `USER_REPORTED` — one person's account. Never upgraded to a fact — see AI_LIFEHACK
  below.
- `COMMUNITY_DISCUSSION` — a forum/thread's aggregate reaction, not any one person's
  claim.

This is a fourth taxonomy alongside the pre-existing `EvidenceKind` (Phase 8.1,
PROMPT-testing provenance — OFFICIAL_TEST/THIRD_PARTY_DEMO/COMMUNITY_TESTED/
OWNER_TESTED). The two answer different questions and are never interchangeable.

## Source-tier rules: a Tier C source cannot be a primary factual source

`sources/capability.py` enforces two independent checks before any classification is
trusted, both purely from a candidate's own already-loaded source metadata — no web
search, no Gemini call:

1. **Does this source's registry entry even declare this content capability?**
   (`SourceDefinition.content_types`, from Step 2's registry.)
2. **What evidence strength can this source's trust tier legitimately vouch for?**
   `TrustTier.OFFICIAL` may supply `PRIMARY_SOURCE`/`OFFICIAL_PRODUCT_PAGE`/
   `RESEARCH_PAPER`. `TrustTier.REPUTABLE_SECONDARY` may only supply
   `REPUTABLE_SECONDARY`. `TrustTier.COMMUNITY_SIGNAL` — Hacker News, Reddit, Product
   Hunt discussion — may only supply `USER_REPORTED` or `COMMUNITY_DISCUSSION`, **never**
   `PRIMARY_SOURCE` or `RESEARCH_PAPER`, no matter how the candidate's own content
   reads. This is the rule the whole module exists for.

Both checks must pass; a source with the right capability but the wrong trust tier is
still rejected, and vice versa.

## AI_LIFEHACK never upgrades an anecdote into a fact

The one failure mode this category exists to prevent: "a user reported that doing X
worked" quietly becoming "AI does X," stated as an established capability. Two
independent guards, both in `editorial/safety.py`'s `validate_lifehack`:

- **Evidence**: only `USER_REPORTED` or `COMMUNITY_DISCUSSION` is accepted — enforced
  again here even though `sources.capability` already gates it at the source level,
  because a post's own claim can still overstate weaker evidence in prose even when the
  source tier was correct.
- **Framing**: the post must present the claim as somebody's report ("за словами
  користувача...", "хтось поділився, що...") — never as a flat factual sentence about
  what the AI does.

## PROMPT_WORKFLOW tracks provenance

`PromptOrigin` (`domain/enums.py`) states how a shown prompt relates to what the source
actually published:

- `SOURCE_VERBATIM` — the source published this exact text. May be shown in quotes.
- `SOURCE_ADAPTED` — reworded from the source for readability. Must **never** be shown
  in quotes.
- `WORKFLOW_DERIVED` — the source describes a workflow with no literal prompt text;
  what's shown is assembled from that description, not lifted from it. Must never be
  shown in quotes either.

`editorial/safety.py`'s `validate_prompt_provenance` enforces this: quotation marks are
reserved for text that is actually verbatim, because a reader who copies a "quoted"
prompt is trusting that it is exactly what the source published.

## FREE_DEAL fails closed

No explicit source evidence of free/trial/open-source status means **reject**, not
"probably fine." `editorial/safety.py`'s `validate_free_deal` treats the absence of
evidence the same as evidence against — never as a reason to assume the best case. A
paid product is never implied to be free, and a stated time limit, quota, or condition
on an offer is never silently dropped.

## RESEARCH keeps three framings distinct

A summary of a research result can conflate three different levels of certainty
without meaning to:

1. What the **paper itself** reports as its result.
2. What a **company's** press materials claim about it.
3. What an **independent** party has actually verified.

`editorial/safety.py`'s `ResearchClaimFraming` and `validate_research_claim` keep these
apart: a sentence may only be framed as independently verified when the supplied
material itself states that verification happened — never inferred from the paper's
existence or the company's confidence.

## Diversity: a soft preference, never a hard rotation

`editorial/diversity.py` is a small, transparent, centrally-configured scoring
adjustment — not an opaque recommendation engine, and not a rigid rotation schedule. A
candidate that unanimously repeats the last few posts' category *or* source family
(default lookback: 3 posts, and the match must be unanimous across the whole window —
a single differing post clears the penalty) gets a small negative adjustment. It never
excludes a candidate; it only reorders it against its peers. Every weight lives in one
`DiversityWeights` dataclass, and the rule that matters most: **safety and relevance
always beat diversity** — the one strong, trustworthy candidate of the day still wins
even if it repeats yesterday's category or source, because nothing here can remove a
candidate from consideration.

This is deliberately kept structurally and conceptually separate from
`automation.pipeline`'s domain-cooldown (an HTTP-reliability mechanism from Step 2 that
excludes an unreliable domain outright for the rest of one run). Diversity never
excludes; cooldown never reorders. `editorial/diversity.py` does not import
`automation.pipeline` and never will.

`planning/recent_history.py` reads the real recent-publication history (`Publication`
→ `Draft` → `Article` → source registry, plus the authorizing `Evaluation`'s
`editorial_category`) that feeds `diversity.rank()`'s `recent` parameter in practice.
It lives in `planning/`, not `editorial/`, because the `editorial` package is forbidden
by a safety test from importing `DraftRepository` at all — `planning` is the existing
"reads but never writes" layer for exactly this kind of glue.

## Primary-source preference

`editorial/primary_source.py` prefers the highest-trust version of a story when two
already-collected candidates report the same thing. It groups candidates using the
`possible_duplicate_of_id` link normalization already records (a conservative
cross-source match, not a new matching mechanism) and picks the highest-`TrustTier`
member of each group — Tier A over an equivalent Tier B report of the same story. The
lower-tier candidate is never discarded, only deprioritized, matching diversity's own
"reorder, never remove" discipline.

## Dedup stays URL-based

`editorial/dedup.py` adds one narrow check on top of the existing URL-based dedup
(`canonical_url`/`content_hash`/`simhash` at the `Article` layer, untouched by this
module): a single article's evaluation history must not disagree with itself — NEWS
today, `AI_TOOL` tomorrow, for the very same already-deduplicated story. A first-ever
classification never conflicts, and repeating the same category across re-evaluations
is always fine.

## The `editorial preview` command

`ai-news editorial preview` (`cli/editorial.py`) is the deterministic, offline explain
surface for all of the above: no Gemini call, no Telegram call. It reads the shortlist,
the source registry, and recent publication history, then shows — for each shortlisted
candidate — its source family and trust tier, its chosen content type and evidence,
whether the source's registry metadata actually supports that pairing, and its
diversity-adjusted rank, so an editor can see *why* the ranking looks the way it does
before anything is generated.

## Production status

As of Step 3, the live automation pipeline (`automation/pipeline.py`) is unchanged: it
still selects and writes `NEWS` only, from `TrustTier.OFFICIAL` sources, via
`automation.provider.select_candidate`/`generate_post`. Everything in this document —
the classification schema (`automation/classification.py`), the capability and safety
validators, the diversity ranking, primary-source preference, and the preview command —
was additive and unwired as of Step 3.

**Step 5 wires this taxonomy into a new, parallel pipeline** — `automation/pipeline_v2.py`
— that actually uses `sources.capability`/`editorial.diversity`/`editorial.primary_source`
to select a candidate, then `automation/classification.py` (skipped when a candidate is
already classified) and `automation/generation_v2.py` to write it, bounded to at most 2
Gemini calls per successful post. This is still **not** `automation.pipeline`'s live
entrypoint: `pipeline_v2.run_pipeline_v2` is a new, additive orchestration function, not
yet the unattended cron path. See `docs/style.md` for how its output is rendered, and
`docs/media.md` for how media is attached.
