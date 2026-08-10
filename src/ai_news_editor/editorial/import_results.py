"""Validating and importing editorial decisions.

Editorial output is untrusted input, exactly like a feed. It is checked against the
schema, the enums, the rubric gates and the actual database state *before* anything is
written, and it cannot express approval or publication at all — the reviewed document
has no vocabulary for those.

Two properties this module guarantees:

*Atomicity.* The whole file is validated first. If a single review is bad, nothing is
imported — a half-imported batch would leave the shortlist quietly wrong.

*Idempotency.* Importing the same reviewed file twice adds nothing the second time.
Identity is ``(article_id, batch_id, content_fingerprint)``: a genuinely revised
judgement arrives under a new batch id and becomes a new evaluation rather than
silently overwriting the old one.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ai_news_editor.domain.enums import EvaluatorType
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.domain.models import Evaluation
from ai_news_editor.editorial.export import build_excerpt, fingerprint_for
from ai_news_editor.editorial.rubric import composite_score
from ai_news_editor.editorial.schema import ArticleReview, ReviewedBatch
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.storage.db import transaction
from ai_news_editor.storage.repositories import ArticleRepository, EvaluationRepository

logger = get_logger(__name__)


class EditorialImportError(AiNewsError):
    """A reviewed batch was rejected. Nothing was written."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            f"{len(problems)} problem(s) in the reviewed batch:\n  - " + "\n  - ".join(problems)
        )
        self.problems = problems


@dataclass(frozen=True, slots=True)
class ImportReport:
    """What an import did."""

    batch_id: str
    imported: int = 0
    already_present: int = 0
    shortlisted: int = 0
    held: int = 0
    rejected: int = 0
    stale_skipped: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)


def load_reviewed(path: Path) -> ReviewedBatch:
    """Parse and structurally validate a reviewed batch file.

    Raises:
        EditorialImportError: unreadable, not JSON, or failing schema validation.
    """
    if not path.exists():
        raise EditorialImportError([f"reviewed file not found: {path}"])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EditorialImportError([f"{path} is not valid JSON: {exc}"]) from exc
    except OSError as exc:
        raise EditorialImportError([f"could not read {path}: {exc}"]) from exc

    try:
        return ReviewedBatch.model_validate(raw)
    except ValidationError as exc:
        raise EditorialImportError(_readable(exc)) from exc


def _readable(exc: ValidationError) -> list[str]:
    problems: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(document)"
        problems.append(f"{location}: {error['msg']}")
    return problems


def validate_against_database(connection: sqlite3.Connection, reviewed: ReviewedBatch) -> list[str]:
    """Check a reviewed batch against real articles. Returns problems, empty if sound.

    Schema validation already covers shape, enums, ranges and the rubric gates. This is
    the half that needs the database: do these articles exist, and does each judgement
    refer to the content that article actually had?
    """
    articles_repo = ArticleRepository(connection)
    problems: list[str] = []

    for review in reviewed.reviews:
        label = f"article {review.article_id}"
        try:
            article = articles_repo.get(review.article_id)
        except AiNewsError:
            problems.append(f"{label}: unknown article id")
            continue

        excerpt, _ = build_excerpt(article.clean_text)
        expected = fingerprint_for(article, excerpt)
        if review.content_fingerprint != expected:
            problems.append(
                f"{label}: content_fingerprint does not match the article's current content "
                f"(reviewed {review.content_fingerprint[:12]}…, current {expected[:12]}…). "
                "The article changed after the batch was exported; re-export and re-review."
            )
    return problems


def import_reviewed(
    connection: sqlite3.Connection,
    reviewed: ReviewedBatch,
    *,
    evaluator_type: EvaluatorType = EvaluatorType.CLAUDE_CODE,
) -> ImportReport:
    """Validate fully, then persist every evaluation in one transaction.

    Raises:
        EditorialImportError: validation failed. Nothing was written.
    """
    problems = validate_against_database(connection, reviewed)
    if problems:
        raise EditorialImportError(problems)

    repo = EvaluationRepository(connection)
    to_insert: list[Evaluation] = []
    already = 0

    for review in reviewed.reviews:
        if repo.exists(review.article_id, reviewed.batch_id, review.content_fingerprint):
            already += 1
            continue
        to_insert.append(_to_evaluation(review, reviewed, evaluator_type))

    with transaction(connection):
        for evaluation in to_insert:
            repo.add(evaluation)

    decisions = [evaluation.decision.value for evaluation in to_insert]
    report = ImportReport(
        batch_id=reviewed.batch_id,
        imported=len(to_insert),
        already_present=already,
        shortlisted=decisions.count("SHORTLIST"),
        held=decisions.count("HOLD_FOR_VERIFICATION"),
        rejected=decisions.count("REJECT"),
    )
    logger.info(
        "editorial batch imported",
        extra={
            "batch_id": reviewed.batch_id,
            "imported": report.imported,
            "already_present": report.already_present,
            "shortlisted": report.shortlisted,
            "held": report.held,
            "rejected": report.rejected,
        },
    )
    return report


def _to_evaluation(
    review: ArticleReview, reviewed: ReviewedBatch, evaluator_type: EvaluatorType
) -> Evaluation:
    scores = review.scores.as_dict()
    return Evaluation(
        article_id=review.article_id,
        schema_version=reviewed.schema_version,
        rubric_version=reviewed.rubric_version,
        evaluator_type=evaluator_type,
        evaluator=reviewed.reviewer,
        batch_id=reviewed.batch_id,
        content_fingerprint=review.content_fingerprint,
        decision=review.decision,
        category=review.category,
        audience=review.audience,
        scores=scores,
        # Computed here, never taken from the reviewed document: ranking stays
        # deterministic and out of the evaluator's hands.
        composite_score=composite_score(scores),
        verification_status=review.verification_status,
        verification_sources=tuple(
            source.model_dump(mode="json") for source in review.verification_sources
        ),
        why_selected=tuple(review.why_selected),
        editorial_angle=review.editorial_angle,
        notes=review.notes,
    )
