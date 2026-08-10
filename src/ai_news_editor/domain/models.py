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
from ai_news_editor.domain.content import compute_content_hash
from ai_news_editor.domain.enums import (
    ArticleStatus,
    AudienceTier,
    Category,
    DraftStatus,
    FetchOutcome,
    ReviewAction,
    SourceKind,
    TrustTier,
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
    content_hash: str | None = None
    duplicate_of_id: UUID | None = None
    status: ArticleStatus = ArticleStatus.COLLECTED
    filtered_by: str | None = None
    created_at: UtcDatetime = Field(default_factory=now_utc)
    updated_at: UtcDatetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def _duplicate_is_not_self(self) -> Self:
        if self.duplicate_of_id is not None and self.duplicate_of_id == self.id:
            raise ValueError("an article cannot be a duplicate of itself")
        return self


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
