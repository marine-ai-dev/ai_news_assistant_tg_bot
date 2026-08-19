"""planning.recent_history.recent_history — Step 3 section 8.

Builds a real source -> raw_item -> article -> evaluation -> draft -> publication
chain against a migrated SQLite connection, then checks the reader recovers the right
editorial category and source family for each successful publication.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    EditorialCategory,
    EditorialDecision,
    EvaluatorType,
    PublicationStatus,
    VerificationStatus,
)
from ai_news_editor.domain.models import Evaluation, Publication, ReviewDecision
from ai_news_editor.editorial.diversity import RecentPost
from ai_news_editor.planning.recent_history import recent_history
from ai_news_editor.sources.config import SourceDefinition, SourcesConfig
from ai_news_editor.storage import db
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    DraftRepository,
    EvaluationRepository,
    PublicationRepository,
    RawItemRepository,
    ReviewDecisionRepository,
    SourceRepository,
)
from tests.conftest import DRAFT_CONTENT, make_article, make_raw_item, make_source


def _registry(*definitions: SourceDefinition) -> SourcesConfig:
    return SourcesConfig(sources=list(definitions))


def _source_def(source_id: str, family: str) -> SourceDefinition:
    return SourceDefinition.model_validate(
        {
            "id": source_id,
            "name": source_id,
            "adapter": "rss",
            "url": f"https://example.invalid/{source_id}.xml",
            "trust_tier": "OFFICIAL",
            "editorial_role": "test",
            "priority": "PRIMARY_NORMAL",
            "content_types": ["NEWS"],
            "source_family": family,
            "publisher_region": "UNITED_STATES",
        }
    )


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "history.db")
    db.migrate(conn)
    return conn


def _publish_one(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    editorial_category: EditorialCategory | None,
) -> None:
    sources = SourceRepository(conn)
    raw_items = RawItemRepository(conn)
    articles = ArticleRepository(conn)
    evaluations = EvaluationRepository(conn)
    drafts = DraftRepository(conn)
    decisions = ReviewDecisionRepository(conn)
    publications = PublicationRepository(conn)

    sources.upsert(make_source(source_id))
    item = raw_items.add(make_raw_item(source_id))
    article = articles.add(make_article(item.id, source_id))

    evaluation = Evaluation(
        article_id=article.id,
        schema_version="1",
        rubric_version="v1",
        evaluator_type=EvaluatorType.HUMAN,
        evaluator="marina",
        content_fingerprint="fingerprint-" + article.id.hex,
        decision=EditorialDecision.SHORTLIST,
        category=Category.PRODUCT_UPDATE,
        audience=AudienceTier.BEGINNER,
        scores={
            "credibility": 8,
            "general_ai_relevance": 8,
            "reader_interest": 8,
            "usefulness": 8,
            "novelty": 8,
            "wow_factor": 8,
            "virality_potential": 8,
            "accessibility": 8,
            "consumer_impact": 8,
        },
        composite_score=8.0,
        verification_status=VerificationStatus.VERIFIED,
        editorial_category=editorial_category,
    )
    evaluations.add(evaluation)

    draft, version = drafts.create(
        article_id=article.id, evaluation_id=evaluation.id, **DRAFT_CONTENT
    )
    decision = decisions.add(
        ReviewDecision(
            draft_id=draft.id,
            draft_version_id=version.id,
            content_hash=version.content_hash,
            action="APPROVE",
            actor="marina",
        )
    )
    publications.add(
        Publication(
            draft_id=draft.id,
            draft_version_id=version.id,
            review_decision_id=decision.id,
            content_hash=version.content_hash,
            channel="@test_channel",
            status=PublicationStatus.SUCCEEDED,
            message_id=1,
            chat_id="-100",
            published_at=now_utc(),
        )
    )


class TestRecentHistory:
    def test_reads_category_and_source_family_from_a_real_chain(
        self, connection: sqlite3.Connection
    ) -> None:
        _publish_one(
            connection, source_id="openai_blog", editorial_category=EditorialCategory.AI_TOOL
        )
        registry = _registry(_source_def("openai_blog", "OpenAI"))

        history = recent_history(
            publications=PublicationRepository(connection),
            drafts=DraftRepository(connection),
            articles=ArticleRepository(connection),
            evaluations=EvaluationRepository(connection),
            sources=registry,
        )

        assert history == [RecentPost(EditorialCategory.AI_TOOL, "OpenAI")]

    def test_a_null_editorial_category_defaults_to_news(
        self, connection: sqlite3.Connection
    ) -> None:
        _publish_one(connection, source_id="reuters", editorial_category=None)
        registry = _registry(_source_def("reuters", "Reuters"))

        history = recent_history(
            publications=PublicationRepository(connection),
            drafts=DraftRepository(connection),
            articles=ArticleRepository(connection),
            evaluations=EvaluationRepository(connection),
            sources=registry,
        )

        assert history == [RecentPost(EditorialCategory.NEWS, "Reuters")]

    def test_newest_publication_comes_first(self, connection: sqlite3.Connection) -> None:
        _publish_one(connection, source_id="openai_blog", editorial_category=EditorialCategory.NEWS)
        _publish_one(
            connection, source_id="anthropic_blog", editorial_category=EditorialCategory.AI_TOOL
        )
        registry = _registry(
            _source_def("openai_blog", "OpenAI"), _source_def("anthropic_blog", "Anthropic")
        )

        history = recent_history(
            publications=PublicationRepository(connection),
            drafts=DraftRepository(connection),
            articles=ArticleRepository(connection),
            evaluations=EvaluationRepository(connection),
            sources=registry,
        )

        assert [p.source_family for p in history] == ["Anthropic", "OpenAI"]

    def test_a_source_removed_from_the_registry_yields_no_source_family(
        self, connection: sqlite3.Connection
    ) -> None:
        _publish_one(
            connection, source_id="retired_source", editorial_category=EditorialCategory.NEWS
        )
        empty_registry = _registry(_source_def("placeholder_source", "Placeholder"))

        history = recent_history(
            publications=PublicationRepository(connection),
            drafts=DraftRepository(connection),
            articles=ArticleRepository(connection),
            evaluations=EvaluationRepository(connection),
            sources=empty_registry,
        )

        assert history == [RecentPost(EditorialCategory.NEWS, None)]

    def test_respects_the_limit(self, connection: sqlite3.Connection) -> None:
        for i in range(5):
            _publish_one(
                connection, source_id=f"source_{i}_{uuid4().hex[:4]}", editorial_category=None
            )
        registry = _registry(
            *[_source_def(f"source_{i}_x", "X") for i in range(5)]
        )
        history = recent_history(
            publications=PublicationRepository(connection),
            drafts=DraftRepository(connection),
            articles=ArticleRepository(connection),
            evaluations=EvaluationRepository(connection),
            sources=registry,
            limit=2,
        )
        assert len(history) == 2
