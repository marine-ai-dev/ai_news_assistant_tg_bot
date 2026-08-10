"""RawItem → Article normalization.

A :class:`RawItem` is a faithful ingestion record and is never mutated. Normalization
*derives* an :class:`Article` from it and keeps the link, so the original stays
traceable forever.

Everything here is mechanical: decode, strip markup, collapse whitespace, canonicalize
the URL, compute fingerprints. Nothing is summarized, translated, rewritten or inferred.
A raw item that cannot yield a usable article is rejected explicitly rather than being
padded with invented values.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import ArticleStatus
from ai_news_editor.domain.models import Article, RawItem
from ai_news_editor.pipeline.fingerprint import (
    content_fingerprint,
    simhash,
    title_fingerprint,
)
from ai_news_editor.pipeline.text import clean_text, clean_title
from ai_news_editor.pipeline.urls import InvalidUrlError, canonicalize_url


@dataclass(frozen=True, slots=True)
class NormalizationRejected:
    """A raw item that cannot become an article, and why."""

    raw_item_id: object
    reason: str


def normalize(item: RawItem) -> Article | NormalizationRejected:
    """Derive an article from a raw item.

    Returns :class:`NormalizationRejected` when the item lacks the two things an
    editorial candidate cannot exist without: a usable title and a fetchable URL.
    """
    try:
        canonical = canonicalize_url(item.url_original)
    except InvalidUrlError as exc:
        return NormalizationRejected(item.id, f"unusable URL: {exc}")

    title = clean_title(item.title_original)
    if not title:
        return NormalizationRejected(item.id, "no usable title")

    # Prefer the fuller body the feed supplied; fall back to the summary.
    body = clean_text(item.content_raw) or clean_text(item.summary_raw)

    digest = simhash(f"{title}\n{body or ''}")
    article = Article(
        raw_item_id=item.id,
        source_id=item.source_id,
        title=title,
        canonical_url=canonical,
        clean_text=body,
        # Language detection is deliberately absent: guessing from a short snippet is
        # unreliable, and every configured source declares its language already.
        language=None,
        published_at=item.published_at,
        content_hash=content_fingerprint(title, body),
        title_fingerprint=title_fingerprint(title),
        simhash=digest,
        status=ArticleStatus.COLLECTED,
        normalized_at=now_utc(),
    )
    return article
