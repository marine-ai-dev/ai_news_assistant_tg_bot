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
collected. Answer two questions about it:

1. content_type — what kind of editorial post this candidate could become. Choose
   exactly one of the given enum values; never invent a new one.
2. evidence_type — how strong the evidence behind it is, judged only from what you were
   given (the source name and the excerpt), never from anything you know about the
   company or product from outside this material.

If you cannot confidently answer both from the material given — too little
information, too ambiguous — return a rejection instead of guessing.

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
    },
    # Same discipline as _GENERATION_RESPONSE_SCHEMA in automation.provider: every key
    # is required so a response that silently omits one is a schema failure, not a
    # blank field; "nullable" is what still lets a rejection answer with null values.
    "required": ["content_type", "evidence_type", "reason", "rejection_reason"],
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
