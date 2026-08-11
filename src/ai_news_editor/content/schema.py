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
    PostFormat,
    PromptTopic,
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


class SubmittedPrompt(StrictModel):
    """A prompt item: the structure plus the post written around it."""

    content_type: Literal[ContentType.PROMPT] = ContentType.PROMPT
    title: NonEmpty
    audience: AudienceTier
    topic: PromptTopic
    what_you_can_do: NonEmpty
    prompt_text: NonEmpty
    customization_tips: list[NonEmpty] = Field(min_length=1, max_length=6)
    works_with: str | None = None
    references: list[SubmittedReference] = Field(default_factory=list)
    post: SubmittedPost

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
    SubmittedPrompt | SubmittedExplainer, Field(discriminator="content_type")
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
    "SubmittedExplainer",
    "SubmittedItem",
    "SubmittedPost",
    "SubmittedPrompt",
    "SubmittedReference",
]
