"""The editorial-content exchange contract.

The third exchange in the project, and the simplest. Phase 4 sends articles out to be
judged; Phase 5 sends shortlisted stories out to be written. Here there is nothing to
send out — a prompt or an explainer starts as an idea, so the batch only comes *back*:
a Claude Code session writes one file containing both the editorial substance and the
finished Ukrainian post, and Python validates and imports it.

One batch, one direction, because splitting it would mean inventing an "assignment" for
content nobody has thought of yet.

What this schema deliberately cannot express: approval, publication, a status, or a
channel. Importing produces drafts in PENDING_REVIEW and there is no field that could
ask for anything else. Editorial-original content earns no shortcut for having been
written in-house — if anything it deserves more scrutiny, since no external source
would have caught an invented claim.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_news_editor.domain.clock import UtcDatetime, now_utc
from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    ContentType,
    EvidenceKind,
    MediaOrigin,
    MediaRole,
    PostFormat,
    PromptPlacement,
    PromptRepresentation,
    PromptTopic,
    ResourceType,
    SourceTier,
    UseCaseTheme,
)
from ai_news_editor.writing.format import (
    MAX_HEADLINE_CHARS,
    disallowed_tags,
    hard_limit_problem,
    render_post,
)

#: Bumped when this JSON contract changes shape.
CONTENT_SCHEMA_VERSION = "1"

NonEmpty = Annotated[str, Field(min_length=1)]


class StrictModel(BaseModel):
    """Unknown fields are a bug in the producer, never something to tolerate."""

    model_config = ConfigDict(extra="forbid")


class SubmittedReference(StrictModel):
    """An optional factual reference. Never dressed up as a source article."""

    label: NonEmpty
    url: NonEmpty
    supports: NonEmpty


class SubmittedPost(StrictModel):
    """The Ukrainian post itself — the part a human will read and approve.

    Same shape as a written news draft, minus the source. Python renders and hashes it,
    so the writer supplies parts and never the final text.
    """

    headline: str = Field(min_length=1, max_length=MAX_HEADLINE_CHARS)
    body: NonEmpty
    category: Category
    post_format: PostFormat
    hashtags: list[str] = Field(default_factory=list, max_length=5)
    #: Internal notes for the reviewer. Never published.
    writer_notes: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _is_publishable_text(self) -> Self:
        for field_name, text in (("headline", self.headline), ("body", self.body)):
            bad = disallowed_tags(text)
            if bad:
                raise ValueError(
                    f"{field_name} uses markup outside the permitted subset: "
                    f"{', '.join(sorted(bad))}"
                )
        problem = hard_limit_problem(render_post(headline=self.headline, body=self.body))
        if problem:
            raise ValueError(problem)
        return self


class SubmittedEvidence(StrictModel):
    """The demonstration a prompt rests on.

    Required, and required in full. A prompt post is a claim that this worked for
    somebody, and every field here is part of that claim: drop one and the reader is
    being told something we cannot support.

    Nothing may be inferred to fill a gap. If the source does not name a model version,
    ``model_version`` stays absent — guessing it is inventing evidence, which is the
    exact failure this whole contract exists to prevent.
    """

    source_url: NonEmpty
    source_title: NonEmpty
    source_tier: SourceTier
    #: Where it was found, for social sources: Reddit, Threads, X, YouTube, a blog.
    source_platform: str | None = None
    #: The handle or byline, as the platform shows it.
    source_author: str | None = None
    tested_by: NonEmpty
    tool_used: NonEmpty
    model_version: str | None = None
    what_was_tested: NonEmpty
    observed_result: NonEmpty
    limitations: list[NonEmpty] = Field(default_factory=list, max_length=6)
    requires: list[NonEmpty] = Field(default_factory=list, max_length=6)
    checked_at: UtcDatetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def _the_source_is_reachable_in_principle(self) -> Self:
        if not self.source_url.startswith(("http://", "https://")):
            raise ValueError(
                f"source_url must be an http(s) link, got {self.source_url!r}. Python "
                "cannot check that a page exists — verifying the source is the research "
                "step's job — but a value that could never be a link is refused here."
            )
        return self


class SubmittedPrompt(StrictModel):
    """A prompt item: the demonstration, the structure, and the post written around it."""

    content_type: Literal[ContentType.PROMPT] = ContentType.PROMPT
    title: NonEmpty
    audience: AudienceTier
    topic: PromptTopic
    what_you_can_do: NonEmpty
    prompt_text: NonEmpty
    customization_tips: list[NonEmpty] = Field(min_length=1, max_length=6)
    works_with: str | None = None
    #: Required. There is no such thing as a prompt post without one.
    evidence: SubmittedEvidence
    #: How the prompt relates to the source's: quoted, reworded, or reconstructed from a
    #: described workflow. An adapted prompt is never presented as a quotation.
    representation: PromptRepresentation
    references: list[SubmittedReference] = Field(default_factory=list)
    post: SubmittedPost

    @model_validator(mode="after")
    def _the_post_links_its_source(self) -> Self:
        """A reader must be able to go and look at the demonstration themselves.

        The whole point of the correction is that these posts are reports of somebody
        else's work. A report without a link is indistinguishable from an invention.
        """
        if self.evidence.source_url not in self.post.body:
            raise ValueError(
                "the post body does not contain the source link; a tested-workflow post "
                "has to show where the test came from"
            )
        return self

    @model_validator(mode="after")
    def _the_prompt_is_in_the_post(self) -> Self:
        """The reader must be able to copy the prompt out of the post.

        A prompt post whose body does not contain the prompt is a description of a
        prompt. Checked structurally rather than trusted, because this is the one thing
        the format exists to deliver.
        """
        needle = " ".join(self.prompt_text.split())[:60]
        haystack = " ".join(self.post.body.split())
        if needle not in haystack:
            raise ValueError(
                "the post body does not contain the prompt text; a reader has to be able "
                "to copy it"
            )
        return self


class SubmittedMedia(StrictModel):
    """An image, screenshot or file that belongs to a post."""

    role: MediaRole
    origin: MediaOrigin
    reference: NonEmpty
    description: NonEmpty
    tool_used: str | None = None
    model_version: str | None = None
    source_url: str | None = None


class SubmittedResource(StrictModel):
    """A downloadable or curated thing a RESOURCE post is built around."""

    resource_type: ResourceType
    title: NonEmpty
    description: NonEmpty
    version: str | None = None
    asset: SubmittedMedia | None = None


class SubmittedUseCase(StrictModel):
    """Something a real person did with AI, retold.

    Distinct from a prompt: the value is the workflow and the result, and the prompt may
    be one component or absent. The evidence requirement is the same, because "somebody
    did this" is exactly as much of a claim as "this prompt works".
    """

    content_type: Literal[ContentType.TESTED_USE_CASE] = ContentType.TESTED_USE_CASE
    title: NonEmpty
    audience: AudienceTier
    theme: UseCaseTheme
    what_the_person_did: NonEmpty
    reported_benefit: NonEmpty
    how_to_try: list[NonEmpty] = Field(min_length=1, max_length=6)
    prompt_text: str | None = None
    evidence: SubmittedEvidence
    evidence_kind: EvidenceKind
    prompt_placement: PromptPlacement = PromptPlacement.NONE
    comment_text: str | None = None
    media: list[SubmittedMedia] = Field(default_factory=list, max_length=4)
    series_name: str | None = None
    series_order: int | None = Field(default=None, ge=1)
    references: list[SubmittedReference] = Field(default_factory=list)
    post: SubmittedPost

    @model_validator(mode="after")
    def _the_bundle_is_coherent(self) -> Self:
        if self.prompt_placement is PromptPlacement.COMMENT and not (
            self.comment_text and self.comment_text.strip()
        ):
            raise ValueError(
                "prompt_placement is COMMENT but no comment_text was supplied; the post "
                "would promise a prompt that is not there"
            )
        if self.prompt_placement is PromptPlacement.INLINE and not self.prompt_text:
            raise ValueError("prompt_placement is INLINE but there is no prompt")
        if self.evidence.source_url not in self.post.body:
            raise ValueError(
                "the post body does not contain the source link; a retold use case has "
                "to show whose it was"
            )
        if (self.series_order is None) != (self.series_name is None):
            raise ValueError("a series needs both a name and a position, or neither")
        return self


class SubmittedResourcePost(StrictModel):
    """A curated or downloadable resource."""

    content_type: Literal[ContentType.RESOURCE] = ContentType.RESOURCE
    title: NonEmpty
    audience: AudienceTier
    resource: SubmittedResource
    what_it_gives_you: NonEmpty
    how_to_use: list[NonEmpty] = Field(min_length=1, max_length=6)
    series_name: str | None = None
    series_order: int | None = Field(default=None, ge=1)
    references: list[SubmittedReference] = Field(default_factory=list)
    post: SubmittedPost


class SubmittedExplainer(StrictModel):
    """An explainer item: one concept, plus the post written around it."""

    content_type: Literal[ContentType.EXPLAINER] = ContentType.EXPLAINER
    title: NonEmpty
    audience: AudienceTier
    concept: NonEmpty
    simple_explanation: NonEmpty
    real_life_example: NonEmpty
    why_it_matters: NonEmpty
    try_this: str | None = None
    references: list[SubmittedReference] = Field(default_factory=list)
    post: SubmittedPost


SubmittedItem = Annotated[
    SubmittedPrompt | SubmittedExplainer | SubmittedUseCase | SubmittedResourcePost,
    Field(discriminator="content_type"),
]


class ContentBatch(StrictModel):
    """A batch of editorial-original items returned by a writing session."""

    schema_version: str
    style_version: str
    batch_id: str = Field(min_length=1)
    created_at: UtcDatetime = Field(default_factory=now_utc)
    author: str = Field(default="claude-code", min_length=1)
    items: list[SubmittedItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _versions_and_uniqueness(self) -> Self:
        from ai_news_editor.writing.schema import STYLE_VERSION

        if self.schema_version != CONTENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {self.schema_version!r}; "
                f"expected {CONTENT_SCHEMA_VERSION!r}"
            )
        if self.style_version != STYLE_VERSION:
            raise ValueError(
                f"unsupported style_version {self.style_version!r}; expected {STYLE_VERSION!r}"
            )
        titles = [(item.content_type, item.title) for item in self.items]
        if len(titles) != len(set(titles)):
            duplicates = sorted({t for _c, t in titles if titles.count((_c, t)) > 1})
            raise ValueError(f"an item appears more than once: {', '.join(duplicates)}")
        return self


__all__ = [
    "CONTENT_SCHEMA_VERSION",
    "ContentBatch",
    "SubmittedEvidence",
    "SubmittedExplainer",
    "SubmittedItem",
    "SubmittedMedia",
    "SubmittedPost",
    "SubmittedPrompt",
    "SubmittedReference",
    "SubmittedResource",
    "SubmittedResourcePost",
    "SubmittedUseCase",
]
