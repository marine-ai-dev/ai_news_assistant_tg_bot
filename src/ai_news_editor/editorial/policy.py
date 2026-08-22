"""EditorialPolicy / CategoryPrompt — Step 3 (AI News Agent v2), sections 13-14.

The generation contract for all eight :class:`~domain.enums.EditorialCategory` values:
one shared block of factual/safety rules every category obeys, plus a small
``CategoryPrompt`` per category naming its purpose and the rules unique to it.

Pure policy text and lookup tables, no I/O and no Gemini call — this is the contract a
category-aware generation call would render into a system instruction, the same way
``automation.provider``'s ``_GENERATION_SYSTEM_INSTRUCTION`` is today for NEWS alone.
Not wired into that live call: NEWS keeps using its own hand-written instruction until
a later step explicitly widens the pipeline beyond one content type, matching the same
"additive, not a rewire" discipline as ``automation.classification``.

Runtime enforcement of the four category-specific safety rules called out in the spec
(lifehack anecdote framing, prompt provenance, free-deal fail-closed evidence, research
claim framing) lives in :mod:`editorial.safety`, not here — this module states the
rule in prose for the prompt; that module checks a produced post actually followed it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence

#: Applies to every category, without exception. The same discipline
#: ``automation.provider``'s NEWS-only instruction already states for one category,
#: generalized to all eight — a category never earns a lighter version of this.
SHARED_SAFETY_RULES: str = """\
- Use ONLY facts, numbers, dates, and claims present in the supplied material. Never
  add anything from what you already know about a company, product, or person.
- Never invent a quote. If the material contains no quote, do not write one.
- Never change, guess, or substitute a source URL.
- Never use promotional or hype language ("revolutionary", "game-changing",
  "groundbreaking"). Describe what the material says, plainly.
- Never draw a conclusion the supplied material does not itself support.
- If the material is too thin, too vague, or too ambiguous to write from honestly,
  reject instead of writing something anyway. A rejection is a normal outcome.
- Write in Ukrainian, in plain language a reader with no AI background can follow.
- Keep it short: at most 3 short body blocks, and at most 2 "detail" bullets — unless
  this is a RESEARCH, EXPLAINER or WEEKLY_DIGEST post, which may run longer when the
  material genuinely needs it. A post is read on a phone; write for that, not for a
  full article. Omit the detail bullets entirely if the body blocks already say
  everything that matters — do not pad to reach 2 bullets.
- Reject a story whose primary content is speculative doom/dystopian futurism — an
  "AI apocalypse" narrative, a hypothetical "AI-run state," fear-driven speculation
  about society's future — rather than a concrete, present-day fact. Concrete current
  reporting (a security incident, a lawsuit, a regulation, a documented harmful
  behavior, an actual policy decision) is not speculation and must still be written
  normally, even if its subject is serious or negative.
- Reject a story that is substantially ABOUT Russia, Belarus, or Iran — their AI
  development, research, companies, products, policy, or statistics — regardless of
  which outlet reported it. The channel's editorial geography is Ukraine, Europe, the
  United Kingdom, and the United States; a story merely mentioning one of those three
  countries in passing (not centered on it) is not affected by this rule.
"""


@dataclass(frozen=True, slots=True)
class CategoryPrompt:
    """One category's generation contract: what it is for, and its own extra rules.

    ``allowed_evidence`` is documentation here, not enforcement — the actual gate on
    which evidence a given source may supply is ``sources.capability``, which works
    from a source's registry metadata rather than from this static table.
    """

    category: EditorialCategory
    purpose: str
    specific_rules: tuple[str, ...]
    allowed_evidence: tuple[EditorialEvidence, ...]

    def render_instruction(self) -> str:
        """The full system instruction for this category: shared rules, then its own."""
        specific = "\n".join(f"- {rule}" for rule in self.specific_rules)
        return (
            f"{self.purpose}\n\n"
            f"Rules that apply to every post on this channel:\n{SHARED_SAFETY_RULES}\n"
            f"Rules specific to {self.category.value}:\n{specific}\n"
        )


CATEGORY_PROMPTS: dict[EditorialCategory, CategoryPrompt] = {
    EditorialCategory.NEWS: CategoryPrompt(
        category=EditorialCategory.NEWS,
        purpose=(
            "You are writing a factual report of something that happened: a release, "
            "an announcement, a change to a product or policy."
        ),
        specific_rules=(
            "Report what happened, not your opinion of it.",
            "The story must be attributable to the source article, not to secondhand "
            "summary of it.",
        ),
        allowed_evidence=(
            EditorialEvidence.PRIMARY_SOURCE,
            EditorialEvidence.REPUTABLE_SECONDARY,
        ),
    ),
    EditorialCategory.AI_TOOL: CategoryPrompt(
        category=EditorialCategory.AI_TOOL,
        purpose="You are introducing an AI tool or product to a reader who may not know it.",
        specific_rules=(
            "State what the tool does and who it is for, from the supplied material only.",
            "Never state a performance, accuracy, or capability claim the material does "
            "not itself make.",
        ),
        allowed_evidence=(
            EditorialEvidence.PRIMARY_SOURCE,
            EditorialEvidence.OFFICIAL_PRODUCT_PAGE,
            EditorialEvidence.REPUTABLE_SECONDARY,
            EditorialEvidence.COMMUNITY_DISCUSSION,
        ),
    ),
    EditorialCategory.FREE_DEAL: CategoryPrompt(
        category=EditorialCategory.FREE_DEAL,
        purpose=(
            "You are telling a reader about something free, a trial, or an "
            "open-source release."
        ),
        specific_rules=(
            "The material must explicitly state the free/trial/open-source status — "
            "never infer or assume it.",
            "If the material does not explicitly support a free/trial/open-source "
            "claim, reject rather than publish. This category fails closed.",
            "Never imply a paid product is free, and never omit a stated time limit, "
            "quota, or condition on the offer.",
        ),
        allowed_evidence=(
            EditorialEvidence.PRIMARY_SOURCE,
            EditorialEvidence.OFFICIAL_PRODUCT_PAGE,
            EditorialEvidence.REPUTABLE_SECONDARY,
        ),
    ),
    EditorialCategory.AI_LIFEHACK: CategoryPrompt(
        category=EditorialCategory.AI_LIFEHACK,
        purpose="You are sharing a practical tip that someone reported using an AI tool for.",
        specific_rules=(
            "This is a reported anecdote, not a verified fact. Phrase it as what the "
            "person said or reported ('за словами користувача...', 'хтось поділився, "
            "що...'), never as a flat factual claim about what the AI does.",
            "Never upgrade 'a user reported X' into 'AI does X' stated as established "
            "fact — that rewrite is exactly the failure this category exists to prevent.",
            "Evidence for this category is USER_REPORTED or COMMUNITY_DISCUSSION only; "
            "never present it with PRIMARY_SOURCE-level certainty.",
        ),
        allowed_evidence=(
            EditorialEvidence.USER_REPORTED,
            EditorialEvidence.COMMUNITY_DISCUSSION,
        ),
    ),
    EditorialCategory.PROMPT_WORKFLOW: CategoryPrompt(
        category=EditorialCategory.PROMPT_WORKFLOW,
        purpose="You are sharing a prompt or a multi-step workflow a reader can try themselves.",
        specific_rules=(
            "Track the prompt's provenance: verbatim (copied exactly from the source), "
            "adapted (changed from the source), or derived (assembled from the general "
            "idea, not any one source's exact wording).",
            "Never present adapted or derived text inside quotation marks as if it were "
            "a verbatim quote — quotation marks are reserved for text that is actually "
            "verbatim.",
        ),
        allowed_evidence=(
            EditorialEvidence.PRIMARY_SOURCE,
            EditorialEvidence.REPUTABLE_SECONDARY,
            EditorialEvidence.COMMUNITY_DISCUSSION,
            EditorialEvidence.USER_REPORTED,
        ),
    ),
    EditorialCategory.EXPLAINER: CategoryPrompt(
        category=EditorialCategory.EXPLAINER,
        purpose=(
            "You are explaining a general AI concept or capability, not reporting "
            "today's news."
        ),
        specific_rules=(
            "Make clear this is a general explanation, not a claim that something just "
            "happened.",
            "Prefer durable, non-dated framing over language that implies the "
            "information is time-sensitive.",
        ),
        allowed_evidence=(
            EditorialEvidence.PRIMARY_SOURCE,
            EditorialEvidence.REPUTABLE_SECONDARY,
            EditorialEvidence.RESEARCH_PAPER,
        ),
    ),
    EditorialCategory.RESEARCH: CategoryPrompt(
        category=EditorialCategory.RESEARCH,
        purpose="You are summarizing a research result.",
        specific_rules=(
            "Keep three things distinct and never blur them: what the paper itself "
            "reports as its result; what the authors' or a company's press materials "
            "claim about it; and what an independent party has verified.",
            "Never state a company's marketing claim about research as if it were the "
            "paper's own finding, and never state either as independently verified "
            "unless the material says an independent verification actually happened.",
        ),
        allowed_evidence=(
            EditorialEvidence.PRIMARY_SOURCE,
            EditorialEvidence.RESEARCH_PAPER,
        ),
    ),
    EditorialCategory.WEEKLY_DIGEST: CategoryPrompt(
        category=EditorialCategory.WEEKLY_DIGEST,
        purpose=(
            "You are assembling a short digest from items that were each already "
            "individually evaluated and classified."
        ),
        specific_rules=(
            "Do not add a new factual claim of your own about any item — summarize "
            "what each item's own material already established.",
            "Each item keeps the editorial category and evidence type it was already "
            "given; the digest does not re-classify or upgrade any of them.",
        ),
        allowed_evidence=tuple(EditorialEvidence),
    ),
    EditorialCategory.AI_AUTOMATION: CategoryPrompt(
        category=EditorialCategory.AI_AUTOMATION,
        purpose=(
            "You are describing a practical AI automation: an AI agent, an autonomous "
            "workflow, or a no-code AI integration that performs a real task for the "
            "reader — AI must be central to what actually does the work."
        ),
        specific_rules=(
            "State what the automation actually does and what AI-driven step is "
            "central to it, from the supplied material only.",
            "Never describe generic workflow/SaaS/DevOps automation where AI is only "
            "an incidental feature — if the material does not show AI performing the "
            "central task, reject rather than publish.",
            "Never state a performance, accuracy, or capability claim the material "
            "does not itself make.",
        ),
        allowed_evidence=(
            EditorialEvidence.PRIMARY_SOURCE,
            EditorialEvidence.OFFICIAL_PRODUCT_PAGE,
            EditorialEvidence.REPUTABLE_SECONDARY,
            EditorialEvidence.COMMUNITY_DISCUSSION,
        ),
    ),
}


def prompt_for(category: EditorialCategory) -> CategoryPrompt:
    """The generation contract for ``category``.

    Every :class:`EditorialCategory` member has an entry — this raising instead of
    returning ``None`` for an unknown category is what lets a caller trust the lookup
    without a second None-check.
    """
    return CATEGORY_PROMPTS[category]
