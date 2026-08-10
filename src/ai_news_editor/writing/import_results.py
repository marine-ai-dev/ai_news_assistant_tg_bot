"""Validating finished drafts and turning them into Draft + DraftVersion records.

Everything the editorial importer does, plus the eligibility rules: a draft can only be
created from a current SHORTLIST evaluation of the article it names.

The two properties that matter most here:

*Nothing is approved.* Drafts land in ``PENDING_REVIEW``. There is no code path from
importing a draft to ``APPROVED`` or ``PUBLISHED``, and the returned schema has no field
that could request one.

*The hash is Python's.* ``DraftVersion.content_hash`` is computed from the stored
content, never supplied by the writer, so what a human later approves is exactly what
was validated here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from ai_news_editor.domain.enums import DraftStatus
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.editorial.export import build_excerpt, fingerprint_for
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    DraftRepository,
    EvaluationRepository,
)
from ai_news_editor.writing.export import eligibility_problem
from ai_news_editor.writing.format import check_length, source_line
from ai_news_editor.writing.schema import DraftBatch, DraftResult

logger = get_logger(__name__)


class DraftImportError(AiNewsError):
    """A draft batch was rejected. Nothing was written."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            f"{len(problems)} problem(s) in the draft batch:\n  - " + "\n  - ".join(problems)
        )
        self.problems = problems


@dataclass(frozen=True, slots=True)
class DraftImportReport:
    """What an import did."""

    batch_id: str
    created: int = 0
    already_present: int = 0
    draft_ids: tuple[UUID, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def load_drafts(path: Path) -> DraftBatch:
    """Parse and structurally validate a draft batch file."""
    if not path.exists():
        raise DraftImportError([f"draft file not found: {path}"])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DraftImportError([f"{path} is not valid JSON: {exc}"]) from exc
    except OSError as exc:
        raise DraftImportError([f"could not read {path}: {exc}"]) from exc

    try:
        return DraftBatch.model_validate(raw)
    except ValidationError as exc:
        raise DraftImportError(
            [
                f"{'.'.join(str(p) for p in error['loc']) or '(document)'}: {error['msg']}"
                for error in exc.errors()
            ]
        ) from exc


def validate_against_database(
    connection: sqlite3.Connection, batch: DraftBatch
) -> tuple[list[str], list[str]]:
    """Check a draft batch against real articles and evaluations.

    Returns ``(problems, warnings)``. Problems block the import; warnings are reported
    but do not — a post outside its length target is worth flagging, not refusing.
    """
    articles_repo = ArticleRepository(connection)
    evaluations_repo = EvaluationRepository(connection)

    problems: list[str] = []
    warnings: list[str] = []

    for draft in batch.drafts:
        label = f"article {draft.article_id}"
        try:
            article = articles_repo.get(draft.article_id)
        except AiNewsError:
            problems.append(f"{label}: unknown article id")
            continue

        evaluation = evaluations_repo.latest_for_article(article.id)
        if evaluation is None:
            problems.append(f"{label}: no evaluation exists for this article")
            continue
        if evaluation.id != draft.evaluation_id:
            problems.append(
                f"{label}: evaluation {draft.evaluation_id} is not this article's current "
                f"evaluation ({evaluation.id})"
            )
            continue

        # has_draft is deliberately False here. An article that already has a draft is
        # not an error at import time — re-importing the same file must be a harmless
        # no-op, so the import loop skips it as already-present instead.
        problem = eligibility_problem(article, evaluation, has_draft=False)
        if problem:
            problems.append(f"{label}: {problem}")
            continue

        excerpt, _ = build_excerpt(article.clean_text)
        if draft.article_fingerprint != fingerprint_for(article, excerpt):
            problems.append(
                f"{label}: article_fingerprint does not match the article's current content; "
                "re-export the assignment and rewrite"
            )
            continue

        length = check_length(draft.rendered_text, draft.post_format)
        if length.note:
            warnings.append(f"{label}: {length.note}")

    return problems, warnings


def import_drafts(connection: sqlite3.Connection, batch: DraftBatch) -> DraftImportReport:
    """Validate fully, then create Draft and DraftVersion records in one transaction.

    Every draft ends in ``PENDING_REVIEW``. Nothing here approves anything.
    """
    problems, warnings = validate_against_database(connection, batch)
    if problems:
        raise DraftImportError(problems)

    repo = DraftRepository(connection)
    evaluations = EvaluationRepository(connection)
    created: list[UUID] = []
    already = 0

    for result in batch.drafts:
        if repo.find_by_article(result.article_id) is not None:
            already += 1
            continue
        created.append(_create_draft(repo, evaluations, result, batch))

    report = DraftImportReport(
        batch_id=batch.batch_id,
        created=len(created),
        already_present=already,
        draft_ids=tuple(created),
        warnings=tuple(warnings),
    )
    logger.info(
        "draft batch imported",
        extra={
            "batch_id": batch.batch_id,
            # Not "created": logging refuses to overwrite LogRecord's own attributes,
            # and that collision raises at call time rather than at import.
            "drafts_created": report.created,
            "already_present": already,
            "warning_count": len(warnings),
        },
    )
    return report


def _create_draft(
    repo: DraftRepository,
    evaluations: EvaluationRepository,
    result: DraftResult,
    batch: DraftBatch,
) -> UUID:
    """Create the draft and move it to PENDING_REVIEW. Never further."""
    # Category and audience come from the editorial evaluation, not from the writer:
    # classification was decided in Phase 4 and writing does not get to revise it.
    evaluation = evaluations.latest_for_article(result.article_id)
    if evaluation is None:  # pragma: no cover - validation guarantees this
        raise DraftImportError([f"article {result.article_id}: evaluation vanished mid-import"])

    draft, _version = repo.create(
        article_id=result.article_id,
        evaluation_id=result.evaluation_id,
        title=result.headline,
        body=result.body,
        category=evaluation.category,
        audience=evaluation.audience,
        source_attribution=source_line(result.source_label, result.source_url),
        source_url=result.source_url,
        post_format=result.post_format,
        style_version=batch.style_version,
        writer_notes=result.writer_notes,
        hashtags=result.hashtags,
        created_by=f"{batch.writer}:style_v{batch.style_version}",
    )
    # DRAFTED -> PENDING_REVIEW, validated by the Phase-1 transition table. A human
    # still has to approve it; there is no path from here to APPROVED.
    repo.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    return draft.id
