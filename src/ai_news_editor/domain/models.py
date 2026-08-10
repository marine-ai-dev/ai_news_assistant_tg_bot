"""Core domain entities.

Two modelling decisions carry most of the weight here:

*Article vs Draft* — an :class:`Article` is source material (an editorial candidate);
a :class:`Draft` is publishable content produced from it. Publishing acts on drafts.

*Draft vs DraftVersion* — a :class:`Draft` is a stable identity with a lifecycle;
a :class:`DraftVersion` is immutable content. Editing never mutates a version, it
appends a new one. This is what allows an approval to name the exact bytes a human
read, and it is why ``DraftVersion`` is frozen and its hash is computed rather than
supplied.
"""

from __future__ import annotations

from typing import Annotated, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from ai_news_editor.domain.clock import UtcDatetime, now_utc
from ai_news_editor.domain.content import compute_content_hash, compute_editorial_fingerprint
from ai_news_editor.domain.enums import (
    ArticleStatus,
    AudienceTier,
    Category,
    DraftStatus,
    DuplicateReason,
    EditorialDecision,
    EvaluatorType,
    FetchOutcome,
    PrefilterReason,
    ReviewAction,
    SourceKind,
    TrustTier,
    VerificationStatus,
)

NonEmptyStr = Annotated[str, Field(min_length=1)]


class DomainModel(BaseModel):
    """Shared configuration: unknown fields are a bug, not something to tolerate."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ImmutableDomainModel(DomainModel):
    """Base for entities that are append-only by design."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(DomainModel):
    """A configured origin of news items.

    Sources are declared in configuration and mirrored into the database so that every
    collected item keeps a durable reference to where it came from, even if the config
    file later changes.
    """

    id: NonEmptyStr
    name: NonEmptyStr
    kind: SourceKind
    url: NonEmptyStr
    trust_tier: TrustTier
    signal_only: bool = False
    enabled: bool = True
    language: str = "en"
    publisher: str | None = None
    poll_interval_minutes: int = Field(default=60, ge=1)
    editorial_role: str | None = Field(
        default=None,
        description="Why this source is in the mix and what stories it is expected to supply.",
    )
    tags: tuple[str, ...] = ()
    config: dict[str, object] = Field(default_factory=dict)
    created_at: UtcDatetime = Field(default_factory=now_utc)
    updated_at: UtcDatetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def _community_sources_are_signal_only(self) -> Self:
        """Community chatter may indicate attention but never establishes fact."""
        if self.trust_tier is TrustTier.COMMUNITY_SIGNAL and not self.signal_only:
            raise ValueError("COMMUNITY_SIGNAL sources must be marked signal_only")
        return self


class RawItem(ImmutableDomainModel):
    """Exactly what a source gave us, kept verbatim.

    This is the provenance anchor: any published post can be traced back to the bytes
    that produced it. Never updated after insert.
    """

    id: UUID = Field(default_factory=uuid4)
    source_id: NonEmptyStr
    external_id: str | None = None
    title_original: str | None = None
    url_original: NonEmptyStr
    author: str | None = None
    published_at: UtcDatetime | None = None
    fetched_at: UtcDatetime = Field(default_factory=now_utc)
    summary_raw: str | None = None
    content_raw: str | None = None
    payload_raw: str
    content_type: str = "application/octet-stream"
    fetch_run_id: str | None = None


class Article(DomainModel):
    """A normalized editorial candidate derived from one raw item."""

    id: UUID = Field(default_factory=uuid4)
    raw_item_id: UUID
    source_id: NonEmptyStr
    title: NonEmptyStr
    canonical_url: NonEmptyStr
    clean_text: str | None = None
    language: str | None = None
    published_at: UtcDatetime | None = None
    #: Fingerprint of normalized title + body. Exact-duplicate detection (layer 1).
    content_hash: str | None = None
    #: Fingerprint of the normalized title alone, for same-source repeats.
    title_fingerprint: str | None = None
    #: 64-bit simhash of the text, for near-duplicate detection (layer 2).
    simhash: int | None = None
    duplicate_of_id: UUID | None = None
    duplicate_reason: DuplicateReason | None = None
    #: A conservative cross-source match. Recorded, never acted on: secondary reporting
    #: is kept because it is useful for corroboration later, not deleted as redundant.
    possible_duplicate_of_id: UUID | None = None
    status: ArticleStatus = ArticleStatus.COLLECTED
    filtered_by: PrefilterReason | None = None
    normalized_at: UtcDatetime | None = None
    created_at: UtcDatetime = Field(default_factory=now_utc)
    updated_at: UtcDatetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def _duplicate_is_not_self(self) -> Self:
        if self.duplicate_of_id is not None and self.duplicate_of_id == self.id:
            raise ValueError("an article cannot be a duplicate of itself")
        if self.possible_duplicate_of_id is not None and self.possible_duplicate_of_id == self.id:
            raise ValueError("an article cannot be a possible duplicate of itself")
        return self


class DuplicateCandidate(ImmutableDomainModel):
    """An already-stored article reduced to the features duplicate detection needs.

    A projection rather than a full :class:`Article`: comparing candidates never needs
    the body text, and loading it for every neighbour would be wasteful. Lives in the
    domain so storage and pipeline can both use it without depending on each other.
    """

    id: UUID
    source_id: NonEmptyStr
    canonical_url: NonEmptyStr
    trust_tier: TrustTier
    content_hash: str | None = None
    title_fingerprint: str | None = None
    simhash: int | None = None
    published_at: UtcDatetime | None = None
    text_length: int = 0


class Evaluation(ImmutableDomainModel):
    """One editorial judgement about one article, at one point in time.

    Append-only history. An article can be evaluated again — under a newer rubric, or
    after its content changes — and every earlier judgement remains queryable. Nothing
    here is publishable content: an evaluation says a story is *worth covering*, which
    is a different thing from a draft, and a long way from an approval.
    """

    id: UUID = Field(default_factory=uuid4)
    article_id: UUID
    schema_version: NonEmptyStr
    rubric_version: NonEmptyStr
    evaluator_type: EvaluatorType
    evaluator: str | None = None
    batch_id: str | None = None
    #: The content state that was actually reviewed. See :mod:`editorial.schema`.
    content_fingerprint: NonEmptyStr
    decision: EditorialDecision
    category: Category
    audience: AudienceTier
    scores: dict[str, int]
    #: Derived by Python from ``scores``; the evaluator never supplies it.
    composite_score: float
    verification_status: VerificationStatus
    verification_sources: tuple[dict[str, str], ...] = ()
    why_selected: tuple[str, ...] = ()
    editorial_angle: str | None = None
    notes: str | None = None
    created_at: UtcDatetime = Field(default_factory=now_utc)

    def is_current_for(self, article: Article, excerpt: str | None) -> bool:
        """Whether this judgement still describes the content it would be shown today."""
        return self.content_fingerprint == compute_editorial_fingerprint(
            title=article.title,
            canonical_url=article.canonical_url,
            excerpt=excerpt,
            published_at=article.published_at.isoformat() if article.published_at else None,
        )


class DraftVersion(ImmutableDomainModel):
    """One immutable snapshot of publishable content.

    ``content_hash`` is computed, not stored as an input field, so a version whose hash
    disagrees with its text is unrepresentable.
    """

    id: UUID = Field(default_factory=uuid4)
    draft_id: UUID
    version_no: int = Field(ge=1)
    title: NonEmptyStr
    body: NonEmptyStr
    hashtags: tuple[str, ...] = ()
    category: Category
    audience: AudienceTier
    source_attribution: NonEmptyStr
    created_by: NonEmptyStr
    created_at: UtcDatetime = Field(default_factory=now_utc)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        """Fingerprint of everything a reviewer sees. See :mod:`.content`."""
        return compute_content_hash(
            title=self.title,
            body=self.body,
            hashtags=self.hashtags,
            category=self.category.value,
            audience=self.audience.value,
            source_attribution=self.source_attribution,
        )


class Draft(DomainModel):
    """Stable editorial identity for one article's publishable content.

    Holds the lifecycle and a pointer to the current version. The pointer is nullable
    only during creation, before the first version exists.
    """

    id: UUID = Field(default_factory=uuid4)
    article_id: UUID
    status: DraftStatus = DraftStatus.DRAFTED
    current_version_id: UUID | None = None
    created_at: UtcDatetime = Field(default_factory=now_utc)
    updated_at: UtcDatetime = Field(default_factory=now_utc)


class CommunitySignal(ImmutableDomainModel):
    """Evidence that a community is discussing a story.

    Attention metadata, never provenance. A signal can say "people are talking about
    this", which may make a story worth investigating or indicate virality — it can
    never establish that a claim is true, and it never becomes article content.
    Signals live in their own table precisely so they cannot be mistaken for sources.

    Counts are a snapshot from when the signal was first observed; ingestion identity
    prevents the same discussion being re-recorded, so scores do not track upward.
    """

    id: UUID = Field(default_factory=uuid4)
    source_id: NonEmptyStr
    external_id: NonEmptyStr
    #: Set when the linked URL matches a collected article. Null means we saw the
    #: discussion but hold no article for it — kept for Phase 4 rather than discarded.
    article_id: UUID | None = None
    canonical_url: str | None = None
    title: str | None = None
    points: int | None = None
    num_comments: int | None = None
    author: str | None = None
    posted_at: UtcDatetime | None = None
    discussion_url: str | None = None
    observed_at: UtcDatetime = Field(default_factory=now_utc)


class SourceFetchState(DomainModel):
    """HTTP caching validators and the outcome of the last fetch attempt.

    Kept separate from :class:`Source` because it is operational bookkeeping that
    changes on every run, while a source's definition comes from configuration.
    """

    source_id: NonEmptyStr
    etag: str | None = None
    last_modified: str | None = None
    last_attempt_at: UtcDatetime | None = None
    last_success_at: UtcDatetime | None = None
    last_outcome: FetchOutcome | None = None
    last_http_status: int | None = None
    last_error: str | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    updated_at: UtcDatetime = Field(default_factory=now_utc)


class ReviewDecision(ImmutableDomainModel):
    """An append-only record of one human action on one draft version.

    ``content_hash`` is a snapshot of what the human actually read, duplicated here on
    purpose: it must remain correct even if the version row is somehow reached by a
    future migration.
    """

    id: UUID = Field(default_factory=uuid4)
    draft_id: UUID
    draft_version_id: UUID
    content_hash: NonEmptyStr
    action: ReviewAction
    actor: NonEmptyStr
    note: str | None = None
    created_at: UtcDatetime = Field(default_factory=now_utc)
