"""Category-specific safety validation — Step 3 (AI News Agent v2), sections 15-18.

Checks a produced post's *structured* claims against the safety rule its category
carries in :mod:`editorial.policy`, before publication. Structural, not textual: this
project already prefers checking what a model was explicitly asked to state
(``factual_claims``, ``confidence``, ``source_url`` — see ``automation.schema``'s own
docstring on ``GeneratedPost``) over parsing prose for banned words. These four
validators extend that discipline to the four category-specific rules the spec calls
out by name:

* AI_LIFEHACK must never upgrade "a user reported X" into "AI does X" stated as fact.
* PROMPT_WORKFLOW must never present adapted/derived prompt text as a verbatim quote.
* FREE_DEAL must fail closed without explicit source evidence of free/trial/open-source
  status.
* RESEARCH must distinguish a paper's own result from a company's marketing claim from
  independent verification.

None of this is wired into ``automation.provider`` yet — same additive discipline as
``editorial.policy`` and ``automation.classification``. A caller (a future generation
step, or the preview tool) constructs the small claim object below from what a
generated post actually says and passes it through the matching validator.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ai_news_editor.domain.enums import EditorialEvidence, PromptOrigin


class EditorialSafetyError(ValueError):
    """A produced post violates the safety rule its category carries."""


# --- AI_LIFEHACK: never upgrade an anecdote into a verified fact --------------------


@dataclass(frozen=True, slots=True)
class LifehackClaim:
    """What a produced AI_LIFEHACK post is prepared to say."""

    evidence: EditorialEvidence
    #: True when the post frames this as somebody's report ("a user said/found...");
    #: false when it states the claim as an established fact about the AI itself.
    framed_as_report: bool


_LIFEHACK_ALLOWED_EVIDENCE = (
    EditorialEvidence.USER_REPORTED,
    EditorialEvidence.COMMUNITY_DISCUSSION,
)


def validate_lifehack(claim: LifehackClaim) -> None:
    """Raises :class:`EditorialSafetyError` if ``claim`` overstates its own evidence."""
    if claim.evidence not in _LIFEHACK_ALLOWED_EVIDENCE:
        raise EditorialSafetyError(
            f"AI_LIFEHACK requires USER_REPORTED or COMMUNITY_DISCUSSION evidence, "
            f"got {claim.evidence.value}"
        )
    if not claim.framed_as_report:
        raise EditorialSafetyError(
            "AI_LIFEHACK must frame its claim as a user report, never as an "
            "established fact about the AI"
        )


# --- PROMPT_WORKFLOW: never quote non-verbatim text --------------------------------


@dataclass(frozen=True, slots=True)
class PromptClaim:
    """What a produced PROMPT_WORKFLOW post is prepared to say about its prompt text."""

    origin: PromptOrigin
    presented_as_verbatim_quote: bool


def validate_prompt_provenance(claim: PromptClaim) -> None:
    """Raises :class:`EditorialSafetyError` if non-verbatim text is quoted as verbatim."""
    if claim.presented_as_verbatim_quote and claim.origin is not PromptOrigin.SOURCE_VERBATIM:
        raise EditorialSafetyError(
            f"prompt text with origin {claim.origin.value} may not be presented as a "
            f"verbatim quote — only SOURCE_VERBATIM text may be"
        )


# --- FREE_DEAL: fail closed without explicit evidence -------------------------------


@dataclass(frozen=True, slots=True)
class FreeDealClaim:
    """Whether a produced FREE_DEAL post's material actually supports the claim."""

    has_explicit_free_evidence: bool


def validate_free_deal(claim: FreeDealClaim) -> None:
    """Raises :class:`EditorialSafetyError` when no explicit evidence backs the claim.

    Fails closed by design: the absence of evidence is treated the same as evidence
    against, never as a reason to assume the best case.
    """
    if not claim.has_explicit_free_evidence:
        raise EditorialSafetyError(
            "FREE_DEAL requires explicit source evidence of free/trial/open-source "
            "status; failing closed without it"
        )


# --- RESEARCH: keep paper result, company claim, and verification distinct ----------


class ResearchClaimFraming(StrEnum):
    """How a produced RESEARCH post frames one claim it makes.

    Not a source-evidence tier (that is ``EditorialEvidence``) — this is about what the
    *post's own sentence* asserts happened, which can differ from how strong the
    underlying source is. A post may cite a RESEARCH_PAPER source yet still frame a
    specific sentence as the company's claim about that paper, not the paper's own
    finding — this enum is what keeps those two sentences distinguishable.
    """

    PAPER_RESULT = "PAPER_RESULT"
    COMPANY_CLAIM = "COMPANY_CLAIM"
    INDEPENDENT_VERIFICATION = "INDEPENDENT_VERIFICATION"


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    """One claim inside a produced RESEARCH post, and how it is framed."""

    framing: ResearchClaimFraming
    #: True only when the supplied material itself states that an independent party
    #: verified this — never inferred from the claim's framing alone.
    independently_verified: bool


def validate_research_claim(claim: ResearchClaim) -> None:
    """Raises :class:`EditorialSafetyError` if a claim overstates its own verification."""
    if (
        claim.framing is ResearchClaimFraming.INDEPENDENT_VERIFICATION
        and not claim.independently_verified
    ):
        raise EditorialSafetyError(
            "a claim may only be framed as independently verified when the material "
            "itself states an independent verification actually happened"
        )
