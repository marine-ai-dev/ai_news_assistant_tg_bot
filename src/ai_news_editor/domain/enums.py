"""Controlled vocabularies for the editorial domain.

Article and draft lifecycles are deliberately kept as *separate* enums: an article is
source material moving through triage, while a draft is publishable content moving
towards the channel. Conflating them would make it impossible to have several drafts
of one article in different publication states.
"""

from __future__ import annotations

from enum import StrEnum


class SourceKind(StrEnum):
    """Which adapter implementation knows how to read a source.

    Adapters themselves arrive in Phase 2; the vocabulary is needed now because a
    persisted source row must declare its kind.
    """

    RSS = "RSS"
    HTML_CHANGELOG = "HTML_CHANGELOG"
    HN_SIGNAL = "HN_SIGNAL"


class TrustTier(StrEnum):
    """How much evidential weight a source's claims may carry.

    ``COMMUNITY_SIGNAL`` sources indicate attention, never fact; the editorial layer
    (Phase 4) refuses to treat them as the sole basis for a claim.
    """

    OFFICIAL = "OFFICIAL"
    REPUTABLE_SECONDARY = "REPUTABLE_SECONDARY"
    COMMUNITY_SIGNAL = "COMMUNITY_SIGNAL"
    UNVERIFIED = "UNVERIFIED"


class ContentCapability(StrEnum):
    """What a source's items may eventually feed into, editorially.

    Source registry metadata for a future content classifier — a source listing
    ``PROMPT_WORKFLOW`` does not mean anything extracts prompts from it today. The
    live automation pipeline is NEWS-only and reads none of this; it exists so a later
    phase does not have to re-derive "what is this source good for" from scratch.
    """

    NEWS = "NEWS"
    AI_TOOL = "AI_TOOL"
    FREE_DEAL = "FREE_DEAL"
    AI_LIFEHACK = "AI_LIFEHACK"
    PROMPT_WORKFLOW = "PROMPT_WORKFLOW"
    EXPLAINER = "EXPLAINER"
    RESEARCH = "RESEARCH"
    WEEKLY_DIGEST_INPUT = "WEEKLY_DIGEST_INPUT"


class MediaPolicy(StrEnum):
    """How conservatively a source's media may be used, once media handling exists.

    Never inferred from "it's on an official page" — reuse permission has to be
    explicit and traceable, not assumed. Registry metadata only: nothing downloads,
    compresses or reuploads media yet.
    """

    #: Default. No image/video from this source is used in any form.
    NO_MEDIA = "NO_MEDIA"
    #: Telegram's own link-preview card may show the source's page image. Nothing is
    #: downloaded or re-hosted by this application.
    LINK_PREVIEW_ONLY = "LINK_PREVIEW_ONLY"
    #: A future phase may look for media on this source's pages, but reuse permission
    #: is still unresolved — discovery only, not a green light to republish.
    DISCOVER_MEDIA = "DISCOVER_MEDIA"
    #: The source has stated, checkable licensing/reuse terms this application can
    #: point to. Not the default for any source added in this step.
    EXPLICIT_REUSE_ALLOWED = "EXPLICIT_REUSE_ALLOWED"


class SourcePriority(StrEnum):
    """Coarse ranking metadata for a future editorial diversity/ranking pass.

    Deliberately coarse — not a numeric score. ``TrustTier`` says how much a claim can
    be trusted; this says how much weight a source should carry when choosing among
    several eligible stories, which is a different question the current automation
    pipeline does not yet ask.
    """

    PRIMARY_HIGH = "PRIMARY_HIGH"
    PRIMARY_NORMAL = "PRIMARY_NORMAL"
    DISCOVERY = "DISCOVERY"
    COMMUNITY = "COMMUNITY"


class FulltextPolicy(StrEnum):
    """Whether a source's items are expected to need or support a fulltext fetch.

    ``DISCOVERY_ONLY`` sources (community signal sources today) never reach
    ``sources.fulltext.fetch_fulltext`` at all — process.py routes their items to a
    ``CommunitySignal`` instead of normalizing them into articles. This field records
    that fact as registry metadata rather than leaving it implicit in ``signal_only``.
    """

    NORMAL_ATTEMPT = "NORMAL_ATTEMPT"
    DISCOVERY_ONLY = "DISCOVERY_ONLY"


class EditorialCategory(StrEnum):
    """What kind of post this is, editorially — Step 3 (AI News Agent v2).

    Deliberately a fourth taxonomy, not a rename of an existing one:

    * ``ContentType`` (Phase 1) is a *format* split — NEWS (article-sourced) vs.
      PROMPT/EXPLAINER/TESTED_USE_CASE/RESOURCE (editorial-original, from a
      ``ContentItem``). Every value below is still article-sourced, i.e. still
      ``ContentType.NEWS`` at that level — this is a finer split *within* it.
    * ``Category`` (Phase 4, ``evaluations.category`` / ``draft_versions.category``) is
      editorial *tone* (WOW, TRENDING, USEFUL_TOOL, ...), shown to readers and driving
      the Telegram headline emoji. Orthogonal to this: a NEWS post can be any tone.
    * ``ContentCapability`` (Step 2) is *source* metadata — what a source's items may
      eventually feed into. This is what a specific post actually *is*, decided at
      selection time, constrained by its source's ``ContentCapability`` list.

    Every value here is still resolved through ``evaluations.editorial_category``,
    which is deliberately unconstrained TEXT at the database level (see migration 014)
    — this enum is where the real constraint lives, exactly like ``Category`` already
    works.
    """

    NEWS = "NEWS"
    AI_TOOL = "AI_TOOL"
    FREE_DEAL = "FREE_DEAL"
    AI_LIFEHACK = "AI_LIFEHACK"
    PROMPT_WORKFLOW = "PROMPT_WORKFLOW"
    EXPLAINER = "EXPLAINER"
    RESEARCH = "RESEARCH"
    #: Supported end to end (schema, validation, rendering) but never scheduled
    #: automatically — see automation/pipeline.py and docs/editorial.md. A future step
    #: decides publishing cadence; this step only makes the type real.
    WEEKLY_DIGEST = "WEEKLY_DIGEST"


class EditorialEvidence(StrEnum):
    """What kind of evidence backs a post's claims — Step 3.

    A different axis from the existing :class:`EvidenceKind` (Phase 8.1, "what act
    produced a PROMPT's evidence" — OFFICIAL_TEST/THIRD_PARTY_DEMO/...): this classifies
    the *source* behind an automated candidate's claims, not how a human-authored
    prompt was tested. Named distinctly on purpose so the two are never confused.

    The distinction ``AI_LIFEHACK`` exists to enforce: a Tier C community source can
    supply ``USER_REPORTED`` or ``COMMUNITY_DISCUSSION`` evidence, never
    ``PRIMARY_SOURCE`` — see ``sources.capability.allowed_evidence``. A post's evidence
    type is what a validator checks before letting an anecdote read like a verified fact.
    """

    #: The vendor's own official statement — an OFFICIAL-tier source's own words.
    PRIMARY_SOURCE = "PRIMARY_SOURCE"
    #: Independent journalism reporting on something, not the thing itself.
    REPUTABLE_SECONDARY = "REPUTABLE_SECONDARY"
    #: One person's account of what happened to them. Never generalized into "AI does
    #: X" — only "this person reports AI did X for them".
    USER_REPORTED = "USER_REPORTED"
    #: Community attention/reaction with no single identifiable claimant — a thread's
    #: general reaction, not one person's story.
    COMMUNITY_DISCUSSION = "COMMUNITY_DISCUSSION"
    #: A named paper or research report, distinct from a company's own marketing
    #: framing of that same result — see docs/editorial.md's RESEARCH claim framing.
    RESEARCH_PAPER = "RESEARCH_PAPER"
    #: An official pricing/product page — the source ``FREE_DEAL`` claims must resolve
    #: to, not a secondary summary of one.
    OFFICIAL_PRODUCT_PAGE = "OFFICIAL_PRODUCT_PAGE"


class PromptOrigin(StrEnum):
    """How a PROMPT_WORKFLOW post's prompt text relates to what the source actually
    published — Step 3. Never inferred; a generator must state which one applies."""

    #: The source published this exact prompt text. May be shown as a quote.
    SOURCE_VERBATIM = "SOURCE_VERBATIM"
    #: The source's prompt was reworded/clarified for readability. Must not be
    #: presented as a direct quote.
    SOURCE_ADAPTED = "SOURCE_ADAPTED"
    #: The source describes a workflow or use case but no literal prompt text — what's
    #: shown is derived from that description, not lifted from it.
    WORKFLOW_DERIVED = "WORKFLOW_DERIVED"


class FetchOutcome(StrEnum):
    """Result of one attempt to read a source.

    ``NOT_MODIFIED`` is a *success*: the server confirmed nothing changed, so there is
    no body to parse and no new items. Treating it as an error would make every
    well-behaved cached fetch look like a failure.
    """

    OK = "OK"
    NOT_MODIFIED = "NOT_MODIFIED"
    ERROR = "ERROR"


class ArticleStatus(StrEnum):
    """Triage lifecycle of source material."""

    COLLECTED = "COLLECTED"
    NORMALIZED = "NORMALIZED"
    DUPLICATE = "DUPLICATE"
    SCREENED_OUT = "SCREENED_OUT"
    EVALUATED = "EVALUATED"
    SHORTLISTED = "SHORTLISTED"
    DRAFTED = "DRAFTED"
    DISCARDED = "DISCARDED"


class DraftStatus(StrEnum):
    """Publication lifecycle of editorial content."""

    DRAFTED = "DRAFTED"
    PENDING_REVIEW = "PENDING_REVIEW"
    NEEDS_REWRITE = "NEEDS_REWRITE"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PUBLISH_FAILED = "PUBLISH_FAILED"


class DuplicateReason(StrEnum):
    """Why one article was judged a duplicate of another.

    Stored alongside the link so the decision is auditable: "why was B marked a
    duplicate of A?" must always have an answer that is a rule name, not a score.
    """

    #: Identical canonical URL — the strongest signal available.
    SAME_CANONICAL_URL = "SAME_CANONICAL_URL"
    #: Identical normalized title+body fingerprint. Verbatim syndication.
    SAME_CONTENT_FINGERPRINT = "SAME_CONTENT_FINGERPRINT"
    #: Identical normalized title from the same source. A feed re-emitting an entry.
    SAME_TITLE_SAME_SOURCE = "SAME_TITLE_SAME_SOURCE"
    #: Near-identical text by simhash within the comparison window.
    NEAR_DUPLICATE_SIMHASH = "NEAR_DUPLICATE_SIMHASH"


class PrefilterReason(StrEnum):
    """Why an article was screened out before any LLM ever sees it.

    Deliberately narrow: these are "obviously not publishable material" rules, not
    editorial judgement. Deciding whether a real story is *interesting* belongs to the
    LLM editor in Phase 4, which is why nothing here filters on technicality.
    """

    #: No usable title, and no summary or body text either.
    EMPTY_CONTENT = "EMPTY_CONTENT"
    #: Already represented by another article.
    DUPLICATE = "DUPLICATE"
    #: Published implausibly long ago — usually a malformed or paginated feed.
    STALE_ITEM = "STALE_ITEM"
    #: A hiring post rather than news.
    JOB_LISTING = "JOB_LISTING"
    #: Navigation, archive, index or newsletter-plumbing entries.
    BOILERPLATE = "BOILERPLATE"
    #: Investor relations, earnings and legal notices with no product story.
    LEGAL_OR_INVESTOR_NOTICE = "LEGAL_OR_INVESTOR_NOTICE"


class PostFormat(StrEnum):
    """How long a post should be.

    Three formats rather than one length, because a one-line changelog entry and a
    deepfake investigation are not the same story. Forcing both into the same shape
    produces padding in one and compression in the other, and a channel where every
    post looks identical stops being read.
    """

    QUICK = "QUICK"
    STANDARD = "STANDARD"
    DEEP_DIVE = "DEEP_DIVE"


class EditorialDecision(StrEnum):
    """What the editorial layer decided about a candidate story.

    ``HOLD_FOR_VERIFICATION`` exists so that a genuinely interesting story with thin
    evidence is not thrown away. A viral deepfake claim may be worth covering — but
    only once someone has checked it. Rejecting it outright would lose the story;
    shortlisting it would risk publishing something false.
    """

    SHORTLIST = "SHORTLIST"
    HOLD_FOR_VERIFICATION = "HOLD_FOR_VERIFICATION"
    REJECT = "REJECT"


class VerificationStatus(StrEnum):
    """How much independent checking a story's claims received."""

    #: The source is authoritative for the claim — a vendor describing its own product.
    NOT_REQUIRED = "NOT_REQUIRED"
    #: Independently corroborated by at least one qualifying source.
    VERIFIED = "VERIFIED"
    #: Checking was warranted but did not settle the claim.
    NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"


class EvaluatorType(StrEnum):
    """Who produced an evaluation.

    Recorded so that a future automated evaluator's output stays distinguishable from
    an editorial session's, without changing the storage schema.
    """

    CLAUDE_CODE = "CLAUDE_CODE"
    HUMAN = "HUMAN"
    AUTOMATED = "AUTOMATED"


class ReviewAction(StrEnum):
    """What a human did to a draft version during review."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    EDIT = "EDIT"
    REQUEST_REWRITE = "REQUEST_REWRITE"
    SKIP = "SKIP"


class Category(StrEnum):
    """Editorial category shown to channel readers."""

    PRODUCT_UPDATE = "PRODUCT_UPDATE"
    USEFUL_TOOL = "USEFUL_TOOL"
    WOW = "WOW"
    AI_FAIL = "AI_FAIL"
    DEEPFAKE_WATCH = "DEEPFAKE_WATCH"
    SCAM_MISINFO = "SCAM_MISINFO"
    CREATIVE_AI = "CREATIVE_AI"
    AI_FOR_WORK = "AI_FOR_WORK"
    AI_FOR_LEARNING = "AI_FOR_LEARNING"
    EVERYDAY_AI = "EVERYDAY_AI"
    TRENDING = "TRENDING"
    EXPLAINED_SIMPLY = "EXPLAINED_SIMPLY"
    SCIENCE_LITE = "SCIENCE_LITE"
    AI_DRAMA = "AI_DRAMA"


#: Categories where an unverified claim can cause real harm. The editorial layer
#: (Phase 4) requires corroboration before these may be shortlisted.
SENSITIVE_CATEGORIES: frozenset[Category] = frozenset(
    {Category.DEEPFAKE_WATCH, Category.SCAM_MISINFO, Category.AI_DRAMA}
)


class AudienceTier(StrEnum):
    """How much AI familiarity a post assumes of its reader.

    An ordered scale, lowest first. NEWCOMER was added in Phase 7.5 because the channel
    is for ordinary people, and "beginner" had quietly come to mean "beginner developer"
    — someone who knows what an API is and has simply not used this particular tool.
    A NEWCOMER may have opened ChatGPT once and does not know what an agent is.
    """

    NEWCOMER = "NEWCOMER"
    BEGINNER = "BEGINNER"
    GENERAL = "GENERAL"
    TECH_CURIOUS = "TECH_CURIOUS"


#: The scale in order, least assumed knowledge first. Written once here so nothing has
#: to re-derive it from declaration order.
AUDIENCE_ORDER: tuple[AudienceTier, ...] = (
    AudienceTier.NEWCOMER,
    AudienceTier.BEGINNER,
    AudienceTier.GENERAL,
    AudienceTier.TECH_CURIOUS,
)

#: Audiences that assume no technical background. Content for these readers is held to
#: the jargon rules in the style guide.
NON_TECHNICAL_AUDIENCES: frozenset[AudienceTier] = frozenset(
    {AudienceTier.NEWCOMER, AudienceTier.BEGINNER}
)


class ContentType(StrEnum):
    """What kind of thing a post is — a format, not a subject.

    Deliberately not a Category. A category says what a piece of news is about; this
    says whether the piece is news at all. A prompt about cooking and a news story about
    cooking apps are different products for the reader: one is something to try, the
    other is something that happened.
    """

    #: Sourced from an article somebody else published. The Phase 1-7 pipeline.
    NEWS = "NEWS"
    #: A ready-to-use prompt the reader can copy. Usually evergreen.
    PROMPT = "PROMPT"
    #: One concept explained without assuming technical background.
    EXPLAINER = "EXPLAINER"
    #: Something a real person did with AI, retold: the workflow and the result are the
    #: point, and a prompt may be one component or absent entirely. Distinct from
    #: PROMPT, where the copyable prompt *is* the product.
    TESTED_USE_CASE = "TESTED_USE_CASE"
    #: Something to download or keep: a checklist, a cheat sheet, a curated collection.
    RESOURCE = "RESOURCE"


class ContentOrigin(StrEnum):
    """Where a piece of content came from.

    Recorded explicitly so "no article" never has to be read as "we forgot the source".
    Editorial-original content is written by this newsroom; saying so is the honest
    alternative to manufacturing a source article for it.
    """

    SOURCED_ARTICLE = "SOURCED_ARTICLE"
    EDITORIAL_ORIGINAL = "EDITORIAL_ORIGINAL"


class PromptTopic(StrEnum):
    """What a prompt helps with, in the reader's terms rather than the industry's.

    A small vocabulary on purpose. These are the situations someone actually finds
    themselves in — not a taxonomy of AI capabilities.
    """

    EVERYDAY_LIFE = "EVERYDAY_LIFE"
    WORK = "WORK"
    LEARNING = "LEARNING"
    CREATIVE = "CREATIVE"
    TRAVEL = "TRAVEL"
    SHOPPING = "SHOPPING"
    FOOD = "FOOD"
    PERSONAL_ORGANIZATION = "PERSONAL_ORGANIZATION"
    FUN = "FUN"


class PublicationStatus(StrEnum):
    """Outcome of one attempt to send a draft version to a channel.

    ``UNCERTAIN`` is the honest state, not an oversight. A send whose response was lost
    in transit may or may not have produced a post, and the only safe thing an
    application can do is say so and stop.
    """

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class EvidenceStatus(StrEnum):
    """Whether a prompt rests on a real, findable demonstration.

    Introduced in Phase 8.1 to correct a mistake in the content model. Prompts were
    allowed to be editorial-original, which in practice meant inventing something
    plausible and presenting it as advice. A prompt that reads well is not a prompt that
    was shown to work, and the difference is invisible to a reader.

    Only :attr:`VERIFIED_SOURCE_BACKED` may reach a channel.
    """

    #: Someone published a demonstration of this workflow and the evidence is recorded.
    VERIFIED_SOURCE_BACKED = "VERIFIED_SOURCE_BACKED"
    #: A candidate whose source turned out not to demonstrate anything. Kept, not sent.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    #: Written before this rule existed. Never publishable, never retro-justified: the
    #: honest label for content whose provenance we cannot reconstruct without inventing it.
    LEGACY_UNVERIFIED = "LEGACY_UNVERIFIED"


class SourceTier(StrEnum):
    """How close the evidence sits to whoever actually ran the workflow."""

    #: The vendor's own documentation or demo — OpenAI, Anthropic, Google, Adobe, Canva.
    OFFICIAL_PRODUCT = "OFFICIAL_PRODUCT"
    #: A publication, a case study, or a creator tutorial that actually shows the work.
    REPUTABLE_WRITEUP = "REPUTABLE_WRITEUP"
    #: A person on Reddit, Hacker News, a forum or YouTube reporting what happened to
    #: them. Legitimate for this content type — "somebody tried this" is the whole point
    #: — provided the post never upgrades it into "this works".
    COMMUNITY_REPORT = "COMMUNITY_REPORT"


class PromptRepresentation(StrEnum):
    """How the prompt in a post relates to the prompt in the source."""

    #: A short prompt reproduced faithfully.
    VERBATIM_SHORT = "VERBATIM_SHORT"
    #: Reworded for Ukrainian readers or clarity. Same intent, different words.
    ADAPTED = "ADAPTED"
    #: The source described a workflow rather than quoting a prompt; this is our
    #: reconstruction of it, and it is labelled as such rather than passed off as theirs.
    WORKFLOW_RECONSTRUCTION = "WORKFLOW_RECONSTRUCTION"


class UseCaseTheme(StrEnum):
    """What area of life a tested use case belongs to.

    Discovery directions, not a taxonomy to fill. An empty theme is a theme nobody has
    found a real example for yet, which is the correct state for it to be in.
    """

    PERSONAL_ASSISTANT = "PERSONAL_ASSISTANT"
    ORGANIZATION = "ORGANIZATION"
    PLANNING = "PLANNING"
    LEARNING = "LEARNING"
    LANGUAGE_LEARNING = "LANGUAGE_LEARNING"
    DOCUMENT_ANALYSIS = "DOCUMENT_ANALYSIS"
    EMAIL_AND_MESSAGES = "EMAIL_AND_MESSAGES"
    TRAVEL = "TRAVEL"
    SHOPPING_RESEARCH = "SHOPPING_RESEARCH"
    CAREER = "CAREER"
    INTERVIEWS = "INTERVIEWS"
    CV = "CV"
    STUDY = "STUDY"
    NOTES = "NOTES"
    CREATIVE = "CREATIVE"
    IMAGE_WORKFLOW = "IMAGE_WORKFLOW"
    PRODUCTIVITY = "PRODUCTIVITY"
    EVERYDAY_AI = "EVERYDAY_AI"


class EvidenceKind(StrEnum):
    """What kind of act produced the evidence.

    A different axis from :class:`SourceTier`, which says *who is vouching*. This says
    *what happened*. A vendor demo and a Reddit post can both be honest evidence; they
    are not the same kind of claim, and the writing has to reflect which one it is.
    """

    #: The vendor demonstrated their own product.
    OFFICIAL_TEST = "OFFICIAL_TEST"
    #: A publication or creator showed the work.
    THIRD_PARTY_DEMO = "THIRD_PARTY_DEMO"
    #: Several people in a community reported the same thing.
    COMMUNITY_TESTED = "COMMUNITY_TESTED"
    #: The channel owner ran it herself and has the result.
    OWNER_TESTED = "OWNER_TESTED"
    #: One person said this worked for them. Anecdote — useful, and never upgraded into
    #: a general claim by the writing.
    USER_REPORTED_LIFEHACK = "USER_REPORTED_LIFEHACK"


class PromptPlacement(StrEnum):
    """Where the prompt lives in the published bundle."""

    #: Short enough to sit in the post and be copied from it.
    INLINE = "INLINE"
    #: Long enough that it would swamp the post; goes in the first comment, and the post
    #: says so. The comment is part of what a human approves, not written afterwards.
    COMMENT = "COMMENT"
    #: The post has no prompt — common for a use case where the workflow is the point.
    NONE = "NONE"


class MediaRole(StrEnum):
    """What a piece of media is doing in the post."""

    RESULT_IMAGE = "RESULT_IMAGE"
    BEFORE_IMAGE = "BEFORE_IMAGE"
    AFTER_IMAGE = "AFTER_IMAGE"
    SCREENSHOT = "SCREENSHOT"
    SOURCE_SCREENSHOT = "SOURCE_SCREENSHOT"
    INFOGRAPHIC = "INFOGRAPHIC"
    PDF = "PDF"
    OTHER = "OTHER"


class MediaOrigin(StrEnum):
    """Where a piece of media came from. Never inferred, because reusing somebody
    else's image without knowing it is theirs is how a channel gets a complaint."""

    #: Belongs to the source. Recorded by URL; not downloaded and republished.
    SOURCE_MEDIA = "SOURCE_MEDIA"
    #: The owner generated it with an AI tool, and the tool is recorded.
    OWNER_GENERATED = "OWNER_GENERATED"
    #: The owner's own screenshot of their own screen.
    OWNER_SCREENSHOT = "OWNER_SCREENSHOT"
    #: Made for the channel — a diagram, a cover.
    EDITORIAL_ASSET = "EDITORIAL_ASSET"


class ResourceType(StrEnum):
    """What kind of thing a RESOURCE post gives the reader."""

    PDF_COLLECTION = "PDF_COLLECTION"
    CHECKLIST = "CHECKLIST"
    CHEAT_SHEET = "CHEAT_SHEET"
    CURATED_LIST = "CURATED_LIST"
    MINI_GUIDE = "MINI_GUIDE"
    PROMPT_COLLECTION = "PROMPT_COLLECTION"
    TOOL_COLLECTION = "TOOL_COLLECTION"


#: Content types whose posts must rest on evidence somebody published or the owner
#: produced. An explainer explains and a resource curates; neither is a claim that a
#: workflow worked, so neither carries this requirement.
EVIDENCE_REQUIRED_TYPES: frozenset[ContentType] = frozenset(
    {ContentType.PROMPT, ContentType.TESTED_USE_CASE}
)


class QueueStatus(StrEnum):
    """Where a scheduled publication stands.

    Deliberately small, and deliberately separate from ``DraftStatus``. A draft's status
    says what the editorial process has decided about it; this says what the scheduler
    is doing about a decision already made. Overloading one onto the other would mean a
    cancelled schedule looked like a withdrawn approval, which it is not.

    Three of these end the item's life (PUBLISHED, CANCELLED, INVALIDATED). Four ask a
    human to look (STALE_REVIEW_REQUIRED, HOLD_FOR_REVIEW, FAILED, UNCERTAIN), and the
    scheduler never resolves any of them by itself.
    """

    #: Waiting for its time.
    SCHEDULED = "SCHEDULED"
    #: A worker holds the lease and is publishing it now.
    PROCESSING = "PROCESSING"
    #: It went out.
    PUBLISHED = "PUBLISHED"
    #: The owner withdrew it. The approval is untouched and it can be scheduled again.
    CANCELLED = "CANCELLED"
    #: What it pointed at changed underneath it — the draft was edited, or the approval
    #: stopped being valid. It can never publish; a new approval and a new schedule can.
    INVALIDATED = "INVALIDATED"
    #: The content aged past the window for its type. An editorial judgement, so a human
    #: makes it: the scheduler will not extend an approval by publishing anyway.
    STALE_REVIEW_REQUIRED = "STALE_REVIEW_REQUIRED"
    #: A precondition failed at publication time — a missing image, an unresolved
    #: earlier attempt. Operational rather than editorial, and still not auto-resolved.
    HOLD_FOR_REVIEW = "HOLD_FOR_REVIEW"
    #: Telegram definitely refused it. Nothing is on the channel.
    FAILED = "FAILED"
    #: The send may or may not have landed. Never retried automatically; duplicates on a
    #: channel are visible to readers and cannot be undone.
    UNCERTAIN = "UNCERTAIN"


#: Statuses a queue item never leaves.
TERMINAL_QUEUE_STATUSES: frozenset[QueueStatus] = frozenset(
    {QueueStatus.PUBLISHED, QueueStatus.CANCELLED, QueueStatus.INVALIDATED}
)

#: Statuses that mean the scheduler has stopped and a human has to decide what happens.
ATTENTION_QUEUE_STATUSES: frozenset[QueueStatus] = frozenset(
    {
        QueueStatus.STALE_REVIEW_REQUIRED,
        QueueStatus.HOLD_FOR_REVIEW,
        QueueStatus.FAILED,
        QueueStatus.UNCERTAIN,
    }
)
