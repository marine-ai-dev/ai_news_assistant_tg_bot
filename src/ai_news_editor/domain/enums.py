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
