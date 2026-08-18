"""Structured, category-aware generation — Step 5 (AI News Agent v2).

The Step 3 generation contracts (``editorial.policy.CATEGORY_PROMPTS``) were written
but never wired to a live Gemini call. This module is that call: given a candidate
already classified into an :class:`~domain.enums.EditorialCategory` (by
``automation.classification``) with its evidence type, full article text, and that
category's own generation rules, ask Gemini for **semantic fields**, never Markdown.

Gemini does not decide bold, links, emoji, or ``parse_mode`` — ``rendering.render`` is
the only code that turns the result into Telegram markup (see that module's own
docstring). This call's response schema mirrors ``rendering.content.EditorialContent``
closely enough that its output plugs into ``EditorialContent`` almost directly — the
one deliberate exception is ``source_url``: exactly like ``SelectionResult`` never
lets Gemini's own URL copy reach a candidate, the source URL for the final post is
always this application's own record of the candidate, never Gemini's echo of it (see
``build_editorial_content``).
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_news_editor.automation.gemini import GeminiClient, GeminiResponseError, parse_json_object
from ai_news_editor.domain.enums import (
    EditorialCategory,
    EditorialEvidence,
    FreeDealKind,
    PromptOrigin,
)
from ai_news_editor.editorial.policy import prompt_for
from ai_news_editor.editorial.safety import ResearchClaimFraming
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.rendering.content import BodyBlock, DigestItem, EditorialContent
from ai_news_editor.rendering.style import BLOCK_EMOJI

logger = get_logger(__name__)

_KNOWN_PURPOSES = tuple(BLOCK_EMOJI.keys())

_STRUCTURED_OUTPUT_RULE = """\

Respond only with JSON matching the given schema. You are never asked to write
Markdown, bold text, emoji placement, or links — that is decided entirely by this
application after your response, from the plain fields you provide. Write plain
sentences with no markup of any kind.

Every "purpose" value in a body block must be exactly one of the given enum values —
never invent a new one.
"""


class _GeneratedBodyBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str
    text: str

    @model_validator(mode="after")
    def _purpose_is_a_known_block(self) -> Self:
        # Re-checked here, not just trusted from the wire-level schema enum: this is
        # the same "never trust the model, verify locally" discipline the rest of the
        # project already applies to enum-constrained Gemini output.
        if self.purpose not in _KNOWN_PURPOSES:
            raise ValueError(f"{self.purpose!r} is not a known body-block purpose")
        return self


class _GeneratedDigestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str
    summary: str
    source_label: str | None = None
    source_url: str | None = None


class GeneratedEditorialContent(BaseModel):
    """Gemini's structured answer: semantic fields only, or an explicit rejection.

    Deliberately close to :class:`rendering.content.EditorialContent`, minus
    ``category``/``evidence`` (already decided before this call, by
    ``automation.classification`` — never re-asked) and minus ``source_url`` (always
    this application's own candidate record, never Gemini's copy of it).
    """

    model_config = ConfigDict(extra="forbid")

    headline: str | None = None
    body: tuple[_GeneratedBodyBlock, ...] = ()
    detail_bullets: tuple[str, ...] = ()
    source_label: str | None = None

    prompt_origin: PromptOrigin | None = None
    prompt_text: str | None = None
    free_deal_kind: FreeDealKind | None = None
    research_framing: ResearchClaimFraming | None = None
    research_independently_verified: bool = False
    digest_items: tuple[_GeneratedDigestItem, ...] = ()

    #: The model's own belief that every fact above is grounded in the supplied
    #: article text — same discipline as ``GeneratedPost.confidence``.
    confidence: int | None = Field(default=None, ge=0, le=100)
    rejection_reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _null_arrays_and_flags_become_their_empty_default(cls, data: object) -> object:
        """The wire schema marks every array/boolean field ``nullable`` (Gemini's own
        requirement: ``required`` means the key must be present, independent of
        whether its value may be null) — a rejection response legitimately sends
        ``null`` for all of them. Normalized to the Python-side defaults here, once,
        rather than making every one of those fields ``Optional`` and asking every
        later reader to handle two different "empty" shapes."""
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for key in ("body", "detail_bullets", "digest_items"):
            if normalized.get(key) is None:
                normalized[key] = []
        if normalized.get("research_independently_verified") is None:
            normalized["research_independently_verified"] = False
        return normalized

    @model_validator(mode="after")
    def _post_or_rejection(self) -> Self:
        have_post = self.headline is not None or bool(self.body)
        if self.rejection_reason is not None and have_post:
            raise ValueError("a rejection must not also carry post content")
        if self.rejection_reason is None and (
            not self.headline or not self.body or not self.source_label or self.confidence is None
        ):
            raise ValueError(
                "a post that is not a rejection must supply headline, body, "
                "source_label and confidence"
            )
        return self

    @property
    def is_rejection(self) -> bool:
        return self.rejection_reason is not None


class GenerationV2Rejected(Exception):
    """Gemini declined to write from the supplied article. A normal outcome."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GenerationV2Invalid(Exception):
    """The response did not parse, or did not match the schema."""


def _response_schema() -> dict[str, object]:
    body_item = {
        "type": "OBJECT",
        "properties": {
            "purpose": {"type": "STRING", "enum": list(_KNOWN_PURPOSES)},
            "text": {"type": "STRING"},
        },
        "required": ["purpose", "text"],
    }
    digest_item = {
        "type": "OBJECT",
        "properties": {
            "headline": {"type": "STRING"},
            "summary": {"type": "STRING"},
            "source_label": {"type": "STRING", "nullable": True},
            "source_url": {"type": "STRING", "nullable": True},
        },
        "required": ["headline", "summary", "source_label", "source_url"],
    }
    return {
        "type": "OBJECT",
        "properties": {
            "headline": {"type": "STRING", "nullable": True},
            "body": {"type": "ARRAY", "items": body_item, "nullable": True},
            "detail_bullets": {
                "type": "ARRAY", "items": {"type": "STRING"}, "nullable": True,
            },
            "source_label": {"type": "STRING", "nullable": True},
            "prompt_origin": {
                "type": "STRING", "enum": [m.value for m in PromptOrigin], "nullable": True,
            },
            "prompt_text": {"type": "STRING", "nullable": True},
            "free_deal_kind": {
                "type": "STRING", "enum": [m.value for m in FreeDealKind], "nullable": True,
            },
            "research_framing": {
                "type": "STRING",
                "enum": [m.value for m in ResearchClaimFraming],
                "nullable": True,
            },
            "research_independently_verified": {"type": "BOOLEAN", "nullable": True},
            "digest_items": {"type": "ARRAY", "items": digest_item, "nullable": True},
            "confidence": {"type": "INTEGER", "nullable": True},
            "rejection_reason": {"type": "STRING", "nullable": True},
        },
        "required": [
            "headline", "body", "detail_bullets", "source_label", "prompt_origin",
            "prompt_text", "free_deal_kind", "research_framing",
            "research_independently_verified", "digest_items", "confidence",
            "rejection_reason",
        ],
    }


def generate_editorial_content(
    client: GeminiClient,
    *,
    category: EditorialCategory,
    article_text: str,
    source_label: str,
) -> GeneratedEditorialContent:
    """Ask Gemini to write the semantic fields for one post of ``category``.

    Raises:
        GenerationV2Rejected: Gemini declined to write from the supplied article.
        GenerationV2Invalid: the response did not parse or match the schema.
    """
    system_instruction = prompt_for(category).render_instruction() + _STRUCTURED_OUTPUT_RULE
    prompt = (
        f"Source: {source_label}\n\n"
        f"Article text:\n{article_text}"
    )

    try:
        result = client.generate(
            system_instruction=system_instruction,
            prompt=prompt,
            response_schema=_response_schema(),
        )
        parsed = GeneratedEditorialContent.model_validate(parse_json_object(result.text))
    except GeminiResponseError as exc:
        raise GenerationV2Invalid(f"Gemini's response was unusable: {exc}") from exc
    except ValueError as exc:
        raise GenerationV2Invalid(f"Gemini's response did not match the schema: {exc}") from exc

    if parsed.is_rejection:
        assert parsed.rejection_reason is not None
        raise GenerationV2Rejected(parsed.rejection_reason)
    return parsed


def build_editorial_content(
    generated: GeneratedEditorialContent,
    *,
    category: EditorialCategory,
    evidence: EditorialEvidence,
    source_url: str,
) -> EditorialContent:
    """Combine Gemini's semantic fields with this application's own record.

    ``category``/``evidence``/``source_url`` never come from ``generated`` — the same
    "never trust a URL the model types back" discipline ``SelectionResult`` already
    uses, applied here to the whole classification+source identity, not just the URL.
    """
    assert generated.headline is not None and generated.source_label is not None
    return EditorialContent(
        category=category,
        evidence=evidence,
        headline=generated.headline,
        body=tuple(BodyBlock(purpose=b.purpose, text=b.text) for b in generated.body),
        detail_bullets=generated.detail_bullets,
        source_label=generated.source_label,
        source_url=source_url,
        prompt_origin=generated.prompt_origin,
        prompt_text=generated.prompt_text,
        free_deal_kind=generated.free_deal_kind,
        research_framing=generated.research_framing,
        research_independently_verified=generated.research_independently_verified,
        digest_items=tuple(
            DigestItem(
                headline=item.headline,
                summary=item.summary,
                source_label=item.source_label,
                source_url=item.source_url,
            )
            for item in generated.digest_items
        ),
    )


__all__ = [
    "GeneratedEditorialContent",
    "GenerationV2Invalid",
    "GenerationV2Rejected",
    "build_editorial_content",
    "generate_editorial_content",
]
