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
    ContentOrigin,
    ContentType,
    DraftStatus,
    DuplicateReason,
    EditorialDecision,
    EvaluatorType,
    FetchOutcome,
    PostFormat,
    PrefilterReason,
    PromptTopic,
    PublicationStatus,
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
    #: Machine-readable source link, kept alongside the rendered attribution line.
    source_url: str | None = None
    post_format: PostFormat | None = None
    style_version: str | None = None
    #: Internal reviewer notes. Never published, and deliberately outside the content
    #: hash: a note must not change what a human is approving.
    writer_notes: tuple[str, ...] = ()
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
    """Stable editorial identity for one piece of publishable content.

    Holds the lifecycle and a pointer to the current version. The pointer is nullable
    only during creation, before the first version exists.

    A draft has exactly one origin. News comes from an ``Article`` somebody else
    published; a prompt or an explainer comes from a ``ContentItem`` this newsroom
    wrote. Both end up here, in the same lifecycle, under the same approval gate —
    writing something ourselves is not a reason to trust it more.
    """

    id: UUID = Field(default_factory=uuid4)
    content_type: ContentType = ContentType.NEWS
    #: Set for NEWS. Null for editorial-original content, which has no article and must
    #: never be given a fabricated one.
    article_id: UUID | None = None
    #: Set for PROMPT and EXPLAINER.
    content_item_id: UUID | None = None
    #: The editorial judgement that authorised writing. A news draft always traces back
    #: to the decision that said the story was worth covering.
    evaluation_id: UUID | None = None
    status: DraftStatus = DraftStatus.DRAFTED
    current_version_id: UUID | None = None
    created_at: UtcDatetime = Field(default_factory=now_utc)
    updated_at: UtcDatetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def _exactly_one_origin(self) -> Self:
        """Mirrors the database CHECK, so a bad draft cannot be built in memory either."""
        if self.content_type is ContentType.NEWS:
            if self.article_id is None:
                raise ValueError("a NEWS draft needs the article it was written from")
            if self.content_item_id is not None:
                raise ValueError("a NEWS draft comes from an article, not a content item")
        else:
            if self.content_item_id is None:
                raise ValueError(
                    f"a {self.content_type.value} draft needs the content item it was "
                    "written from"
                )
            if self.article_id is not None:
                raise ValueError(
                    f"a {self.content_type.value} draft is editorial-original; giving it "
                    "an article would invent provenance it does not have"
                )
            if self.evaluation_id is not None:
                raise ValueError(
                    "editorial-original content has no article to evaluate, so it carries "
                    "no evaluation"
                )
        return self

    @property
    def origin(self) -> ContentOrigin:
        """Where this draft's substance came from."""
        return (
            ContentOrigin.SOURCED_ARTICLE
            if self.content_type is ContentType.NEWS
            else ContentOrigin.EDITORIAL_ORIGINAL
        )


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


class Publication(ImmutableDomainModel):
    """One attempt to send an exact draft version to an exact destination.

    Every attempt is recorded, including the failures and the ones whose outcome was
    never learned. A publications table that only holds successes cannot answer the
    question that matters after a bad night: "did that post go out or not?"

    ``content_hash`` is duplicated here for the same reason it is duplicated on a review
    decision — it records what was actually sent, independently of what the version row
    says today.
    """

    id: UUID = Field(default_factory=uuid4)
    draft_id: UUID
    draft_version_id: UUID
    #: The approval this publication rests on. Publication is not the decision; it is
    #: the consequence of one, and the link makes that auditable.
    review_decision_id: UUID
    content_hash: NonEmptyStr
    #: The destination as configured — "@channel" or a numeric id, as written.
    channel: NonEmptyStr
    status: PublicationStatus
    #: Telegram's message id. Present only for a send known to have succeeded.
    message_id: int | None = None
    #: The chat id Telegram reported back, which may be numeric even when the
    #: destination was configured by @username.
    chat_id: str | None = None
    attempt_no: int = Field(default=1, ge=1)
    #: Why a FAILED or UNCERTAIN attempt ended that way. Redacted before storage.
    failure_reason: str | None = None
    published_at: UtcDatetime | None = None
    created_at: UtcDatetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def _successful_publications_have_proof(self) -> Self:
        """A success must carry Telegram's own evidence that it happened."""
        if self.status is PublicationStatus.SUCCEEDED:
            if self.message_id is None:
                raise ValueError("a SUCCEEDED publication must record a Telegram message id")
            if self.published_at is None:
                raise ValueError("a SUCCEEDED publication must record when it was sent")
        return self


class ContentReference(ImmutableDomainModel):
    """An optional factual reference behind editorial-original content.

    Not a source in the news sense — nothing here was reported by anyone else. This is
    "we checked the pricing page before saying what the free plan includes". Kept
    separate from ``Article`` provenance precisely so the two can never be confused.
    """

    label: NonEmptyStr
    url: NonEmptyStr
    #: What this reference actually supports. A bare link proves nothing about which
    #: claim it was consulted for.
    supports: NonEmptyStr


class PromptBody(ImmutableDomainModel):
    """The structure every prompt post needs.

    The three things a reader must get: what this is for, what to paste, and how to
    make it theirs. A prompt without the third is a magic incantation, which is the
    genre this channel is trying not to be.
    """

    what_you_can_do: NonEmptyStr
    prompt_text: NonEmptyStr
    #: At least one. "Change X to Y" is what turns a demo into something usable.
    customization_tips: tuple[NonEmptyStr, ...] = Field(min_length=1)
    #: Honest compatibility, or nothing. Claiming a prompt works everywhere is a claim
    #: about tools we have not tested, and file upload or browsing is not universal.
    works_with: str | None = None

    @model_validator(mode="after")
    def _the_prompt_must_be_substantial(self) -> Self:
        """A one-word 'prompt' is not a prompt.

        The floor is deliberately low — it catches empty and pathological content, not
        terse-but-real prompts. Judging prompt quality is a reviewer's job.
        """
        if len(self.prompt_text.strip()) < 40:
            raise ValueError(
                "prompt_text is too short to be a usable prompt; it should give the AI "
                "context, a goal and the shape of the answer wanted"
            )
        return self


class ExplainerBody(ImmutableDomainModel):
    """The structure every explainer post needs.

    One concept. The temptation is to explain prompts, agents, tokens and context
    windows in a single post, which produces something nobody finishes reading.
    """

    concept: NonEmptyStr
    simple_explanation: NonEmptyStr
    #: Something from the reader's own life, not another piece of technology.
    real_life_example: NonEmptyStr
    why_it_matters: NonEmptyStr
    #: Optional: one thing to go and try. An explainer that ends in action beats one
    #: that ends in a definition.
    try_this: str | None = None


class ContentItem(ImmutableDomainModel):
    """Editorial-original source material for a prompt or an explainer.

    What ``Article`` is to news, this is to content this newsroom wrote itself. It is
    *not* a draft: it holds the editorial substance, and the Ukrainian post written from
    it still goes through Draft, DraftVersion, human review and the approval gate like
    everything else. Writing something ourselves earns no shortcut.
    """

    id: UUID = Field(default_factory=uuid4)
    content_type: ContentType
    origin: ContentOrigin = ContentOrigin.EDITORIAL_ORIGINAL
    audience: AudienceTier
    #: Working title, for finding it again. The published headline is written later and
    #: is the thing a human actually approves.
    title: NonEmptyStr
    topic: PromptTopic | None = None
    body: PromptBody | ExplainerBody
    references: tuple[ContentReference, ...] = ()
    created_by: NonEmptyStr
    created_at: UtcDatetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def _body_matches_its_type(self) -> Self:
        """The payload has to be the shape the content type promises."""
        if self.content_type is ContentType.PROMPT:
            if not isinstance(self.body, PromptBody):
                raise ValueError("a PROMPT content item needs a prompt body")
            if self.topic is None:
                raise ValueError("a PROMPT content item needs a topic")
        elif self.content_type is ContentType.EXPLAINER:
            if not isinstance(self.body, ExplainerBody):
                raise ValueError("an EXPLAINER content item needs an explainer body")
            if self.topic is not None:
                raise ValueError(
                    "an EXPLAINER is described by its concept, not a prompt topic"
                )
        else:
            raise ValueError(
                f"{self.content_type.value} is sourced from an article, not written as a "
                "content item"
            )
        return self

    @property
    def subject(self) -> str:
        """What this item is about, for a review screen: a topic or a concept."""
        if isinstance(self.body, ExplainerBody):
            return self.body.concept
        return self.topic.value if self.topic else self.title
