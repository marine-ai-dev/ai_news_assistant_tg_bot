"""Editorial eligibility — the AI-first relevance gate + hard content filters.

Priority step, AI News Agent v3: the single place that decides whether a classified
candidate is eligible for autonomous publication, given Gemini's structured answers to
the AI-first-relevance question and each hard-filter question. Every caller
(``automation.classification``, and through it ``automation.pipeline_v2`` — the
scheduled production path, see ``automation.pipeline_v2_live``) delegates to this one
function rather than re-implementing its own slice of the decision — the point of "one
clear gate," not several scattered checks across modules.

The core product rule (see docs/v3.md § editorial policy for the full write-up):

    If removing the AI component would leave essentially the same news story, reject
    it. AI must be the primary subject of the story, not a secondary angle, keyword,
    quote, or incidental mention.

This is enforced here as code, not left to prompt wording alone: ``is_ai_primary`` is
a boolean Gemini answers, but whether that boolean (and each hard-filter boolean)
actually blocks publication is this module's deterministic decision — covered by tests
that construct flag combinations directly and never touch Gemini at all.

Fail-closed by construction: every parameter defaults to the *rejecting* value except
``is_ai_primary``, which has no safe default and must be answered explicitly by every
caller — an omitted or unresolved "is AI actually primary here" is exactly the
low-confidence case section 5 requires this to reject rather than guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

#: Machine-readable rejection codes. Stable strings — safe to alert/dashboard on;
#: never change one without checking who else reads it (see docs/v3.md § logging).
REJECT_NOT_AI_FIRST = "REJECT_NOT_AI_FIRST"
REJECT_POLITICS = "REJECT_POLITICS"
REJECT_WAR = "REJECT_WAR"
REJECT_MILTECH = "REJECT_MILTECH"
REJECT_DEFTECH = "REJECT_DEFTECH"
REJECT_CYBERSECURITY = "REJECT_CYBERSECURITY"
REJECT_GENERIC_GOVERNMENT = "REJECT_GENERIC_GOVERNMENT"
REJECT_GENERIC_DEVTECH = "REJECT_GENERIC_DEVTECH"
REJECT_SPECULATIVE_DOOM = "REJECT_SPECULATIVE_DOOM"
REJECT_FORBIDDEN_GEOGRAPHY = "REJECT_FORBIDDEN_GEOGRAPHY"

#: Human-readable text for each code — exact wording for the two pre-existing filters
#: (SPECULATIVE_DOOM, FORBIDDEN_GEOGRAPHY) is preserved verbatim from the classifier
#: module this replaces the inline checks in, so every message a reviewer or an
#: existing log consumer already recognizes keeps reading the same.
_REASON_TEXT: dict[str, str] = {
    REJECT_SPECULATIVE_DOOM: (
        "speculative dystopian/doom-futurism narrative, not concrete reporting"
    ),
    REJECT_FORBIDDEN_GEOGRAPHY: (
        "story is substantially about Russia, Belarus, or Iran — outside the "
        "channel's Ukraine/Europe/UK/US editorial geography"
    ),
    REJECT_POLITICS: (
        "primarily a political story (elections, party politics, political disputes "
        "or statements, geopolitical disputes)"
    ),
    REJECT_WAR: "primarily about war, armed conflict, combat operations, or military operations",
    REJECT_MILTECH: (
        "primarily about military AI, battlefield AI, military drones, targeting "
        "systems, autonomous weapons, military robotics, or military surveillance"
    ),
    REJECT_DEFTECH: (
        "primarily about defence-industry/defence-technology — a startup or 'AI "
        "company' framing does not exempt it"
    ),
    REJECT_CYBERSECURITY: (
        "primarily a cybersecurity incident (hack, breach, cyberattack, vulnerability, "
        "malware, ransomware, phishing, threat actor) rather than an AI capability or "
        "product in its own right"
    ),
    REJECT_GENERIC_GOVERNMENT: (
        "generic government/state news where AI is not clearly the main practical subject"
    ),
    REJECT_GENERIC_DEVTECH: (
        "ordinary developer tooling/cloud/infrastructure/SaaS news where AI is only "
        "an incidental feature, not the material subject"
    ),
    REJECT_NOT_AI_FIRST: (
        "AI is not a primary subject of this story — removing the AI component would "
        "leave essentially the same news"
    ),
}


@dataclass(frozen=True, slots=True)
class EditorialEligibilityResult:
    """One deterministic eligibility decision for one classified candidate.

    ``eligible`` is exactly ``not rejection_codes`` — never computed any other way, so
    the two can never silently disagree. ``rejection_codes``/``rejection_reasons`` list
    *every* hard filter that fired, not just the first, so a reviewer or a log line
    sees the whole picture rather than one arbitrary reason among several.
    """

    is_ai_primary: bool
    is_political: bool
    is_war_or_conflict: bool
    is_miltech: bool
    is_deftech: bool
    is_cybersecurity: bool
    is_generic_government_news: bool
    is_generic_devtech: bool
    is_speculative_doom: bool
    is_about_forbidden_geography: bool
    eligible: bool
    rejection_codes: tuple[str, ...]
    rejection_reasons: tuple[str, ...]


def evaluate_eligibility(
    *,
    is_ai_primary: bool,
    is_political: bool = False,
    is_war_or_conflict: bool = False,
    is_miltech: bool = False,
    is_deftech: bool = False,
    is_cybersecurity: bool = False,
    is_generic_government_news: bool = False,
    is_generic_devtech: bool = False,
    is_speculative_doom: bool = False,
    is_about_forbidden_geography: bool = False,
) -> EditorialEligibilityResult:
    """Decide eligibility from a fully-answered set of classifier flags.

    Deterministic, pure, and total — every combination of booleans produces a result,
    never an exception. No network I/O, no database access; this function's only
    inputs are the booleans given to it. A candidate whose flags genuinely could not
    be determined at all is a job for the *caller* to fail closed on before ever
    reaching this function (see ``automation.classification.classify_candidate``,
    which never calls this without a fully-parsed, schema-validated response) — this
    function itself has no "unknown" state to guess through.
    """
    codes: list[str] = []
    # Existing doom/geography order preserved first (this is what their inline checks
    # in automation.classification looked like before this module existed), then the
    # new hard filters in the order the v3 priority spec lists them, then the
    # AI-first check last. Order only affects which reason a human reads first in the
    # joined string — never the eligible/ineligible outcome itself.
    if is_speculative_doom:
        codes.append(REJECT_SPECULATIVE_DOOM)
    if is_about_forbidden_geography:
        codes.append(REJECT_FORBIDDEN_GEOGRAPHY)
    if is_political:
        codes.append(REJECT_POLITICS)
    if is_war_or_conflict:
        codes.append(REJECT_WAR)
    if is_miltech:
        codes.append(REJECT_MILTECH)
    if is_deftech:
        codes.append(REJECT_DEFTECH)
    if is_cybersecurity:
        codes.append(REJECT_CYBERSECURITY)
    if is_generic_government_news:
        codes.append(REJECT_GENERIC_GOVERNMENT)
    if is_generic_devtech:
        codes.append(REJECT_GENERIC_DEVTECH)
    if not is_ai_primary:
        codes.append(REJECT_NOT_AI_FIRST)

    eligible = not codes
    if not eligible:
        # The one place this rejection is guaranteed to be logged, regardless of which
        # caller reached it or whether that caller also logs its own wrapping message —
        # see docs/v3.md § logging. Never logs the candidate's own text/URL: codes are
        # the machine-readable part, and rejection_reasons (attached to the raised
        # exception a caller may also log) already carries the human-readable form.
        logger.info("editorial_eligibility_rejected", extra={"codes": codes})

    return EditorialEligibilityResult(
        is_ai_primary=is_ai_primary,
        is_political=is_political,
        is_war_or_conflict=is_war_or_conflict,
        is_miltech=is_miltech,
        is_deftech=is_deftech,
        is_cybersecurity=is_cybersecurity,
        is_generic_government_news=is_generic_government_news,
        is_generic_devtech=is_generic_devtech,
        is_speculative_doom=is_speculative_doom,
        is_about_forbidden_geography=is_about_forbidden_geography,
        eligible=eligible,
        rejection_codes=tuple(codes),
        rejection_reasons=tuple(_REASON_TEXT[code] for code in codes),
    )


__all__ = [
    "REJECT_CYBERSECURITY",
    "REJECT_DEFTECH",
    "REJECT_FORBIDDEN_GEOGRAPHY",
    "REJECT_GENERIC_DEVTECH",
    "REJECT_GENERIC_GOVERNMENT",
    "REJECT_MILTECH",
    "REJECT_NOT_AI_FIRST",
    "REJECT_POLITICS",
    "REJECT_SPECULATIVE_DOOM",
    "REJECT_WAR",
    "EditorialEligibilityResult",
    "evaluate_eligibility",
]
