"""Editorial classification — Step 3 (AI News Agent v2), section 12.

A second, independent Gemini call that labels one candidate with its
:class:`~domain.enums.EditorialCategory` and :class:`~domain.enums.EditorialEvidence`,
structured and enum-constrained so Gemini can never invent an arbitrary type string —
the response schema below lists the exact enum members as its ``enum`` array, and
:class:`~automation.schema.ClassificationResult` re-validates against the same real
enums on the way back.

Wired into :func:`automation.pipeline_v2.run_pipeline_v2` — the scheduled production
path (``automation.pipeline_v2_live``) calls this for every candidate that has not
already been classified. It is still not read by v1's NEWS-only
``automation.pipeline._run_pipeline``, which has its own, separate selection/generation
call and does not use this module at all.

AI News Agent v3 priority step: this is also where the AI-first relevance gate and the
hard editorial content filters (politics, war, miltech, deftech, cybersecurity, generic
government news, generic devtech) are enforced, alongside the pre-existing
speculative-doom and forbidden-geography checks — see
:mod:`automation.eligibility` for the single deterministic decision all of these now
go through.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from ai_news_editor.automation.eligibility import evaluate_eligibility
from ai_news_editor.automation.gemini import GeminiClient, GeminiResponseError, parse_json_object
from ai_news_editor.automation.schema import ClassificationResult, SelectionCandidate
from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence
from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

_CLASSIFICATION_SYSTEM_INSTRUCTION = """\
You are classifying ONE candidate story for a Ukrainian Telegram channel about AI.

The channel is specifically about useful AI news, tools, workflows, models and
practical AI use — never a generic news aggregator. The central rule: if removing the
AI component would leave essentially the same news story, it does not belong here. AI
must be one of the story's primary subjects, not a secondary angle, keyword, quote, or
incidental mention.

You will receive one candidate's title, source, URL and any short excerpt already
collected. Answer every question below about it.

1. content_type — what kind of editorial post this candidate could become. Choose
   exactly one of the given enum values; never invent a new one.
2. evidence_type — how strong the evidence behind it is, judged only from what you were
   given (the source name and the excerpt), never from anything you know about the
   company or product from outside this material.
3. is_ai_primary — true ONLY if AI itself is one of the primary subjects: a new AI
   model, a ChatGPT/Claude/Gemini feature, AI image/video/audio generation, AI agents,
   AI automation, AI coding tools, AI workflows, AI research, a meaningful AI product
   release, or practical AI use. False for a story where AI is a secondary angle or a
   passing mention — a political story that happens to mention AI, a government story
   with an AI quote, a military story about a system that happens to contain AI, a
   cybersecurity incident involving an AI company, ordinary software/dev news with a
   tiny AI feature, or any story whose core subject is something else entirely. If you
   are not confident AI is genuinely primary, answer false — prefer a false negative
   here over letting an irrelevant story through.
4. is_speculative_doom — true only if this candidate's primary content is speculative
   doom/dystopian futurism about AI: an "AI apocalypse" narrative, a hypothetical
   "AI-run state," fear-driven speculation about society's future, clickbait about AI
   destroying society. Answer false for concrete, present-day reporting even when its
   subject is serious or negative — a security incident, a lawsuit, an actual
   regulation, a documented harmful behavior, a real policy decision are not
   speculation and must be answered false.
5. is_about_forbidden_geography — true if this candidate's story is substantially
   ABOUT Russia, Belarus, or Iran: their AI development, research, companies, product
   announcements, government policy, statistics, or any innovation/feature tied to one
   of those countries. This is about the story's own subject, not who published it — a
   US, European, British or Ukrainian outlet's story that is itself substantially
   about one of those three countries still answers true. A story that merely mentions
   one of those countries in passing, without being about it, answers false — the
   channel's editorial focus is Ukraine, Europe, the United Kingdom and the United
   States, and this question exists to keep stories actually centered elsewhere out.
6. is_political — true if this is primarily a political story: elections, political
   campaigns, party politics, political disputes, political speeches/statements,
   politicians as the main subject, or geopolitical disputes. False for a story whose
   core subject is an AI capability or product, even if a politician is briefly quoted.
7. is_war_or_conflict — true if this is primarily about war, armed conflict, combat
   operations, battlefield stories, military operations, or wartime operational news.
8. is_miltech — true if this is primarily about military AI, battlefield AI, military
   drones, targeting systems, autonomous weapons, military robotics, military
   surveillance AI, or weapons-related AI.
9. is_deftech — true if this is primarily about defence-industry or
   defence-technology, including a defence startup, defence procurement technology, a
   defence software platform, or a battlefield-intelligence system. A "startup" or "AI
   company" label is never a reason to answer false here if the actual subject is
   defence technology.
10. is_cybersecurity — true if this is primarily about a hack, breach, cyberattack,
    vulnerability, malware, ransomware, phishing campaign, threat actor, security
    incident, or offensive cybersecurity — even if the target or subject is an AI
    company. A genuinely new AI security product or tool being announced is not this;
    only answer true for the security-incident angle, not an AI-native security tool.
11. is_generic_government_news — true if this is ordinary government/state news where
    AI is not clearly the main practical subject: a minister's or president's
    statement that merely mentions AI, a generic public-sector digitalisation story.
    False for a story whose direct, primary subject is an AI-specific product,
    service, or policy change that materially affects AI users or products.
12. is_generic_devtech — true if this is ordinary developer tooling, cloud,
    databases, infrastructure, SaaS, or software-engineering news where AI is only an
    incidental feature. False when the product or update is materially and primarily
    AI-related.

If you cannot confidently answer content_type and evidence_type from the material
given — too little information, too ambiguous — return a rejection instead of
guessing. Answer every boolean question (3 through 12) either way, even on a
rejection — every one of them is required on every response, not only on a
successful classification.

Respond only with JSON matching the given schema.
"""

_CLASSIFICATION_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "OBJECT",
    "properties": {
        "content_type": {
            "type": "STRING",
            "enum": [member.value for member in EditorialCategory],
            "nullable": True,
        },
        "evidence_type": {
            "type": "STRING",
            "enum": [member.value for member in EditorialEvidence],
            "nullable": True,
        },
        "reason": {"type": "STRING", "nullable": True},
        "rejection_reason": {"type": "STRING", "nullable": True},
        "is_ai_primary": {"type": "BOOLEAN"},
        "is_speculative_doom": {"type": "BOOLEAN"},
        "is_about_forbidden_geography": {"type": "BOOLEAN"},
        "is_political": {"type": "BOOLEAN"},
        "is_war_or_conflict": {"type": "BOOLEAN"},
        "is_miltech": {"type": "BOOLEAN"},
        "is_deftech": {"type": "BOOLEAN"},
        "is_cybersecurity": {"type": "BOOLEAN"},
        "is_generic_government_news": {"type": "BOOLEAN"},
        "is_generic_devtech": {"type": "BOOLEAN"},
    },
    # Same discipline as _GENERATION_RESPONSE_SCHEMA in automation.provider: every key
    # is required so a response that silently omits one is a schema failure, not a
    # blank field; "nullable" is what still lets a rejection answer with null values.
    "required": [
        "content_type",
        "evidence_type",
        "reason",
        "rejection_reason",
        "is_ai_primary",
        "is_speculative_doom",
        "is_about_forbidden_geography",
        "is_political",
        "is_war_or_conflict",
        "is_miltech",
        "is_deftech",
        "is_cybersecurity",
        "is_generic_government_news",
        "is_generic_devtech",
    ],
}


class ClassificationRejected(Exception):
    """Gemini could not confidently classify the candidate. A normal outcome."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ClassificationInvalid(Exception):
    """Gemini's classification response did not parse, or did not match the schema."""


@dataclass(frozen=True, slots=True)
class Classification:
    """One candidate's editorial classification, as Gemini answered it."""

    content_type: EditorialCategory
    evidence_type: EditorialEvidence
    reason: str | None


def classify_candidate(client: GeminiClient, candidate: SelectionCandidate) -> Classification:
    """Ask Gemini to classify one candidate's content type and evidence strength.

    Raises:
        ClassificationRejected: Gemini declined to classify this candidate.
        ClassificationInvalid: the response did not parse, or did not match the schema
            (which includes Gemini naming a value outside the given enums — the schema
            constrains the wire response, and :class:`ClassificationResult` re-checks
            against the real Python enums on top of that).
    """
    prompt = _render_classification_prompt(candidate)

    try:
        result = client.generate(
            system_instruction=_CLASSIFICATION_SYSTEM_INSTRUCTION,
            prompt=prompt,
            response_schema=_CLASSIFICATION_RESPONSE_SCHEMA,
        )
        parsed = ClassificationResult.model_validate(parse_json_object(result.text))
    except GeminiResponseError as exc:
        raise ClassificationInvalid(
            f"Gemini's classification response was unusable: {exc}"
        ) from exc
    except ValidationError as exc:
        raise ClassificationInvalid(
            f"Gemini's classification response did not match the schema: {exc}"
        ) from exc

    # AI News Agent v3 priority step: the single eligibility gate — AI-first
    # relevance plus every hard content filter — runs before Gemini's own
    # rejection_reason is even consulted, same precedence the doom/geography checks
    # this replaces already had. Deterministic and unconditional: never overridden by
    # a content_type Gemini also filled in on the same response, and never skipped
    # just because Gemini itself already declined to classify.
    eligibility = evaluate_eligibility(
        is_ai_primary=parsed.is_ai_primary,
        is_political=parsed.is_political,
        is_war_or_conflict=parsed.is_war_or_conflict,
        is_miltech=parsed.is_miltech,
        is_deftech=parsed.is_deftech,
        is_cybersecurity=parsed.is_cybersecurity,
        is_generic_government_news=parsed.is_generic_government_news,
        is_generic_devtech=parsed.is_generic_devtech,
        is_speculative_doom=parsed.is_speculative_doom,
        is_about_forbidden_geography=parsed.is_about_forbidden_geography,
    )
    if not eligibility.eligible:
        raise ClassificationRejected("; ".join(eligibility.rejection_reasons))

    if parsed.rejection_reason is not None:
        raise ClassificationRejected(parsed.rejection_reason)

    assert parsed.content_type is not None  # the model's own validator guarantees this
    assert parsed.evidence_type is not None
    return Classification(
        content_type=parsed.content_type,
        evidence_type=parsed.evidence_type,
        reason=parsed.reason,
    )


def _render_classification_prompt(candidate: SelectionCandidate) -> str:
    lines = [
        f"Title: {candidate.title}",
        f"Source: {candidate.source_name}",
        f"URL: {candidate.url}",
    ]
    if candidate.published_at:
        lines.append(f"Published: {candidate.published_at}")
    if candidate.summary:
        lines.append(f"Summary: {candidate.summary}")
    return "\n".join(lines)
