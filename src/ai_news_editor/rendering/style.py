"""Centralized editorial visual identity — Step 5 (AI News Agent v2).

One canonical emoji per :class:`~domain.enums.EditorialCategory`, one set of semantic
body-block emoji, and the size targets every category renderer is checked against.
Nothing about *how a post looks* is decided anywhere else — `rendering/render.py`
reads these tables, it never invents an emoji or a target inline.

Internal editorial identity (not published verbatim anywhere, just the direction every
choice below serves): AI-новини без шуму — що сталося, навіщо це знати, і як це можна
використати. Source-first, practical, concise, clear, low-hype, human, visually
scannable. This document's judgment calls, and every category template in
`render.py`, exist to serve exactly those seven words — see docs/style.md for the full
articulation and worked examples.

This is deliberately an *original* style, not a copy of any existing Telegram AI-news
channel. Nothing here encodes a competitor's name, wording, or branded rubric name —
see docs/style.md for that boundary; it applies to prompts even more than to this file.
"""

from __future__ import annotations

from ai_news_editor.domain.enums import EditorialCategory

#: One stable emoji per category, used at the start of the bold headline line. Chosen
#: once and never varied per-post — a reader should recognize the category at a glance
#: before reading a single word.
CATEGORY_EMOJI: dict[EditorialCategory, str] = {
    EditorialCategory.NEWS: "🚀",
    EditorialCategory.AI_TOOL: "🛠",
    EditorialCategory.FREE_DEAL: "🎁",
    EditorialCategory.AI_LIFEHACK: "💡",
    EditorialCategory.PROMPT_WORKFLOW: "🧪",
    EditorialCategory.EXPLAINER: "🧠",
    EditorialCategory.RESEARCH: "🔬",
    EditorialCategory.WEEKLY_DIGEST: "📚",
}

#: Semantic emoji for the recurring paragraph *purposes* every category's body draws
#: from — "what happened", "why it matters", and so on. A category's own template in
#: render.py picks which of these it uses and in what order; this table is only the
#: one place each purpose's symbol is decided, so two categories never draw the same
#: idea with two different emoji.
BLOCK_EMOJI: dict[str, str] = {
    "what_happened": "✨",
    "why_it_matters": "💡",
    "availability": "🌍",
    "who_its_for": "🎯",
    "what_you_can_do": "🛠",
    "conditions": "⏳",
    "who_needs_it": "💡",
    "anecdote": "👤",
    "workflow": "🛠",
    "anecdote_result": "✨",
    "caveat": "⚠️",
    "task": "🎯",
    "steps": "🧩",
    "prompt": "📝",
    "what_it_is": "🔍",
    "why_useful": "💡",
    "where_used": "🛠",
    "what_was_tested": "🧪",
    "what_was_found": "📊",
    "limitation": "⚠️",
    "why_interesting": "💡",
}

#: The "🔆 Детальніше" bullet block — used only when a category's content actually has
#: 2-4 concrete specifics to list (see render.py's per-category logic), never
#: mechanically on every post.
DETAIL_HEADER = "🔆 Детальніше:"

#: Editorial-attribution formats for the closing source line — see docs/style.md.
#: Exactly one is used consistently; render.py never mixes them.
SOURCE_LINE_LABEL = "Джерело"

# --- Size targets --------------------------------------------------------------------

#: An ordinary NEWS/AI_TOOL/FREE_DEAL/AI_LIFEHACK/PROMPT_WORKFLOW post's rendered body
#: (excluding headline and source line) should usually land in this range. Not a hard
#: cap — see WIDE_CATEGORIES below and render.py's own "never cut facts to hit a
#: number" rule — but a post far outside it is worth a human's attention. Tightened
#: after the first real test-channel soak (Step 6B): real posts read as too long at
#: the old 500-900 target, and the fix is a smaller generation contract (fewer, shorter
#: fields), never a renderer-side truncation of what Gemini already wrote.
TARGET_BODY_CHARS_MIN = 400
TARGET_BODY_CHARS_MAX = 750

#: Step 6B: how many semantic body blocks an ordinary (non-wide) category's generation
#: contract asks for, and how many "🔆 Детальніше" bullets it may add on top. A category
#: prompt that asks for more than this produces exactly the "4 paragraphs + 4 bullets"
#: wall of text real human review flagged — capped once, here, rather than in every
#: category's own prompt text.
ORDINARY_MAX_BODY_BLOCKS = 3
ORDINARY_MAX_DETAIL_BULLETS = 2

#: RESEARCH and EXPLAINER routinely need more room to state a finding and its caveats
#: honestly; WEEKLY_DIGEST is multiple items in one post. These categories are exempt
#: from the "too long" warning that applies to the rest.
WIDE_CATEGORIES = frozenset(
    {EditorialCategory.RESEARCH, EditorialCategory.EXPLAINER, EditorialCategory.WEEKLY_DIGEST}
)

#: Beyond this, even a wide category is flagged — not rejected, just surfaced, since a
#: renderer that silently produces a wall of text defeats "visually scannable."
ABSOLUTE_BODY_CHARS_WARNING = 2200

#: Corporate hype phrases the shared safety rules (`editorial.policy.SHARED_SAFETY_RULES`)
#: already forbid Gemini from using — kept here too as the renderer's own last-resort
#: check, since the renderer must not depend on generation having obeyed the prompt.
HYPE_PHRASES: tuple[str, ...] = (
    "революційний",
    "змінить світ",
    "неймовірний прорив",
    "game-changing",
    "проривна технологія",
    "безпрецедентний",
)


def category_emoji(category: EditorialCategory) -> str:
    """The one canonical emoji for ``category``. Every category has an entry."""
    return CATEGORY_EMOJI[category]


def block_emoji(purpose: str) -> str:
    """The emoji for one semantic paragraph purpose.

    Raises:
        KeyError: ``purpose`` is not a known block purpose — a bug in the calling
            template, not something to silently default away.
    """
    return BLOCK_EMOJI[purpose]
