"""``ai-news editorial preview`` — Step 3 section 29.

Runs against the real shipped config/sources.yaml, same pattern as
TestSourcesCommand in tests/integration/test_cli.py.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_news_editor.cli.main import app
from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    EditorialCategory,
    EditorialDecision,
    EditorialEvidence,
    EvaluatorType,
    VerificationStatus,
)
from ai_news_editor.domain.models import Evaluation
from ai_news_editor.settings import get_settings
from ai_news_editor.storage import db
from tests.conftest import make_article, make_raw_item, make_source

runner = CliRunner()
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"

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


def output_of(result: object) -> str:
    parts = [getattr(result, "output", "") or ""]
    with contextlib.suppress(AttributeError, ValueError):
        parts.append(result.stderr or "")  # type: ignore[attr-defined]
    return " ".join(" ".join(parts).split())


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AI_NEWS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AI_NEWS_SOURCES_CONFIG_PATH", str(REPO_CONFIG))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed_shortlisted(tmp_path: Path) -> None:
    runner.invoke(app, ["db", "init"])
    connection = db.connect(tmp_path / "ai_news.sqlite3")
    from ai_news_editor.storage.repositories import (
        ArticleRepository,
        EvaluationRepository,
        RawItemRepository,
        SourceRepository,
    )

    sources = SourceRepository(connection)
    raw_items = RawItemRepository(connection)
    articles = ArticleRepository(connection)
    evaluations = EvaluationRepository(connection)

    sources.upsert(make_source("openai_news"))
    item = raw_items.add(make_raw_item("openai_news"))
    article = articles.add(make_article(item.id, "openai_news", title="OpenAI ships something"))

    evaluations.add(
        Evaluation(
            article_id=article.id,
            schema_version="1",
            rubric_version="v1",
            evaluator_type=EvaluatorType.HUMAN,
            evaluator="marina",
            content_fingerprint="fp-preview-cli",
            decision=EditorialDecision.SHORTLIST,
            category=Category.PRODUCT_UPDATE,
            audience=AudienceTier.BEGINNER,
            scores=_SCORES,
            composite_score=9.0,
            verification_status=VerificationStatus.VERIFIED,
            editorial_category=EditorialCategory.NEWS,
            evidence_type=EditorialEvidence.PRIMARY_SOURCE,
        )
    )
    connection.close()


class TestEditorialPreviewCommand:
    def test_with_nothing_shortlisted_it_says_so(self) -> None:
        runner.invoke(app, ["db", "init"])
        result = runner.invoke(app, ["editorial", "preview"])
        assert result.exit_code == 0
        assert "Nothing shortlisted yet" in output_of(result)

    def test_a_shortlisted_candidate_is_shown_with_its_classification(
        self, tmp_path: Path
    ) -> None:
        _seed_shortlisted(tmp_path)
        result = runner.invoke(app, ["editorial", "preview"])

        assert result.exit_code == 0
        # Title text is asserted separately: Rich wraps a narrow table column under
        # the test runner's terminal width, same caveat test_cli.py's own
        # test_every_source_declares_an_editorial_role notes for column rendering.
        output = output_of(result)
        assert "openai_news" in output
        assert "NEWS" in output
        assert "PRIMARY_SOURCE" in output
        assert "ok" in output.lower()

    def test_it_never_calls_gemini_or_telegram(self, tmp_path: Path) -> None:
        """No network fixture is wired at all — the autouse _no_network fixture in
        conftest would fail the test outright if this command tried to reach out."""
        _seed_shortlisted(tmp_path)
        result = runner.invoke(app, ["editorial", "preview"])
        assert result.exit_code == 0
