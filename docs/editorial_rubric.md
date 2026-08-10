# Editorial rubric — v1

The scoring standard for deciding which stories this channel covers. The arithmetic
(weights, gates, ranking) lives in `src/ai_news_editor/editorial/rubric.py`; this
document is the judgement half.

## Who we are writing for

A Ukrainian popular-science / consumer-technology channel about AI. Readers are ordinary
people: ChatGPT users, beginners, creatives, freelancers, office workers, students,
teachers, people who use Canva and Notion. Most have no ML background and no interest in
acquiring one.

The channel should feel interesting, useful, modern, sometimes surprising, sometimes
funny, and trustworthy. It should not feel like an arXiv digest, a benchmark newsletter
or an API changelog.

The question behind every score is: **would this matter, or be fun, to someone who uses
AI but does not build it?**

## Scoring

Nine dimensions, each **0–100**. Score the story as it is, not as it could be written up.

| Dimension | What it measures |
|---|---|
| `credibility` | How confident are we this is *true*, given the source and any corroboration? |
| `general_ai_relevance` | Is this genuinely about AI, rather than AI-adjacent noise? |
| `reader_interest` | Would a non-technical reader stop scrolling? |
| `usefulness` | Can a reader *do* something with this? |
| `novelty` | Is this actually new, or a rehash? |
| `wow_factor` | Surprise, delight, "I didn't know that was possible". |
| `virality_potential` | Is it spreading, or likely to be shared? |
| `accessibility` | Can it be explained without jargon? |
| `consumer_impact` | Does it touch ordinary life, work, study or creativity? |

**You do not set the ranking score.** Python computes the composite from your components
using fixed weights. Supply honest components; the ordering follows.

Current weights: reader_interest .24 · usefulness .20 · consumer_impact .16 ·
accessibility .12 · novelty .10 · wow_factor .09 · virality_potential .09.

`credibility` and `general_ai_relevance` are **not** weighted — they are gates. A story
cannot make up for being unreliable or off-topic by being entertaining.

### Calibration anchors

- **90–100** — genuinely excellent on this dimension. Reserve it.
- **70–89** — clearly strong.
- **40–69** — middling.
- **10–39** — weak.
- **0–9** — absent.

A useful sanity check on this channel:

| Story | reader_interest | usefulness | consumer_impact |
|---|---|---|---|
| ChatGPT ships a feature everyone can use today | ~90 | ~90 | ~90 |
| A vendor open-sources a distillation technique | ~25 | ~15 | ~15 |
| Someone's AI agent did something absurd and it went viral | ~85 | ~20 | ~35 |

Note what this does *not* say: nothing is rejected for being technical. A deeply
technical release with a real consumer consequence — a model that makes voice cloning
trivial, say — scores high on impact and belongs on the channel. Judge the consequence,
not the vocabulary.

## Decisions

Exactly three:

- **`SHORTLIST`** — worth covering. Requires `credibility ≥ 70` and
  `general_ai_relevance ≥ 50`, plus a `why_selected` list and an `editorial_angle`.
- **`HOLD_FOR_VERIFICATION`** — interesting but not sufficiently established. **Use this
  instead of rejecting a good story with thin evidence.** Rejecting loses it; holding
  keeps it for someone to check.
- **`REJECT`** — not for this channel, or not true.

## Credibility and verification

`credibility` is about the *claim*, not the prose.

**An official source is authoritative for its own affairs** — its product, its pricing,
its release, its own corporate statement. A vendor saying "our app now does X" is
usually adequate on its own; `verification_status: NOT_REQUIRED`.

**An official source is not automatically authoritative about anyone else.** A company
making claims about a competitor, a third party, fraud, wrongdoing, a public figure, or
a disputed event needs independent corroboration exactly like any other source.

Verify — actually look it up — when the story involves:

- deepfakes, scams, or misinformation
- accusations against a person or company
- viral claims
- statements about third parties
- controversial external events

Prefer, in order: **Tier 1** the primary/official source or platform statement ·
**Tier 2** major reputable journalism (Reuters, AP, BBC, established technology
publications) · **Tier 3** other useful secondary context.

Do **not** treat as verification: SEO pages, content farms, AI-generated summaries,
search-result snippets alone, or social posts on their own. Community discussion
(Hacker News) can point you at a story; it never establishes that the story is true.

`verification_status` is one of `NOT_REQUIRED`, `VERIFIED`, `NEEDS_MORE_EVIDENCE`. The
importer enforces the coherent combinations: a sensitive category cannot be shortlisted
on `NOT_REQUIRED`, `VERIFIED` needs at least one source, and `NEEDS_MORE_EVIDENCE`
cannot be `SHORTLIST` — that is what `HOLD_FOR_VERIFICATION` is for.

## Category and audience

Pick one category from the controlled vocabulary: `PRODUCT_UPDATE`, `USEFUL_TOOL`,
`WOW`, `AI_FAIL`, `DEEPFAKE_WATCH`, `SCAM_MISINFO`, `CREATIVE_AI`, `AI_FOR_WORK`,
`AI_FOR_LEARNING`, `EVERYDAY_AI`, `TRENDING`, `EXPLAINED_SIMPLY`, `SCIENCE_LITE`,
`AI_DRAMA`.

`DEEPFAKE_WATCH`, `SCAM_MISINFO` and `AI_DRAMA` are **sensitive** and carry the stricter
verification rule above.

Audience is `BEGINNER`, `GENERAL` or `TECH_CURIOUS`. Most shortlisted stories should be
BEGINNER or GENERAL. `TECH_CURIOUS` is for stories that genuinely need some background —
not a place to park anything mildly technical.

## Editorial angle

Every shortlisted story needs one sentence saying what the story *is for the reader*.
Not a headline — the angle a writer would take.

Good: *"What does this actually change for someone who uses ChatGPT every day?"* ·
*"Why this deepfake fooled so many people."* · *"Canva quietly added something that
saves designers an afternoon."*

Weak: *"OpenAI released a feature."* That is the news, not the angle.

## Why selected

Two to four short phrases: `"new user-facing capability"`, `"available today"`,
`"official announcement"`, `"high practical usefulness"`. Reasons, not sentences.
