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
    """How much AI familiarity a post assumes of its reader."""

    BEGINNER = "BEGINNER"
    GENERAL = "GENERAL"
    TECH_CURIOUS = "TECH_CURIOUS"
