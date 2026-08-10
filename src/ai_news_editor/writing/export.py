"""Building writing assignments from shortlisted evaluations.

Eligibility is deliberately narrow and enforced here rather than left to a CLI flag:

* the article's most recent evaluation must be ``SHORTLIST`` — ``REJECT`` and
  ``HOLD_FOR_VERIFICATION`` produce nothing
* that evaluation must be *current*, i.e. its fingerprint still matches the article's
  content; a stale judgement has to be re-made before anything is written
* the article must not already have a draft

A story that was held for verification is held for a reason. Writing it anyway would
route around the only check standing between an unverified claim and a post.
"""

from __future__ import annotations

import sqlite3
from uuid import UUID, uuid4

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import Category, EditorialDecision, PostFormat
from ai_news_editor.domain.models import Article, Evaluation
from ai_news_editor.editorial.export import build_excerpt, fingerprint_for
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    DraftRepository,
    EvaluationRepository,
    SourceRepository,
)
from ai_news_editor.writing.schema import (
    SOURCE_EXCERPT_LIMIT,
    AssignmentEvaluation,
    AssignmentSource,
    WritingAssignment,
    WritingBatch,
)

logger = get_logger(__name__)

#: A first guess at length, refined by whoever writes the post. Small changelog entries
#: rarely deserve a long post; a verified deepfake or scam usually needs the room to
#: explain how the deception worked.
_FORMAT_HINTS: dict[Category, PostFormat] = {
    Category.PRODUCT_UPDATE: PostFormat.STANDARD,
    Category.USEFUL_TOOL: PostFormat.STANDARD,
    Category.AI_FOR_WORK: PostFormat.QUICK,
    Category.AI_FOR_LEARNING: PostFormat.STANDARD,
    Category.EVERYDAY_AI: PostFormat.STANDARD,
    Category.WOW: PostFormat.QUICK,
    Category.AI_FAIL: PostFormat.STANDARD,
    Category.TRENDING: PostFormat.QUICK,
    Category.CREATIVE_AI: PostFormat.STANDARD,
    Category.DEEPFAKE_WATCH: PostFormat.DEEP_DIVE,
    Category.SCAM_MISINFO: PostFormat.DEEP_DIVE,
    Category.AI_DRAMA: PostFormat.DEEP_DIVE,
    Category.EXPLAINED_SIMPLY: PostFormat.DEEP_DIVE,
    Category.SCIENCE_LITE: PostFormat.DEEP_DIVE,
}


class Ineligible(Exception):
    """An article cannot be written, with the reason why."""


def eligibility_problem(
    article: Article, evaluation: Evaluation | None, *, has_draft: bool
) -> str | None:
    """Return why this article cannot be written, or ``None`` if it can.

    The single place the rules live, so the CLI, the exporter and the importer all agree
    and none of them can be talked out of it.
    """
    if evaluation is None:
        return "no editorial evaluation; run the editorial review first"
    if evaluation.decision is EditorialDecision.REJECT:
        return "the most recent evaluation rejected this story"
    if evaluation.decision is EditorialDecision.HOLD_FOR_VERIFICATION:
        return (
            "the story is held for verification; resolve the verification before writing "
            "rather than writing around it"
        )
    if evaluation.decision is not EditorialDecision.SHORTLIST:
        return f"unexpected decision {evaluation.decision.value}"

    excerpt, _ = build_excerpt(article.clean_text)
    if evaluation.content_fingerprint != fingerprint_for(article, excerpt):
        return (
            "the article changed since it was evaluated; it needs re-evaluation before "
            "it can be written"
        )
    if has_draft:
        return "a draft already exists for this article"
    return None


def build_writing_batch(
    connection: sqlite3.Connection,
    *,
    limit: int = 5,
    article_ids: list[UUID] | None = None,
    categories: list[Category] | None = None,
    batch_id: str | None = None,
) -> tuple[WritingBatch, list[tuple[UUID, str]]]:
    """Assemble writing assignments. Returns the batch and any skipped articles."""
    articles_repo = ArticleRepository(connection)
    sources_repo = SourceRepository(connection)
    evaluations_repo = EvaluationRepository(connection)
    drafts_repo = DraftRepository(connection)

    sources = {source.id: source for source in sources_repo.list_all()}
    drafted = drafts_repo.article_ids_with_drafts()

    shortlisted = evaluations_repo.shortlist(limit=500)
    if article_ids:
        wanted = set(article_ids)
        shortlisted = [e for e in shortlisted if e.article_id in wanted]
    if categories:
        allowed = set(categories)
        shortlisted = [e for e in shortlisted if e.category in allowed]

    assignments: list[WritingAssignment] = []
    skipped: list[tuple[UUID, str]] = []

    for evaluation in shortlisted:
        article = articles_repo.get(evaluation.article_id)
        latest = evaluations_repo.latest_for_article(article.id)
        problem = eligibility_problem(article, latest, has_draft=article.id in drafted)
        if problem:
            skipped.append((article.id, problem))
            continue

        assignments.append(_to_assignment(article, evaluation, sources.get(article.source_id)))
        if len(assignments) >= limit:
            break

    batch = WritingBatch(
        batch_id=batch_id or f"write-{now_utc():%Y%m%dT%H%M%S}-{uuid4().hex[:8]}",
        assignments=assignments,
    )
    logger.info(
        "writing batch built",
        extra={
            "batch_id": batch.batch_id,
            "assignments": len(assignments),
            "skipped": len(skipped),
        },
    )
    return batch, skipped


def _to_assignment(
    article: Article, evaluation: Evaluation, source: object | None
) -> WritingAssignment:
    excerpt = article.clean_text
    truncated = False
    if excerpt and len(excerpt) > SOURCE_EXCERPT_LIMIT:
        excerpt = excerpt[: SOURCE_EXCERPT_LIMIT - 4].rstrip() + " […]"
        truncated = True

    return WritingAssignment(
        article_id=article.id,
        article_fingerprint=evaluation.content_fingerprint,
        source=AssignmentSource(
            id=article.source_id,
            name=getattr(source, "name", article.source_id),
            trust_tier=getattr(source, "trust_tier", "UNVERIFIED"),  # type: ignore[arg-type]
            url=article.canonical_url,
        ),
        original_title=article.title,
        published_at=article.published_at.isoformat() if article.published_at else None,
        source_excerpt=excerpt,
        source_excerpt_truncated=truncated,
        evaluation=AssignmentEvaluation(
            evaluation_id=evaluation.id,
            category=evaluation.category,
            audience=evaluation.audience,
            composite_score=evaluation.composite_score,
            editorial_angle=evaluation.editorial_angle,
            why_selected=evaluation.why_selected,
            verification_status=evaluation.verification_status,
            verification_sources=evaluation.verification_sources,
            scores=evaluation.scores,
        ),
        # Several sources (changelogs especially) supply a title and nothing else. Say
        # so plainly, so the writer checks the original instead of padding the gap.
        needs_source_check=not excerpt,
        suggested_format=_FORMAT_HINTS.get(evaluation.category, PostFormat.STANDARD),
    )
