"""What kind of thing a post is, editorially — derived, never stored.

The channel wants a mix: some news, some product updates, something to try, something
explained, something surprising, occasionally something deeper. Those six buckets are
how an editor thinks about a week.

They are **not a new taxonomy**. Every one is derived from the `ContentType` and
`Category` a post already carries, because a second stored taxonomy is a second thing to
keep in sync and a second thing to get wrong. Nothing here writes to the database; this
module answers a question about data that already exists.

The mapping is deliberately explicit rather than clever. An editor should be able to
read it and disagree with it.
"""

from __future__ import annotations

from enum import StrEnum

from ai_news_editor.domain.enums import AudienceTier, Category, ContentType


class Bucket(StrEnum):
    """The editorial mix a week is judged against."""

    #: Something happened in AI worth knowing about.
    NEWS = "NEWS"
    #: A tool or product changed in a way a reader would notice.
    PRODUCT_UPDATE = "PRODUCT_UPDATE"
    #: Something to try: a prompt, a workflow, a lifehack.
    PRACTICAL = "PRACTICAL"
    #: One concept, explained without assumed background.
    EXPLAINER = "EXPLAINER"
    #: Surprising, strange, or cautionary. The reason someone forwards a post.
    WOW = "WOW"
    #: Deeper or more technical material, in small doses.
    SCIENCE = "SCIENCE"


#: Roughly what a good week looks like. **Targets, not quotas.** Nothing in this project
#: refuses good content because a percentage is imperfect — these exist so a drift of
#: six weeks in one direction is visible before a reader notices it.
DEFAULT_MIX: dict[Bucket, float] = {
    Bucket.NEWS: 0.30,
    Bucket.PRODUCT_UPDATE: 0.20,
    Bucket.PRACTICAL: 0.20,
    Bucket.EXPLAINER: 0.15,
    Bucket.WOW: 0.10,
    Bucket.SCIENCE: 0.05,
}

#: Categories that make a NEWS post a product-update post rather than a general one.
_PRODUCT_CATEGORIES = frozenset({Category.PRODUCT_UPDATE, Category.USEFUL_TOOL})

#: Categories whose appeal is that they are surprising, alarming or entertaining.
_WOW_CATEGORIES = frozenset(
    {
        Category.WOW,
        Category.AI_FAIL,
        Category.DEEPFAKE_WATCH,
        Category.SCAM_MISINFO,
        Category.AI_DRAMA,
    }
)

#: The one category that means "this is the deeper end".
_SCIENCE_CATEGORIES = frozenset({Category.SCIENCE_LITE})

#: Audience tiers an ordinary reader can follow without prior AI experience.
ACCESSIBLE_TIERS: frozenset[AudienceTier] = frozenset(
    {AudienceTier.NEWCOMER, AudienceTier.BEGINNER}
)

#: Roughly what share of a week should be readable by someone who has never opened an
#: AI chat. The channel exists for those readers; drifting technical is the failure mode
#: this number is here to make visible.
ACCESSIBLE_TARGET_MIN = 0.40
ACCESSIBLE_TARGET_MAX = 0.50

#: Content types that give the reader something to do rather than something to know.
PRACTICAL_TYPES: frozenset[ContentType] = frozenset(
    {ContentType.PROMPT, ContentType.TESTED_USE_CASE}
)

BUCKET_LABELS: dict[Bucket, str] = {
    Bucket.NEWS: "📰 NEWS",
    Bucket.PRODUCT_UPDATE: "🚀 PRODUCT",
    Bucket.PRACTICAL: "🛠 PRACTICAL",
    Bucket.EXPLAINER: "🧠 EXPLAINER",
    Bucket.WOW: "✨ WOW",
    Bucket.SCIENCE: "🔬 SCIENCE",
}


def bucket_for(content_type: ContentType, category: Category) -> Bucket:
    """Which editorial bucket a post falls into.

    Content type decides first, because it says what the post *is* — a prompt is
    something to try whatever it is about. Category only breaks the tie within news.
    """
    if content_type in PRACTICAL_TYPES:
        return Bucket.PRACTICAL
    if content_type is ContentType.EXPLAINER:
        # An explainer about the deep end is still an explainer; the reader is being
        # taught something, not shown a result.
        return Bucket.EXPLAINER
    if content_type is ContentType.RESOURCE:
        # A checklist or collection is something to keep and use.
        return Bucket.PRACTICAL

    # NEWS, split by what the news is about.
    if category in _PRODUCT_CATEGORIES:
        return Bucket.PRODUCT_UPDATE
    if category in _WOW_CATEGORIES:
        return Bucket.WOW
    if category in _SCIENCE_CATEGORIES:
        return Bucket.SCIENCE
    if category is Category.EXPLAINED_SIMPLY:
        return Bucket.EXPLAINER
    return Bucket.NEWS


def is_accessible(audience: AudienceTier) -> bool:
    """Could someone who has never used AI follow this?"""
    return audience in ACCESSIBLE_TIERS


def is_practical(content_type: ContentType) -> bool:
    """Does this give the reader something to do?"""
    return content_type in PRACTICAL_TYPES
