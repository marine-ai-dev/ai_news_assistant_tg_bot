"""The semantic contract between generation and rendering — Step 5 (AI News Agent v2).

``EditorialContent`` is what Gemini (or a deterministic fixture) produces: plain
strings and structural fields, never Markdown, never a Telegram concept. Every field
here answers an editorial question ("what happened", "what kind of free is this",
"is this prompt verbatim") — none of them is "how bold should this be" or "where does
the link go." That split is the whole point: :mod:`rendering.render` is the only code
that turns this record into MarkdownV2, and it escapes every string field on the way
in, so nothing placed here can inject Telegram markup even if it tried.

``extra="forbid"`` everywhere, matching ``automation.schema``'s own discipline: an
unexpected field from a generation call is a bug in the prompt or the model's
understanding of it, not something to silently accept.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_news_editor.domain.enums import (
    EditorialCategory,
    EditorialEvidence,
    FreeDealKind,
    PromptOrigin,
)
from ai_news_editor.editorial.safety import ResearchClaimFraming
from ai_news_editor.rendering.style import (
    ORDINARY_MAX_BODY_BLOCKS,
    ORDINARY_MAX_DETAIL_BULLETS,
    WIDE_CATEGORIES,
)

NonEmpty = Field(min_length=1, max_length=4000)


class ContentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BodyBlock(ContentModel):
    """One semantic paragraph: what it's *for*, and its plain text.

    ``purpose`` must be a key ``rendering.style.BLOCK_EMOJI`` recognizes — checked at
    render time, not here, since the valid purpose set is a rendering-layer concern
    this model does not need to duplicate.
    """

    purpose: str = Field(min_length=1, max_length=60)
    text: str = NonEmpty


class DigestItem(ContentModel):
    """One entry in a WEEKLY_DIGEST post."""

    headline: str = Field(min_length=1, max_length=200)
    summary: str = NonEmpty
    source_label: str | None = None
    source_url: str | None = None

    @model_validator(mode="after")
    def _label_and_url_travel_together(self) -> Self:
        if (self.source_label is None) != (self.source_url is None):
            raise ValueError("source_label and source_url must be set together, or not at all")
        return self


class EditorialContent(ContentModel):
    """Everything ``rendering.render`` needs to produce one post, for one category.

    Fields below the shared block are category-specific and optional; each category's
    render function in :mod:`rendering.render` requires the ones it needs and ignores
    the rest — enforced by that function raising, not by a variant model per category,
    since the shared fields (headline, body, source) are identical across all eight.
    """

    category: EditorialCategory
    evidence: EditorialEvidence
    #: Plain text — no leading emoji, no markup. The renderer adds both.
    headline: str = Field(min_length=1, max_length=200)
    body: tuple[BodyBlock, ...] = Field(min_length=1)
    #: Optional "🔆 Детальніше" bullets — 2-4 concrete specifics, never a long dump.
    detail_bullets: tuple[str, ...] = Field(default=(), max_length=4)
    source_label: str = Field(min_length=1, max_length=120)
    source_url: str = Field(min_length=1)

    # --- PROMPT_WORKFLOW ---------------------------------------------------------
    prompt_origin: PromptOrigin | None = None
    prompt_text: str | None = Field(default=None, max_length=2000)

    # --- FREE_DEAL -----------------------------------------------------------------
    free_deal_kind: FreeDealKind | None = None

    # --- RESEARCH --------------------------------------------------------------
    research_framing: ResearchClaimFraming | None = None
    research_independently_verified: bool = False

    # --- WEEKLY_DIGEST ---------------------------------------------------------
    digest_items: tuple[DigestItem, ...] = Field(default=())

    @model_validator(mode="after")
    def _category_specific_fields_are_present_when_required(self) -> Self:
        if self.category is EditorialCategory.PROMPT_WORKFLOW and (
            self.prompt_origin is None or not self.prompt_text
        ):
            raise ValueError("PROMPT_WORKFLOW requires prompt_origin and prompt_text")
        if self.category is EditorialCategory.FREE_DEAL and self.free_deal_kind is None:
            raise ValueError("FREE_DEAL requires free_deal_kind")
        if self.category is EditorialCategory.RESEARCH and self.research_framing is None:
            raise ValueError("RESEARCH requires research_framing")
        if self.category is EditorialCategory.WEEKLY_DIGEST and not self.digest_items:
            raise ValueError("WEEKLY_DIGEST requires at least one digest item")
        return self

    @model_validator(mode="after")
    def _ordinary_categories_stay_short(self) -> Self:
        """Step 6B: the generation contract itself produces fewer, shorter fields for
        an ordinary post — enforced here, not by the renderer truncating afterward.
        RESEARCH/EXPLAINER/WEEKLY_DIGEST (``rendering.style.WIDE_CATEGORIES``) are
        exempt, exactly like the renderer's own length warning already exempts them.
        """
        if self.category in WIDE_CATEGORIES:
            return self
        if len(self.body) > ORDINARY_MAX_BODY_BLOCKS:
            raise ValueError(
                f"{self.category.value} allows at most {ORDINARY_MAX_BODY_BLOCKS} body "
                f"blocks, got {len(self.body)}"
            )
        if len(self.detail_bullets) > ORDINARY_MAX_DETAIL_BULLETS:
            raise ValueError(
                f"{self.category.value} allows at most {ORDINARY_MAX_DETAIL_BULLETS} "
                f"detail bullets, got {len(self.detail_bullets)}"
            )
        return self


__all__ = ["BodyBlock", "DigestItem", "EditorialContent"]
