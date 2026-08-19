"""Editorial classification — Step 3 (AI News Agent v2), section 12.

A second, independent Gemini call that labels one candidate with its
:class:`~domain.enums.EditorialCategory` and :class:`~domain.enums.EditorialEvidence`,
structured and enum-constrained so Gemini can never invent an arbitrary type string —
the response schema below lists the exact enum members as its ``enum`` array, and
:class:`~automation.schema.ClassificationResult` re-validates against the same real
enums on the way back.

Deliberately NOT wired into :func:`automation.provider.select_candidate`,
``generate_post``, or ``automation.pipeline._run_pipeline``: the live NEWS-only
automation pipeline's behaviour must not change as a side effect of this module
existing. This is additive, standalone capability — usable today from the offline
``ai-news editorial preview`` tool and tests, and available for a later step to wire
into the live pipeline on its own terms.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from ai_news_editor.automation.gemini import GeminiClient, GeminiResponseError, parse_json_object
from ai_news_editor.automation.schema import ClassificationResult, SelectionCandidate
from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence
from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

_CLASSIFICATION_SYSTEM_INSTRUCTION = """\
You are classifying ONE candidate story for a Ukrainian Telegram channel about AI.

You will receive one candidate's title, source, URL and any short excerpt already
collected. Answer four questions about it:

1. content_type — what kind of editorial post this candidate could become. Choose
   exactly one of the given enum values; never invent a new one.
2. evidence_type — how strong the evidence behind it is, judged only from what you were
   given (the source name and the excerpt), never from anything you know about the
   company or product from outside this material.
3. is_speculative_doom — true only if this candidate's primary content is speculative
   doom/dystopian futurism about AI: an "AI apocalypse" narrative, a hypothetical
   "AI-run state," fear-driven speculation about society's future, clickbait about AI
   destroying society. Answer false for concrete, present-day reporting even when its
   subject is serious or negative — a security incident, a lawsuit, an actual
   regulation, a documented harmful behavior, a real policy decision are not
   speculation and must be answered false.
4. is_about_forbidden_geography — true if this candidate's story is substantially
   ABOUT Russia, Belarus, or Iran: their AI development, research, companies, product
   announcements, government policy, statistics, or any innovation/feature tied to one
   of those countries. This is about the story's own subject, not who published it — a
   US, European, British or Ukrainian outlet's story that is itself substantially
   about one of those three countries still answers true. A story that merely mentions
   one of those countries in passing, without being about it, answers false — the
   channel's editorial focus is Ukraine, Europe, the United Kingdom and the United
   States, and this question exists to keep stories actually centered elsewhere out.

If you cannot confidently answer content_type and evidence_type from the material
given — too little information, too ambiguous — return a rejection instead of
guessing. Answer is_speculative_doom and is_about_forbidden_geography either way, even
on a rejection.

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
        "is_speculative_doom": {"type": "BOOLEAN"},
        "is_about_forbidden_geography": {"type": "BOOLEAN"},
    },
    # Same discipline as _GENERATION_RESPONSE_SCHEMA in automation.provider: every key
    # is required so a response that silently omits one is a schema failure, not a
    # blank field; "nullable" is what still lets a rejection answer with null values.
    "required": [
        "content_type",
        "evidence_type",
        "reason",
        "rejection_reason",
        "is_speculative_doom",
        "is_about_forbidden_geography",
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

    if parsed.is_speculative_doom:
        # Deterministic, local: a True here is rejected unconditionally, never
        # overridden by a content_type Gemini also filled in on the same response.
        raise ClassificationRejected(
            "speculative dystopian/doom-futurism narrative, not concrete reporting"
        )

    if parsed.is_about_forbidden_geography:
        # Step 6C: a hard content filter, not a soft preference — a story whose own
        # subject is Russia/Belarus/Iran is rejected regardless of which source
        # (even a UA/EU/UK/US one) reported it. Deterministic and unconditional, same
        # discipline as is_speculative_doom above.
        raise ClassificationRejected(
            "story is substantially about Russia, Belarus, or Iran — outside the "
            "channel's Ukraine/Europe/UK/US editorial geography"
        )

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
