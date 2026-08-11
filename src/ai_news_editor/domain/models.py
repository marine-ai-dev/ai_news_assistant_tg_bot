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
    EvidenceKind,
    EvidenceStatus,
    FetchOutcome,
    MediaOrigin,
    MediaRole,
    PostFormat,
    PrefilterReason,
    PromptPlacement,
    PromptRepresentation,
    PromptTopic,
    PublicationStatus,
    ResourceType,
    ReviewAction,
    SourceKind,
    SourceTier,
    TrustTier,
    UseCaseTheme,
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


class MediaAsset(ImmutableDomainModel):
    """One image, screenshot or file that belongs to a post.

    Identity only — role, where it came from, and how to find it. Deliberately no size,
    no modification time, no absolute path: none of those change what a reader receives,
    and hashing them would make an approval expire because a file was touched.

    ``origin`` is never inferred. Reusing somebody else's image without knowing it is
    theirs is how a channel acquires a complaint, so a source image is recorded by URL
    and left where it is rather than downloaded and republished.
    """

    role: MediaRole
    origin: MediaOrigin
    #: Where the asset is: a local path relative to the media directory for our own
    #: files, or a URL for source media we are pointing at rather than reusing.
    reference: NonEmptyStr
    #: What it shows. Doubles as alt text and as the line a reviewer reads.
    description: NonEmptyStr
    #: For OWNER_GENERATED: what made it. Recorded as the tool names itself.
    tool_used: str | None = None
    #: Only when actually known. A model version is exactly the detail that feels safe
    #: to infer and is wrong six months later.
    model_version: str | None = None
    generated_at: UtcDatetime | None = None
    #: For SOURCE_MEDIA: the page it belongs to.
    source_url: str | None = None

    @model_validator(mode="after")
    def _owner_generated_media_says_what_made_it(self) -> Self:
        if self.origin is MediaOrigin.OWNER_GENERATED and not self.tool_used:
            raise ValueError(
                "owner-generated media must record the tool that produced it; "
                "'we made this with AI' is not a provenance record"
            )
        if self.origin is MediaOrigin.SOURCE_MEDIA and not self.source_url:
            raise ValueError("source media must record the page it belongs to")
        return self

    def identity(self) -> dict[str, str]:
        """What the approval hash covers. Stable across file-system noise."""
        return {
            "role": self.role.value,
            "origin": self.origin.value,
            "reference": self.reference,
        }


class ResourceSpec(ImmutableDomainModel):
    """A downloadable or curated thing a RESOURCE post is built around."""

    resource_type: ResourceType
    title: NonEmptyStr
    description: NonEmptyStr
    #: The file, when there is one. A curated list may have none — the post is the
    #: resource — and that is a legitimate state rather than a missing field.
    asset: MediaAsset | None = None
    version: str | None = None

    def identity(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.resource_type.value,
            "title": self.title,
        }
        if self.asset is not None:
            payload["asset"] = self.asset.identity()
        if self.version:
            payload["version"] = self.version
        return payload


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

    # -- the publication bundle (Phase 8.2) ---------------------------------
    #
    # A post stopped being only text. Everything below is part of what a human approves,
    # which is why it is hashed: approving the text and then changing the comment, the
    # image or the footer would be approval of something nobody read.

    #: Where the prompt lives, for content that has one.
    prompt_placement: PromptPlacement = PromptPlacement.NONE
    #: The first comment, published with the post. Approved together with it, never
    #: written afterwards.
    comment_text: str | None = None
    media: tuple[MediaAsset, ...] = ()
    resource: ResourceSpec | None = None
    #: The channel call-to-action, frozen at creation. Stored rather than rendered from
    #: configuration at send time, so a config change cannot alter an approved post.
    footer_text: str | None = None

    created_by: NonEmptyStr
    created_at: UtcDatetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def _a_comment_prompt_needs_a_comment(self) -> Self:
        if self.prompt_placement is PromptPlacement.COMMENT and not (
            self.comment_text and self.comment_text.strip()
        ):
            raise ValueError(
                "prompt_placement is COMMENT but there is no comment text; the post "
                "would promise a prompt that does not exist"
            )
        return self

    def bundle(self) -> dict[str, object]:
        """The approval-relevant publication payload beyond the post text."""
        payload: dict[str, object] = {}
        if self.prompt_placement is not PromptPlacement.NONE:
            payload["prompt_placement"] = self.prompt_placement.value
        if self.comment_text:
            payload["comment_text"] = self.comment_text
        if self.media:
            payload["media"] = [asset.identity() for asset in self.media]
        if self.resource is not None:
            payload["resource"] = self.resource.identity()
        if self.footer_text:
            payload["footer_text"] = self.footer_text
        return payload

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        """Fingerprint of everything a reviewer sees. See :mod:`.content`.

        A version with no bundle content hashes exactly as it did before Phase 8.2,
        which is what keeps the already-published post's approval verifiable.
        """
        return compute_content_hash(
            title=self.title,
            body=self.body,
            hashtags=self.hashtags,
            category=self.category.value,
            audience=self.audience.value,
            source_attribution=self.source_attribution,
            bundle=self.bundle(),
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


class PromptEvidence(ImmutableDomainModel):
    """Where a prompt came from, and what was actually observed when someone ran it.

    Every field answers a question a reviewer would otherwise have to take on trust:
    who tried this, with what tool, what did they ask, what happened, and what did they
    say did *not* work. Structured rather than a prose blob because the review screen
    shows them individually and because a missing field should be a validation error,
    not a paragraph that quietly omits something.

    Nothing here may be inferred. If a source says "ChatGPT", ``tool`` is "ChatGPT" —
    not GPT-5, not GPT-4o. Guessing a model version is inventing evidence.
    """

    source_url: NonEmptyStr
    source_title: NonEmptyStr
    source_tier: SourceTier
    #: Where it was found — Reddit, Threads, X, YouTube, a blog. Social posts vanish;
    #: recording the platform and handle preserves what was reviewed without copying
    #: somebody's whole post into our database.
    source_platform: str | None = None
    source_author: str | None = None
    #: Who ran it, as the source identifies them: an author, a company, "Reddit user
    #: u/…". Not a guess, and not "the internet".
    tested_by: NonEmptyStr
    #: The product named by the source, verbatim.
    tool_used: NonEmptyStr
    #: Only when the source states it. A model version is exactly the kind of detail
    #: that feels safe to infer and is wrong a year later.
    model_version: str | None = None
    what_was_tested: NonEmptyStr
    observed_result: NonEmptyStr
    #: What the source said did not work, or where it stopped. Empty when the source
    #: genuinely mentioned none — an honest absence, recorded as one.
    limitations: tuple[NonEmptyStr, ...] = ()
    #: Features the workflow depends on: file upload, web search, a paid plan. A
    #: NEWCOMER told to upload a PDF needs to know their plan may not allow it.
    requires: tuple[NonEmptyStr, ...] = ()
    #: When we last looked at the source. Prompt behaviour changes under people.
    checked_at: UtcDatetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def _the_url_is_a_real_link(self) -> Self:
        if not self.source_url.startswith(("http://", "https://")):
            raise ValueError(
                f"source_url must be an http(s) link, got {self.source_url!r}; a prompt "
                "without a findable source is not publishable"
            )
        return self


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
    #: How this prompt relates to the one in the source. An adapted prompt is never
    #: presented as a quotation.
    representation: PromptRepresentation = PromptRepresentation.ADAPTED

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


class UseCaseBody(ImmutableDomainModel):
    """What somebody actually did with AI, and what came of it.

    The difference from :class:`PromptBody` is where the value sits. A prompt post gives
    the reader something to copy; a use case gives them something to recognise — *oh,
    you can do that* — and the prompt, if there is one at all, is a detail.
    """

    what_the_person_did: NonEmptyStr
    reported_benefit: NonEmptyStr
    #: How a reader could try the same thing. Not a promise that it will work for them.
    how_to_try: tuple[NonEmptyStr, ...] = Field(min_length=1)
    #: Present only when the source actually showed one.
    prompt_text: str | None = None


class ResourceBody(ImmutableDomainModel):
    """A curated or downloadable thing the post is built around."""

    spec: ResourceSpec
    #: What the reader gets out of it, in their terms.
    what_it_gives_you: NonEmptyStr
    how_to_use: tuple[NonEmptyStr, ...] = Field(min_length=1)


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
    #: For TESTED_USE_CASE: the area of life it belongs to.
    use_case_theme: UseCaseTheme | None = None
    body: PromptBody | ExplainerBody | UseCaseBody | ResourceBody
    #: What kind of act produced the evidence. A vendor demo and one person's Reddit
    #: post are both honest, and they are not the same claim.
    evidence_kind: EvidenceKind | None = None
    #: Optional grouping across items — "7 днів AI-креативів", day 3. Metadata, not a
    #: campaign system: nothing schedules or sequences on it.
    series_name: str | None = None
    series_order: int | None = Field(default=None, ge=1)
    references: tuple[ContentReference, ...] = ()
    #: Prompts only. The demonstration this post rests on.
    evidence: PromptEvidence | None = None
    #: Whether that evidence is good enough to publish. A prompt concept: ``None`` for
    #: an explainer, which is editorial-original by design and says so.
    evidence_status: EvidenceStatus | None = None
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
            if self.evidence_status is None:
                raise ValueError(
                    "a PROMPT needs an evidence status; if its provenance is unknown, "
                    "say so with LEGACY_UNVERIFIED rather than leaving it blank"
                )
            if (
                self.evidence_status is EvidenceStatus.VERIFIED_SOURCE_BACKED
                and self.evidence is None
            ):
                raise ValueError(
                    "a PROMPT cannot be marked source-backed without the evidence that "
                    "backs it"
                )
        elif self.content_type is ContentType.EXPLAINER:
            if not isinstance(self.body, ExplainerBody):
                raise ValueError("an EXPLAINER content item needs an explainer body")
            if self.topic is not None:
                raise ValueError(
                    "an EXPLAINER is described by its concept, not a prompt topic"
                )
            if self.evidence is not None:
                raise ValueError(
                    "an EXPLAINER carries references, not a tested-prompt demonstration"
                )
            if self.evidence_status is not None:
                raise ValueError(
                    "evidence status is a PROMPT concept; an EXPLAINER is editorial-"
                    "original by design"
                )
        elif self.content_type is ContentType.TESTED_USE_CASE:
            if not isinstance(self.body, UseCaseBody):
                raise ValueError("a TESTED_USE_CASE needs a use-case body")
            if self.evidence is None:
                raise ValueError(
                    "a TESTED_USE_CASE reports something somebody did; without the "
                    "evidence it is just a story we made up"
                )
            if self.evidence_status is None:
                raise ValueError("a TESTED_USE_CASE needs an evidence status")
        elif self.content_type is ContentType.RESOURCE:
            if not isinstance(self.body, ResourceBody):
                raise ValueError("a RESOURCE needs a resource body")
            if self.evidence is not None:
                raise ValueError(
                    "a RESOURCE is curated material, not a report of a tested workflow"
                )
        else:
            raise ValueError(
                f"{self.content_type.value} is sourced from an article, not written as a "
                "content item"
            )

        if self.use_case_theme is not None and (
            self.content_type is not ContentType.TESTED_USE_CASE
        ):
            raise ValueError("a use-case theme belongs to a TESTED_USE_CASE")
        if (self.series_order is None) != (self.series_name is None):
            raise ValueError("a series needs both a name and a position, or neither")
        return self

    @property
    def subject(self) -> str:
        """What this item is about, for a review screen."""
        if isinstance(self.body, ExplainerBody):
            return self.body.concept
        if isinstance(self.body, ResourceBody):
            return self.body.spec.resource_type.value
        if self.use_case_theme is not None:
            return self.use_case_theme.value
        return self.topic.value if self.topic else self.title

    @property
    def series_label(self) -> str | None:
        """"7 днів AI-креативів · 3" — for a review screen, when there is a series."""
        if self.series_name is None or self.series_order is None:
            return None
        return f"{self.series_name} · {self.series_order}"
