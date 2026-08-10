"""The writing exchange contract.

The same pattern as the editorial exchange, one stage later:

* :class:`WritingBatch` — assignments going out to be written.
* :class:`DraftBatch` — finished Ukrainian drafts coming back.

A writing session supplies a headline, a body, the source it used and a chosen format.
It does **not** supply the rendered post or its hash — Python assembles both, so the
content a reviewer eventually approves is computed from validated parts rather than
taken on trust.

The returned schema has no field for approving or publishing anything. That is not an
omission to be filled in later; it is why importing a draft cannot produce a published
post no matter what the returned document says.
"""

from __future__ import annotations

from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_news_editor.domain.clock import UtcDatetime, now_utc
from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    PostFormat,
    TrustTier,
    VerificationStatus,
)
from ai_news_editor.writing.format import (
    HARD_MAX_CHARS,
    MAX_HEADLINE_CHARS,
    UnsafeLinkError,
    disallowed_tags,
    hard_limit_problem,
    render_post,
    validate_url,
)

#: Bumped when the assignment/draft JSON contract changes shape.
WRITING_SCHEMA_VERSION = "1"

#: Bumped when the style guide changes in a way that makes older drafts non-comparable.
#: Stored on every draft version, so a guide revision never silently reinterprets text
#: written under the previous one.
STYLE_VERSION = "1"

#: How much source text a writer receives. Enough to write accurately without shipping
#: whole article bodies into a batch.
SOURCE_EXCERPT_LIMIT = 1500

NonEmpty = Annotated[str, Field(min_length=1)]


class StrictModel(BaseModel):
    """Unknown fields are a bug in the producer, never something to tolerate."""

    model_config = ConfigDict(extra="forbid")


# --- outbound: writing assignments ------------------------------------------


class AssignmentSource(StrictModel):
    """Where the story came from. Carried so a draft can never lose its provenance."""

    id: str
    name: str
    trust_tier: TrustTier
    url: str


class AssignmentEvaluation(StrictModel):
    """The Phase-4 judgement that authorised writing this story."""

    evaluation_id: UUID
    category: Category
    audience: AudienceTier
    composite_score: float
    editorial_angle: str | None = None
    why_selected: tuple[str, ...] = ()
    verification_status: VerificationStatus
    verification_sources: tuple[dict[str, str], ...] = ()
    #: Present so a writer can see which dimensions carried the story.
    scores: dict[str, int] = Field(default_factory=dict)


class WritingAssignment(StrictModel):
    """One story to write."""

    article_id: UUID
    article_fingerprint: str = Field(min_length=1)
    source: AssignmentSource
    original_title: str
    published_at: str | None = None
    source_excerpt: str | None = None
    source_excerpt_truncated: bool = False
    evaluation: AssignmentEvaluation
    #: Set when the source gave us no body text. The writer should check the original
    #: rather than write around the gap.
    needs_source_check: bool = False
    suggested_format: PostFormat = PostFormat.STANDARD


class WritingBatch(StrictModel):
    """A set of writing assignments."""

    schema_version: str = WRITING_SCHEMA_VERSION
    style_version: str = STYLE_VERSION
    batch_id: str
    generated_at: UtcDatetime = Field(default_factory=now_utc)
    assignment_count: int = 0
    assignments: list[WritingAssignment]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.schema_version != WRITING_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {self.schema_version!r}; "
                f"expected {WRITING_SCHEMA_VERSION!r}"
            )
        ids = [assignment.article_id for assignment in self.assignments]
        if len(ids) != len(set(ids)):
            raise ValueError("an article appears more than once in the batch")
        object.__setattr__(self, "assignment_count", len(self.assignments))
        return self


# --- inbound: finished drafts -----------------------------------------------


class DraftResult(StrictModel):
    """One finished Ukrainian post."""

    article_id: UUID
    evaluation_id: UUID
    article_fingerprint: str = Field(min_length=1)
    post_format: PostFormat
    headline: NonEmpty = Field(max_length=MAX_HEADLINE_CHARS)
    body: NonEmpty
    #: How the source is named in the post, e.g. "OpenAI".
    source_label: NonEmpty = Field(max_length=120)
    source_url: NonEmpty
    hashtags: tuple[str, ...] = Field(default=(), max_length=6)
    #: Internal only. Never published, and deliberately excluded from the content hash
    #: so a note cannot change what a reviewer is approving.
    writer_notes: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def _post_is_sendable(self) -> Self:
        try:
            validate_url(self.source_url)
        except UnsafeLinkError as exc:
            raise ValueError(str(exc)) from exc

        for field_name, text in (("headline", self.headline), ("body", self.body)):
            bad = disallowed_tags(text)
            if bad:
                raise ValueError(
                    f"{field_name} uses markup outside the permitted subset: "
                    f"{', '.join(sorted(bad))}"
                )
            if not text.strip():
                raise ValueError(f"{field_name} is blank")

        problem = hard_limit_problem(self.rendered_text)
        if problem:
            raise ValueError(problem)
        return self

    @property
    def rendered_text(self) -> str:
        """The post as it would be sent. Assembled by Python, not supplied by the writer."""
        return render_post(
            headline=self.headline,
            body=self.body,
            source_label=self.source_label,
            source_url=self.source_url,
        )


class DraftBatch(StrictModel):
    """Finished drafts returned for a writing batch."""

    schema_version: str
    style_version: str
    batch_id: str = Field(min_length=1)
    written_at: UtcDatetime = Field(default_factory=now_utc)
    writer: str = Field(default="claude-code", min_length=1)
    drafts: list[DraftResult] = Field(min_length=1)

    @model_validator(mode="after")
    def _versions_and_uniqueness(self) -> Self:
        if self.schema_version != WRITING_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {self.schema_version!r}; "
                f"expected {WRITING_SCHEMA_VERSION!r}"
            )
        if self.style_version != STYLE_VERSION:
            raise ValueError(
                f"unsupported style_version {self.style_version!r}; expected {STYLE_VERSION!r}"
            )
        ids = [draft.article_id for draft in self.drafts]
        if len(ids) != len(set(ids)):
            duplicates = sorted({str(i) for i in ids if ids.count(i) > 1})
            raise ValueError(f"an article is drafted more than once: {', '.join(duplicates)}")
        return self


#: Re-exported so callers do not need to reach into the format module for the limit.
__all__ = [
    "HARD_MAX_CHARS",
    "SOURCE_EXCERPT_LIMIT",
    "STYLE_VERSION",
    "WRITING_SCHEMA_VERSION",
    "AssignmentEvaluation",
    "AssignmentSource",
    "DraftBatch",
    "DraftResult",
    "WritingAssignment",
    "WritingBatch",
]
