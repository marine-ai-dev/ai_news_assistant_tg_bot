"""Source capability enforcement — Step 3 (AI News Agent v2).

Two questions a candidate's source must answer before an editorial classification is
trusted, both purely local — no web search, no Gemini call, just the candidate set
already collected and its source's own registry metadata (Step 2):

1. Can this source's items even become this content type? (``SourceDefinition.content_types``)
2. What evidence strength can this source legitimately vouch for? (its ``trust_tier``)

The rule this module exists to enforce: a Tier C community source can supply
``USER_REPORTED`` or ``COMMUNITY_DISCUSSION`` evidence, never ``PRIMARY_SOURCE`` or
``RESEARCH_PAPER`` — Hacker News cannot pretend to be primary factual NEWS just
because a candidate happened to come from it.
"""

from __future__ import annotations

from ai_news_editor.domain.enums import (
    ContentCapability,
    EditorialCategory,
    EditorialEvidence,
    TrustTier,
)
from ai_news_editor.sources.config import SourceDefinition

#: EditorialCategory and ContentCapability share a vocabulary deliberately (see
#: EditorialCategory's own docstring) — WEEKLY_DIGEST is the one exception, since no
#: source directly produces a digest; it aggregates already-qualified items, so it maps
#: to the source capability that means "may feed a digest" instead.
_CATEGORY_TO_CAPABILITY: dict[EditorialCategory, ContentCapability] = {
    EditorialCategory.NEWS: ContentCapability.NEWS,
    EditorialCategory.AI_TOOL: ContentCapability.AI_TOOL,
    EditorialCategory.FREE_DEAL: ContentCapability.FREE_DEAL,
    EditorialCategory.AI_LIFEHACK: ContentCapability.AI_LIFEHACK,
    EditorialCategory.PROMPT_WORKFLOW: ContentCapability.PROMPT_WORKFLOW,
    EditorialCategory.EXPLAINER: ContentCapability.EXPLAINER,
    EditorialCategory.RESEARCH: ContentCapability.RESEARCH,
    EditorialCategory.WEEKLY_DIGEST: ContentCapability.WEEKLY_DIGEST_INPUT,
}

#: What evidence strength a source's trust tier can legitimately vouch for. The one
#: rule this whole module exists for: TrustTier.COMMUNITY_SIGNAL never maps to
#: PRIMARY_SOURCE or RESEARCH_PAPER, no matter what a candidate's content claims.
_EVIDENCE_BY_TIER: dict[TrustTier, tuple[EditorialEvidence, ...]] = {
    TrustTier.OFFICIAL: (
        EditorialEvidence.PRIMARY_SOURCE,
        EditorialEvidence.OFFICIAL_PRODUCT_PAGE,
        EditorialEvidence.RESEARCH_PAPER,
    ),
    TrustTier.REPUTABLE_SECONDARY: (EditorialEvidence.REPUTABLE_SECONDARY,),
    TrustTier.COMMUNITY_SIGNAL: (
        EditorialEvidence.USER_REPORTED,
        EditorialEvidence.COMMUNITY_DISCUSSION,
    ),
    TrustTier.UNVERIFIED: (),
}


class CapabilityError(ValueError):
    """A candidate's requested classification is not permitted by its source."""


def allows_category(source: SourceDefinition, category: EditorialCategory) -> bool:
    """Whether ``source``'s declared ``content_types`` permit ``category`` at all."""
    return _CATEGORY_TO_CAPABILITY[category] in source.content_types


def allowed_evidence(source: SourceDefinition) -> tuple[EditorialEvidence, ...]:
    """The evidence types ``source``'s trust tier can legitimately supply."""
    return _EVIDENCE_BY_TIER.get(source.trust_tier, ())


def is_evidence_allowed(source: SourceDefinition, evidence: EditorialEvidence) -> bool:
    return evidence in allowed_evidence(source)


def validate_classification(
    source: SourceDefinition, *, category: EditorialCategory, evidence: EditorialEvidence
) -> None:
    """Raises :class:`CapabilityError` if ``source`` may not produce this pairing.

    Both checks are independent and both must pass: a source with the right content
    capability but the wrong trust tier is still rejected, and vice versa.
    """
    if not allows_category(source, category):
        raise CapabilityError(
            f"{source.id!r} does not declare a {category.value} content capability "
            f"(has: {', '.join(c.value for c in source.content_types)})"
        )
    if not is_evidence_allowed(source, evidence):
        raise CapabilityError(
            f"{source.id!r} (trust_tier={source.trust_tier.value}) cannot supply "
            f"{evidence.value} evidence "
            f"(allowed: {', '.join(e.value for e in allowed_evidence(source)) or 'none'})"
        )
