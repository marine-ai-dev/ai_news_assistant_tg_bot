"""The editorial exchange contract.

Two documents cross the boundary between the deterministic Python pipeline and the
editorial layer:

* :class:`EditorialBatch` — candidates going out for review.
* :class:`ReviewedBatch` — decisions and scores coming back.

**This schema is the seam.** Today a Claude Code session reads the batch and writes the
reviews. A future automated evaluator could take the same batch and produce the same
reviewed document, and nothing in SQLite or the pipeline would need to change. Keeping
the contract explicit and versioned is what makes that swap cheap.

Everything is validated strictly, in both directions. Editorial output is *data*: it is
checked against enums, ranges and gates before it is allowed anywhere near storage, and
it cannot express approval or publication at all.
"""

from __future__ import annotations

from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_news_editor.domain.clock import UtcDatetime, now_utc
from ai_news_editor.domain.enums import (
    SENSITIVE_CATEGORIES,
    AudienceTier,
    Category,
    EditorialDecision,
    TrustTier,
    VerificationStatus,
)
from ai_news_editor.editorial.rubric import (
    DIMENSIONS,
    RUBRIC_VERSION,
    SCHEMA_VERSION,
    VERIFYING_TIERS,
    gate_failure_reason,
    passes_credibility_gate,
    verification_is_sufficient,
)

Score = Annotated[int, Field(ge=0, le=100)]

#: How much article text a reviewer receives. Enough to judge a story, short enough to
#: keep a 20-article batch readable in one pass. Truncation is always flagged on the
#: item so a thin excerpt is never mistaken for a thin story.
EXCERPT_CHAR_LIMIT = 1200
EXCERPT_TRUNCATION_MARKER = " […]"


class StrictModel(BaseModel):
    """Unknown fields are a bug in the producer, never something to tolerate."""

    model_config = ConfigDict(extra="forbid")


# --- outbound: candidates for review ---------------------------------------


class BatchSource(StrictModel):
    """Where a candidate came from, and how much weight that origin carries."""

    id: str
    name: str
    trust_tier: TrustTier
    editorial_role: str | None = None
    #: True for community sources. Such an item is attention, not evidence.
    signal_only: bool = False


class CommunityAttention(StrictModel):
    """Signals that people are discussing this story. Never evidence that it is true."""

    hacker_news_points: int | None = None
    hacker_news_comments: int | None = None
    hacker_news_url: str | None = None


class BatchArticle(StrictModel):
    """One candidate story as presented for review."""

    article_id: UUID
    source: BatchSource
    title: str
    canonical_url: str
    published_at: str | None = None
    excerpt: str | None = None
    #: Set when ``excerpt`` is shorter than the stored text. A reviewer seeing this
    #: knows the story may have more to it than the words in front of them.
    excerpt_truncated: bool = False
    excerpt_chars: int = 0
    community: CommunityAttention | None = None
    content_fingerprint: str


class EditorialBatch(StrictModel):
    """A set of candidates exported for editorial review."""

    schema_version: str = SCHEMA_VERSION
    rubric_version: str = RUBRIC_VERSION
    batch_id: str
    generated_at: UtcDatetime = Field(default_factory=now_utc)
    article_count: int = 0
    articles: list[BatchArticle]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {self.schema_version!r}; expected {SCHEMA_VERSION!r}"
            )
        ids = [article.article_id for article in self.articles]
        if len(ids) != len(set(ids)):
            raise ValueError("an article appears more than once in the batch")
        object.__setattr__(self, "article_count", len(self.articles))
        return self


# --- inbound: editorial decisions -------------------------------------------


class VerificationSource(StrictModel):
    """A source consulted while checking a claim."""

    url: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    source_type: TrustTier

    @model_validator(mode="after")
    def _must_be_able_to_verify(self) -> Self:
        if self.source_type not in VERIFYING_TIERS:
            raise ValueError(
                f"{self.source_type.value} cannot serve as verification; only "
                f"{', '.join(sorted(t.value for t in VERIFYING_TIERS))} qualify"
            )
        return self


class Scores(StrictModel):
    """The nine rubric dimensions, each 0–100."""

    credibility: Score
    general_ai_relevance: Score
    reader_interest: Score
    usefulness: Score
    novelty: Score
    wow_factor: Score
    virality_potential: Score
    accessibility: Score
    consumer_impact: Score

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in DIMENSIONS}


class ArticleReview(StrictModel):
    """One editorial decision about one candidate."""

    article_id: UUID
    content_fingerprint: str = Field(min_length=1)
    decision: EditorialDecision
    category: Category
    audience: AudienceTier
    scores: Scores
    verification_status: VerificationStatus
    verification_sources: list[VerificationSource] = Field(default_factory=list, max_length=10)
    why_selected: list[str] = Field(default_factory=list, max_length=8)
    #: The angle a Phase-5 writer would take. Not a headline.
    editorial_angle: str | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _decision_is_coherent(self) -> Self:
        scores = self.scores.as_dict()

        if self.decision is EditorialDecision.SHORTLIST:
            if not passes_credibility_gate(scores):
                raise ValueError(gate_failure_reason(scores))
            if not self.why_selected:
                raise ValueError("a SHORTLIST review must say why it was selected")
            if not (self.editorial_angle or "").strip():
                raise ValueError("a SHORTLIST review must supply an editorial_angle")

        problem = verification_is_sufficient(
            self.decision,
            self.verification_status,
            sensitive=self.category in SENSITIVE_CATEGORIES,
            corroborating_sources=len(self.verification_sources),
        )
        if problem:
            raise ValueError(problem)
        return self


class ReviewedBatch(StrictModel):
    """Editorial decisions returned for a batch."""

    schema_version: str
    rubric_version: str
    batch_id: str = Field(min_length=1)
    reviewed_at: UtcDatetime = Field(default_factory=now_utc)
    reviewer: str = Field(default="claude-code", min_length=1)
    reviews: list[ArticleReview] = Field(min_length=1)

    @model_validator(mode="after")
    def _versions_and_uniqueness(self) -> Self:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {self.schema_version!r}; expected {SCHEMA_VERSION!r}"
            )
        if self.rubric_version != RUBRIC_VERSION:
            raise ValueError(
                f"unsupported rubric_version {self.rubric_version!r}; expected {RUBRIC_VERSION!r}"
            )
        ids = [review.article_id for review in self.reviews]
        if len(ids) != len(set(ids)):
            duplicates = sorted({str(i) for i in ids if ids.count(i) > 1})
            raise ValueError(f"an article is reviewed more than once: {', '.join(duplicates)}")
        return self
