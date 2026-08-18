"""editorial.dedup.check_consistency_for_article against a real EvaluationRepository."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    EditorialCategory,
    EditorialDecision,
    EvaluatorType,
    VerificationStatus,
)
from ai_news_editor.domain.models import Evaluation
from ai_news_editor.editorial.dedup import (
    EditorialCategoryDriftError,
    check_consistency_for_article,
)
from ai_news_editor.storage import db
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    EvaluationRepository,
    RawItemRepository,
    SourceRepository,
)
from tests.conftest import make_article, make_raw_item, make_source

_SCORES = {
    "credibility": 8,
    "general_ai_relevance": 8,
    "reader_interest": 8,
    "usefulness": 8,
    "novelty": 8,
    "wow_factor": 8,
    "virality_potential": 8,
    "accessibility": 8,
    "consumer_impact": 8,
}


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "dedup.db")
    db.migrate(conn)
    return conn


def _evaluate(
    evaluations: EvaluationRepository, article_id, category: EditorialCategory | None
) -> None:
    evaluations.add(
        Evaluation(
            article_id=article_id,
            schema_version="1",
            rubric_version="v1",
            evaluator_type=EvaluatorType.HUMAN,
            evaluator="marina",
            content_fingerprint=f"fp-{category}-{article_id.hex[:6]}",
            decision=EditorialDecision.SHORTLIST,
            category=Category.PRODUCT_UPDATE,
            audience=AudienceTier.BEGINNER,
            scores=_SCORES,
            composite_score=8.0,
            verification_status=VerificationStatus.VERIFIED,
            editorial_category=category,
        )
    )


class TestCheckConsistencyForArticle:
    def test_a_second_evaluation_agreeing_with_the_first_is_allowed(
        self, connection: sqlite3.Connection
    ) -> None:
        sources, raw_items, articles = (
            SourceRepository(connection),
            RawItemRepository(connection),
            ArticleRepository(connection),
        )
        evaluations = EvaluationRepository(connection)
        sources.upsert(make_source("openai_blog"))
        item = raw_items.add(make_raw_item("openai_blog"))
        article = articles.add(make_article(item.id, "openai_blog"))

        _evaluate(evaluations, article.id, EditorialCategory.NEWS)

        check_consistency_for_article(evaluations, article.id, EditorialCategory.NEWS)

    def test_a_conflicting_second_evaluation_is_rejected(
        self, connection: sqlite3.Connection
    ) -> None:
        """The real-repository version of section 19's example: NEWS today, AI_TOOL
        tomorrow, for the very same (already URL-deduplicated) article."""
        sources, raw_items, articles = (
            SourceRepository(connection),
            RawItemRepository(connection),
            ArticleRepository(connection),
        )
        evaluations = EvaluationRepository(connection)
        sources.upsert(make_source("openai_blog"))
        item = raw_items.add(make_raw_item("openai_blog"))
        article = articles.add(make_article(item.id, "openai_blog"))

        _evaluate(evaluations, article.id, EditorialCategory.NEWS)

        with pytest.raises(EditorialCategoryDriftError, match="already classified as NEWS"):
            check_consistency_for_article(evaluations, article.id, EditorialCategory.AI_TOOL)

    def test_an_articles_first_classification_never_conflicts(
        self, connection: sqlite3.Connection
    ) -> None:
        sources, raw_items, articles = (
            SourceRepository(connection),
            RawItemRepository(connection),
            ArticleRepository(connection),
        )
        evaluations = EvaluationRepository(connection)
        sources.upsert(make_source("openai_blog"))
        item = raw_items.add(make_raw_item("openai_blog"))
        article = articles.add(make_article(item.id, "openai_blog"))

        check_consistency_for_article(evaluations, article.id, EditorialCategory.RESEARCH)
