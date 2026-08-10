"""The editorial rubric: scoring dimensions, weights, gates and ranking.

This module is the *arithmetic* half of editorial judgement. The reasoning half —
reading a story and deciding what its scores should be — happens in a Claude Code
session following ``docs/editorial_rubric.md``.

Splitting it this way matters: the evaluator supplies component scores and a decision,
but it never supplies the final ranking number. Python computes that from validated
components using weights that live here, so ranking stays deterministic, reproducible
and reviewable no matter who or what did the evaluating.
"""

from __future__ import annotations

from typing import Final

from ai_news_editor.domain.enums import EditorialDecision, TrustTier, VerificationStatus

#: Bumped whenever dimensions, weights or gates change in a way that makes old
#: evaluations non-comparable. Stored on every evaluation, so a rubric change never
#: silently reinterprets historical scores.
RUBRIC_VERSION: Final = "1"

#: Bumped when the batch/review JSON contract changes shape.
SCHEMA_VERSION: Final = "1"

#: The nine dimensions an evaluator scores, each 0–100.
DIMENSIONS: Final[tuple[str, ...]] = (
    "credibility",
    "general_ai_relevance",
    "reader_interest",
    "usefulness",
    "novelty",
    "wow_factor",
    "virality_potential",
    "accessibility",
    "consumer_impact",
)

#: Weights for the composite ranking score.
#:
#: These encode the channel's thesis, not a measured optimum: this is a
#: popular-science channel for ordinary AI users, so "would a non-technical reader
#: care, and can they do something with it?" dominates. They are a starting point to
#: calibrate against real output, not a scientific result.
#:
#: ``credibility`` and ``general_ai_relevance`` are deliberately absent — they are
#: gates (see below), not things a story can compensate for by being entertaining.
WEIGHTS: Final[dict[str, float]] = {
    "reader_interest": 0.24,
    "usefulness": 0.20,
    "consumer_impact": 0.16,
    "accessibility": 0.12,
    "novelty": 0.10,
    "wow_factor": 0.09,
    "virality_potential": 0.09,
}

#: Minimum credibility for a normal shortlist. Below this a story may still be worth
#: covering, but only after verification — hence HOLD rather than REJECT.
CREDIBILITY_SHORTLIST_THRESHOLD: Final = 70

#: Minimum AI relevance for a shortlist. A fascinating story that is not about AI does
#: not belong on an AI channel however well it scores elsewhere.
AI_RELEVANCE_SHORTLIST_THRESHOLD: Final = 50

#: Categories where an unchecked claim can harm someone — a named person accused, a
#: scam amplified, a fake presented as real. Shortlisting one of these requires the
#: verification to have actually happened.
SENSITIVE_REQUIRES_VERIFICATION: Final = True

#: Trust tiers that can serve as independent corroboration. Community chatter can point
#: at a story but never settles whether it is true.
VERIFYING_TIERS: Final[frozenset[TrustTier]] = frozenset(
    {TrustTier.OFFICIAL, TrustTier.REPUTABLE_SECONDARY}
)


def composite_score(scores: dict[str, int]) -> float:
    """Weighted ranking score in 0–100, computed only from validated components.

    The evaluator never supplies this number directly. Two evaluations with identical
    component scores always rank identically, whoever produced them.
    """
    total = sum(WEIGHTS[name] * scores[name] for name in WEIGHTS)
    return round(total, 2)


def passes_credibility_gate(scores: dict[str, int]) -> bool:
    """Whether a story is credible and relevant enough to be shortlisted at all."""
    return (
        scores["credibility"] >= CREDIBILITY_SHORTLIST_THRESHOLD
        and scores["general_ai_relevance"] >= AI_RELEVANCE_SHORTLIST_THRESHOLD
    )


def gate_failure_reason(scores: dict[str, int]) -> str | None:
    """Explain why the gate rejected a shortlist, or ``None`` if it passed."""
    if scores["credibility"] < CREDIBILITY_SHORTLIST_THRESHOLD:
        return (
            f"credibility {scores['credibility']} is below the shortlist threshold of "
            f"{CREDIBILITY_SHORTLIST_THRESHOLD}; use HOLD_FOR_VERIFICATION or REJECT"
        )
    if scores["general_ai_relevance"] < AI_RELEVANCE_SHORTLIST_THRESHOLD:
        return (
            f"general_ai_relevance {scores['general_ai_relevance']} is below the shortlist "
            f"threshold of {AI_RELEVANCE_SHORTLIST_THRESHOLD}"
        )
    return None


def verification_is_sufficient(
    decision: EditorialDecision,
    status: VerificationStatus,
    *,
    sensitive: bool,
    corroborating_sources: int,
) -> str | None:
    """Check a shortlist's verification story. Returns an error message, or ``None``.

    A sensitive story cannot be shortlisted on ``NOT_REQUIRED``: those categories are
    exactly the ones where the source is not authoritative for its own claim. And a
    story cannot be shortlisted as ``VERIFIED`` with nothing to point at — asserting
    verification without evidence is worse than admitting the evidence is thin.
    """
    if decision is not EditorialDecision.SHORTLIST:
        return None

    if status is VerificationStatus.NEEDS_MORE_EVIDENCE:
        return (
            "a story whose verification is incomplete cannot be SHORTLIST; "
            "use HOLD_FOR_VERIFICATION"
        )
    if sensitive and status is VerificationStatus.NOT_REQUIRED:
        return (
            "sensitive categories (deepfakes, scams, accusations, controversies) always "
            "require verification; NOT_REQUIRED is not acceptable here"
        )
    if status is VerificationStatus.VERIFIED and corroborating_sources == 0:
        return "verification_status is VERIFIED but no verification_sources were supplied"
    return None
