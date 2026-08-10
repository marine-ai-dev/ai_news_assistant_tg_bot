"""Persistence for editorial evaluations — append-only history."""

from __future__ import annotations

import json
import sqlite3
from uuid import UUID

from ai_news_editor.domain.clock import to_iso
from ai_news_editor.domain.enums import EditorialDecision
from ai_news_editor.domain.models import Evaluation
from ai_news_editor.editorial.rubric import DIMENSIONS

_SCORE_COLUMNS = ", ".join(DIMENSIONS)
_SCORE_PLACEHOLDERS = ", ".join("?" for _ in DIMENSIONS)


def _to_domain(row: sqlite3.Row) -> Evaluation:
    data = dict(row)
    scores = {name: data.pop(name) for name in DIMENSIONS}
    data["scores"] = scores
    data["verification_sources"] = tuple(json.loads(data.pop("verification_sources_json")))
    data["why_selected"] = tuple(json.loads(data.pop("why_selected_json")))
    return Evaluation.model_validate(data)


class EvaluationRepository:
    """Reads and appends ``evaluations``.

    There is no update method. An article that needs a different judgement gets a new
    evaluation, so the record of what was decided — and on what content — survives.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def add(self, evaluation: Evaluation) -> Evaluation:
        self._conn.execute(
            f"""
            INSERT INTO evaluations (id, article_id, schema_version, rubric_version,
                                     evaluator_type, evaluator, batch_id, content_fingerprint,
                                     decision, category, audience, {_SCORE_COLUMNS},
                                     composite_score, verification_status,
                                     verification_sources_json, why_selected_json,
                                     editorial_angle, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {_SCORE_PLACEHOLDERS},
                    ?, ?, ?, ?, ?, ?, ?)
            """,  # noqa: S608 - column names come from a module constant, never user input
            (
                str(evaluation.id),
                str(evaluation.article_id),
                evaluation.schema_version,
                evaluation.rubric_version,
                evaluation.evaluator_type.value,
                evaluation.evaluator,
                evaluation.batch_id,
                evaluation.content_fingerprint,
                evaluation.decision.value,
                evaluation.category.value,
                evaluation.audience.value,
                *(evaluation.scores[name] for name in DIMENSIONS),
                evaluation.composite_score,
                evaluation.verification_status.value,
                json.dumps(list(evaluation.verification_sources), ensure_ascii=False),
                json.dumps(list(evaluation.why_selected), ensure_ascii=False),
                evaluation.editorial_angle,
                evaluation.notes,
                to_iso(evaluation.created_at),
            ),
        )
        return evaluation

    def exists(self, article_id: UUID, batch_id: str | None, fingerprint: str) -> bool:
        """Whether this exact judgement was already imported.

        The idempotency key for re-importing the same reviewed file.
        """
        row = self._conn.execute(
            "SELECT 1 FROM evaluations WHERE article_id = ? AND batch_id IS ? "
            "AND content_fingerprint = ? LIMIT 1",
            (str(article_id), batch_id, fingerprint),
        ).fetchone()
        return row is not None

    def latest_for_article(self, article_id: UUID) -> Evaluation | None:
        row = self._conn.execute(
            "SELECT * FROM evaluations WHERE article_id = ? ORDER BY created_at DESC, id DESC "
            "LIMIT 1",
            (str(article_id),),
        ).fetchone()
        return _to_domain(row) if row else None

    def history_for_article(self, article_id: UUID) -> list[Evaluation]:
        rows = self._conn.execute(
            "SELECT * FROM evaluations WHERE article_id = ? ORDER BY created_at, id",
            (str(article_id),),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def latest_fingerprints(self) -> dict[UUID, str]:
        """The content fingerprint of each article's most recent evaluation."""
        rows = self._conn.execute(
            """
            SELECT article_id, content_fingerprint FROM evaluations
            WHERE (article_id, created_at, id) IN (
                SELECT article_id, MAX(created_at), MAX(id) FROM evaluations GROUP BY article_id
            )
            """
        ).fetchall()
        return {UUID(row["article_id"]): row["content_fingerprint"] for row in rows}

    def current_fingerprints(self) -> dict[UUID, str]:
        """Alias used by the exporter when deciding what still needs review."""
        return self.latest_fingerprints()

    def shortlist(self, *, limit: int = 50) -> list[Evaluation]:
        """Shortlisted evaluations, highest composite score first.

        Only the most recent evaluation per article counts, so a re-evaluation
        supersedes an earlier judgement in the ranking without deleting it.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM evaluations e
            WHERE e.decision = ?
              AND e.created_at = (
                  SELECT MAX(created_at) FROM evaluations WHERE article_id = e.article_id
              )
            ORDER BY e.composite_score DESC, e.created_at DESC
            LIMIT ?
            """,
            (EditorialDecision.SHORTLIST.value, limit),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def by_decision(self, decision: EditorialDecision, *, limit: int = 50) -> list[Evaluation]:
        rows = self._conn.execute(
            """
            SELECT * FROM evaluations e
            WHERE e.decision = ?
              AND e.created_at = (
                  SELECT MAX(created_at) FROM evaluations WHERE article_id = e.article_id
              )
            ORDER BY e.composite_score DESC, e.created_at DESC
            LIMIT ?
            """,
            (decision.value, limit),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def count_by_decision(self) -> dict[str, int]:
        """Counts of the most recent decision per article."""
        rows = self._conn.execute(
            """
            SELECT e.decision, COUNT(*) AS n FROM evaluations e
            WHERE e.created_at = (
                SELECT MAX(created_at) FROM evaluations WHERE article_id = e.article_id
            )
            GROUP BY e.decision
            """
        ).fetchall()
        return {row["decision"]: row["n"] for row in rows}

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM evaluations").fetchone()["n"])

    def evaluated_article_ids(self) -> set[UUID]:
        rows = self._conn.execute("SELECT DISTINCT article_id FROM evaluations").fetchall()
        return {UUID(row["article_id"]) for row in rows}
