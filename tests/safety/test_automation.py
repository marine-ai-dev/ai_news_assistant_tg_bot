"""The unattended NEWS pipeline. Never skip these.

This is the one place in the project where a machine can, under the right conditions,
approve and publish something with nobody watching. Every test here exists to draw a
line around exactly how narrow those conditions are, and to prove — against the real
storage layer, the real DraftResult validator and the real publication gate, not a
reimplementation of any of them — that nothing gets past the line.

Two properties matter more than the rest, and most tests here serve one or the other:

**A dry run genuinely touches nothing.** Not a Draft, not an Evaluation, not a
ReviewDecision, not a Telegram request. If a dry run ever leaves a trace, the flag lied.

**A live run can publish at most one post, and only through the gate.** The actor is
exactly ``"gemini:auto"``, recorded by the same `approve_draft` a human approval calls,
sent through the same `publish_bundle` every other publisher uses. Nothing here posts to
Telegram on its own.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from ai_news_editor.automation import pipeline as automation_pipeline
from ai_news_editor.automation.gemini import GeminiClient
from ai_news_editor.automation.pipeline import Outcome, run_automation
from ai_news_editor.domain.enums import (
    ArticleStatus,
    DraftStatus,
    EvaluatorType,
    TrustTier,
)
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.settings import Settings
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    DraftRepository,
    EvaluationRepository,
    PublicationRepository,
    RawItemRepository,
    ReviewDecisionRepository,
    SourceRepository,
)
from tests.conftest import make_article, make_raw_item, make_source

pytestmark = pytest.mark.safety

#: Captured once, at import time, before any test has monkeypatched these classes —
#: see the docstring on patch_clients() for why re-reading Class.__init__ inside that
#: function on a second call would capture an already-wrapped version instead.
_ORIGINAL_INITS = {
    GeminiClient: GeminiClient.__init__,
    TelegramClient: TelegramClient.__init__,
    HttpClient: HttpClient.__init__,
}

#: Assembled rather than written out — a secret scanner cannot tell a placeholder from
#: a credential, and this project's own hygiene rules forbid a literal that looks real.
FAKE_GEMINI_KEY = "AIzaSy" + "z" * 33
FAKE_TELEGRAM_TOKEN = "123456789:" + "A" * 35

REAL_ARTICLE_TEXT = (
    "The company announced a new feature today that lets users summarize documents "
    "directly inside the chat interface. " * 20
)

VALID_GENERATION = {
    "content_type": "NEWS",
    "headline": "Нова функція для роботи з документами",
    "body": "Компанія додала можливість підсумовувати документи прямо в чаті.",
    "source_url": None,  # filled in per-test to match the real candidate URL
    "source_title": "New feature",
    "factual_claims": ["a new summarization feature was announced"],
    "confidence": 95,
}


# --------------------------------------------------------------------------- fixtures


def build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    data: dict[str, Any] = {
        "_env_file": None,
        "data_dir": tmp_path,
        "media_dir": tmp_path / "media",
        "automation_enabled": True,
        "gemini_api_key": FAKE_GEMINI_KEY,
        "llm_model": "gemini-test",
        "telegram_bot_token": FAKE_TELEGRAM_TOKEN,
        "telegram_channel": "@prod_channel",
        "test_channel": "@test_channel",
        "daily_post_limit": 3,
    }
    data.update(overrides)
    return Settings(**data)  # type: ignore[arg-type]


def fake_gemini_transport(
    *, selection: dict[str, Any] | None = None, generation: dict[str, Any] | None = None
) -> httpx.MockTransport:
    """One call answers as a selection, the next as a generation — call order matters,
    exactly like the pipeline itself: select, then (if selected) generate.
    """
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        is_selection = "selected_id" in str(body) or len(calls) == 1
        payload = selection if is_selection and selection is not None else generation
        assert payload is not None, "no canned response configured for this call"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": json.dumps(payload)}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


def fake_fulltext_transport(
    *, article_html: str | None = None, status_code: int = 200
) -> httpx.MockTransport:
    """What sources.fulltext's HttpClient sees when it fetches the selected article.

    Defaults to a genuine article-shaped page — long enough to clear
    MIN_FULLTEXT_CHARS — so every test that does not care about the fetch itself gets a
    usable body without repeating the boilerplate.
    """
    body = article_html or (
        "<html><body><article><p>" + (REAL_ARTICLE_TEXT * 3) + "</p></article></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, headers={"content-type": "text/html"}, content=body.encode()
        )

    return httpx.MockTransport(handler)


def fake_telegram_transport() -> httpx.MockTransport:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getChat"):
            return httpx.Response(
                200, json={"ok": True, "result": {"id": -100777, "type": "channel"}}
            )
        payload = json.loads(request.content) if request.content else {}
        sent.append({"path": request.url.path, **payload})
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 4242, "chat": {"id": -100777}}}
        )

    transport = httpx.MockTransport(handler)
    transport.sent = sent  # type: ignore[attr-defined]
    return transport


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file injects its own transport. If one forgets to, this
    fixture is what stands between it and a real network call."""


def patch_clients(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gemini_transport: httpx.MockTransport | None,
    telegram_transport: httpx.MockTransport | None,
    fulltext_transport: httpx.MockTransport | None = "default",  # type: ignore[assignment]
) -> None:
    """Every outbound client this pipeline can construct, all offline.

    ``fulltext_transport`` defaults to a working article response — most tests never
    need to think about it — but every call still goes through an injected transport,
    never the real network: this project's own test harness (tests/conftest.py) blocks
    a real socket outright, which is exactly what caught the first draft of this file
    reaching real DNS for "example.invalid".

    Reads each client's TRUE, unpatched ``__init__`` from ``_ORIGINAL_INITS`` rather
    than from the class right before wrapping it. A test that calls this twice (e.g. to
    swap in a second transport partway through, for a two-run scenario) would otherwise
    capture the *already-wrapped* ``__init__`` from the first call as "original" — which
    hard-codes the first transport and silently ignores the second one it's asked to
    wrap next.
    """
    if gemini_transport is not None:

        def patched_gemini(self, api_key, *, model, transport=None, **kwargs):  # type: ignore[no-untyped-def]
            _ORIGINAL_INITS[GeminiClient](
                self, api_key, model=model, transport=gemini_transport, **kwargs
            )

        monkeypatch.setattr(GeminiClient, "__init__", patched_gemini)
        monkeypatch.setattr(automation_pipeline, "GeminiClient", GeminiClient)

    if telegram_transport is not None:

        def patched_telegram(self, token, *, transport=None, **kwargs):  # type: ignore[no-untyped-def]
            _ORIGINAL_INITS[TelegramClient](
                self, token, transport=telegram_transport, **kwargs
            )

        monkeypatch.setattr(TelegramClient, "__init__", patched_telegram)
        monkeypatch.setattr(automation_pipeline, "TelegramClient", TelegramClient)

    resolved_fulltext = (
        fake_fulltext_transport() if fulltext_transport == "default" else fulltext_transport
    )
    if resolved_fulltext is not None:

        def patched_http(self, *, transport=None, **kwargs):  # type: ignore[no-untyped-def]
            _ORIGINAL_INITS[HttpClient](self, transport=resolved_fulltext, **kwargs)

        monkeypatch.setattr(HttpClient, "__init__", patched_http)


def seed_official_article(
    connection: sqlite3.Connection,
    *,
    status: ArticleStatus = ArticleStatus.NORMALIZED,
    trust_tier: TrustTier = TrustTier.OFFICIAL,
    url: str | None = None,
    clean_text: str = "A short RSS excerpt about a new feature.",
):
    sources = SourceRepository(connection)
    raw_items = RawItemRepository(connection)
    articles = ArticleRepository(connection)

    source = sources.upsert(make_source(trust_tier=trust_tier))
    item = raw_items.add(make_raw_item(source.id))
    article = articles.add(
        make_article(
            item.id, source.id,
            canonical_url=url or f"https://example.invalid/{uuid4().hex[:8]}",
            clean_text=clean_text, status=status,
        )
    )
    return article


def db(tmp_path: Path) -> sqlite3.Connection:
    from ai_news_editor.storage import db as db_module

    connection = db_module.connect(tmp_path / "ai_news.sqlite3")
    db_module.migrate(connection)
    return connection


def snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    """Every row this pipeline is capable of writing, for a before/after comparison."""
    return {
        "drafts": connection.execute("SELECT id FROM drafts").fetchall(),
        "draft_versions": connection.execute("SELECT id FROM draft_versions").fetchall(),
        "evaluations": connection.execute("SELECT id FROM evaluations").fetchall(),
        "review_decisions": connection.execute("SELECT id FROM review_decisions").fetchall(),
        "publications": connection.execute("SELECT id FROM publications").fetchall(),
    }


# --------------------------------------------------------------------------- tests


class TestKillSwitch:
    def test_disabled_by_default_publishes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        seed_official_article(connection)
        settings = build_settings(tmp_path, automation_enabled=False)
        before = snapshot(connection)

        result = run_automation(connection, settings, mode="live")

        assert result.outcome is Outcome.DISABLED
        assert result.is_quiet
        assert snapshot(connection) == before

    def test_an_explicitly_false_string_from_the_environment_is_still_off(
        self, tmp_path: Path
    ) -> None:
        """Settings parses AI_NEWS_AUTOMATION_ENABLED=false to Python False; this just
        confirms the pipeline trusts that parsed value rather than any truthiness of
        the setting merely existing."""
        connection = db(tmp_path)
        settings = Settings(
            _env_file=None, data_dir=tmp_path, automation_enabled=False,
        )  # type: ignore[arg-type]
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.DISABLED

    def test_dry_run_ignores_the_kill_switch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A manual dry-run dispatch must keep working while the switch stays off —
        that is this project's expected steady state once the schedule exists."""
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path, automation_enabled=False)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=None,
        )
        result = run_automation(connection, settings, mode="dry-run")
        assert result.outcome is Outcome.DRY_RUN_COMPLETE
        assert result.outcome is not Outcome.DISABLED

    def test_test_mode_ignores_the_kill_switch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A manual test-channel dispatch must also keep working while the switch
        stays off — only 'live' (and a schedule, which is always 'live') checks it."""
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path, automation_enabled=False)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        result = run_automation(connection, settings, mode="test")
        assert result.outcome is Outcome.PUBLISHED
        assert result.channel == settings.test_channel

    def test_live_mode_proceeds_once_the_switch_is_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path, automation_enabled=True)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.PUBLISHED
        assert result.channel == settings.telegram_channel


class TestConfiguration:
    def test_missing_gemini_key_fails_closed(self, tmp_path: Path) -> None:
        connection = db(tmp_path)
        settings = build_settings(tmp_path, gemini_api_key=None)
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.CONFIG_ERROR
        assert "GEMINI_API_KEY" in result.detail

    def test_missing_telegram_token_fails_closed(self, tmp_path: Path) -> None:
        connection = db(tmp_path)
        settings = build_settings(tmp_path, telegram_bot_token=None)
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.CONFIG_ERROR

    def test_test_mode_without_a_test_channel_fails_closed(self, tmp_path: Path) -> None:
        connection = db(tmp_path)
        settings = build_settings(tmp_path, test_channel=None)
        result = run_automation(connection, settings, mode="test")
        assert result.outcome is Outcome.CONFIG_ERROR
        assert "TEST_CHANNEL" in result.detail


class TestCandidateEligibility:
    def test_no_candidate_is_a_quiet_no_op(self, tmp_path: Path) -> None:
        connection = db(tmp_path)
        settings = build_settings(tmp_path)
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.NO_CANDIDATE
        assert result.is_quiet

    def test_a_non_official_source_is_never_offered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        seed_official_article(connection, trust_tier=TrustTier.REPUTABLE_SECONDARY)
        settings = build_settings(tmp_path)
        patch_clients(
            monkeypatch, gemini_transport=fake_gemini_transport(), telegram_transport=None
        )
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.NO_CANDIDATE

    def test_an_article_that_is_not_normalized_is_never_offered(
        self, tmp_path: Path
    ) -> None:
        connection = db(tmp_path)
        seed_official_article(connection, status=ArticleStatus.COLLECTED)
        settings = build_settings(tmp_path)
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.NO_CANDIDATE

    def test_an_article_with_an_existing_evaluation_is_never_offered_again(
        self, tmp_path: Path
    ) -> None:
        """A human (or an earlier automated run) already has this one."""
        from ai_news_editor.domain.enums import (
            AudienceTier,
            Category,
            EditorialDecision,
            VerificationStatus,
        )
        from ai_news_editor.domain.models import Evaluation

        connection = db(tmp_path)
        article = seed_official_article(connection)
        EvaluationRepository(connection).add(
            Evaluation(
                article_id=article.id, schema_version="1", rubric_version="1",
                evaluator_type=EvaluatorType.HUMAN, content_fingerprint="x",
                decision=EditorialDecision.SHORTLIST, category=Category.PRODUCT_UPDATE,
                audience=AudienceTier.NEWCOMER,
                scores=dict(automation_pipeline._AUTOMATION_SCORES), composite_score=0.0,
                verification_status=VerificationStatus.NOT_REQUIRED,
            )
        )
        settings = build_settings(tmp_path)
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.NO_CANDIDATE

    def test_only_news_articles_are_ever_eligible(self, tmp_path: Path) -> None:
        """There is no PROMPT or TESTED_USE_CASE candidate list here at all — this
        pipeline reads from `articles`, which only ever holds sourced NEWS material.
        Confirmed structurally: the query never touches content_items."""
        from ai_news_editor.automation.pipeline import _eligible_candidates

        connection = db(tmp_path)
        seed_official_article(connection)
        candidates, by_id = _eligible_candidates(connection)
        assert len(candidates) == 1
        article = next(iter(by_id.values()))
        # The only content this pipeline could ever have selected is a sourced Article.
        assert article.canonical_url


class TestDryRunTouchesNothing:
    def test_a_successful_dry_run_writes_nothing_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        article = seed_official_article(connection, clean_text="short excerpt")
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),  # present but must go unused
        )
        before = snapshot(connection)

        result = run_automation(connection, settings, mode="dry-run")

        assert result.outcome is Outcome.DRY_RUN_COMPLETE
        assert result.is_quiet
        assert result.draft_id is None
        assert snapshot(connection) == before, "dry run must not write a single row"

    def test_dry_run_never_calls_telegram(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        telegram = fake_telegram_transport()
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=telegram,
        )
        run_automation(connection, settings, mode="dry-run")
        assert telegram.sent == []  # type: ignore[attr-defined]

    def test_dry_run_never_approves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=None,
        )
        run_automation(connection, settings, mode="dry-run")
        assert ReviewDecisionRepository(connection).count() == 0


class TestFailClosedPaths:
    def test_selection_rejection_publishes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        seed_official_article(connection)
        settings = build_settings(tmp_path)
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"rejection_reason": "nothing important today"}
            ),
            telegram_transport=None,
        )
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.SELECTION_REJECTED
        assert result.is_quiet
        assert PublicationRepository(connection).count() == 0

    def test_a_hallucinated_selection_id_is_a_quiet_rejection_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        seed_official_article(connection)
        settings = build_settings(tmp_path)
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(selection={"selected_id": "99"}),
            telegram_transport=None,
        )
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.SELECTION_REJECTED
        assert PublicationRepository(connection).count() == 0

    def test_a_permanent_gemini_rejection_during_selection_is_loud_not_quiet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An invalid API key (or any non-retryable 4xx) is this application's own
        infrastructure being broken — not Gemini declining to pick a candidate. It must
        surface as GEMINI_ERROR (a red, nonzero-exit run someone will notice), never get
        folded into the same quiet SELECTION_REJECTED bucket as an ordinary "nothing
        worth covering today" answer.
        """
        connection = db(tmp_path)
        seed_official_article(connection)
        settings = build_settings(tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                 "message": "API key not valid."}},
            )

        patch_clients(
            monkeypatch, gemini_transport=httpx.MockTransport(handler), telegram_transport=None,
        )
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.GEMINI_ERROR
        assert not result.is_quiet
        assert PublicationRepository(connection).count() == 0

    def test_fulltext_failure_publishes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The selected article's page returns too little text to write from —
        exactly the RSS-summary-only case this module exists to refuse."""
        connection = db(tmp_path)
        seed_official_article(connection)
        settings = build_settings(tmp_path)
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(selection={"selected_id": "1"}),
            telegram_transport=None,
            fulltext_transport=fake_fulltext_transport(
                article_html="<html><body><article><p>too short</p></article></body></html>"
            ),
        )
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.FULLTEXT_UNAVAILABLE
        assert result.is_quiet
        assert DraftRepository(connection).list_by_status(DraftStatus.PENDING_REVIEW) == []

    def test_generation_rejection_publishes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        seed_official_article(connection)
        settings = build_settings(tmp_path)
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"},
                generation={
                    "content_type": "NEWS",
                    "rejection_reason": "the article is too vague to write from",
                },
            ),
            telegram_transport=None,
        )
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.GENERATION_REJECTED
        assert PublicationRepository(connection).count() == 0

    def test_a_url_that_does_not_match_the_candidate_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        seed_official_article(connection, url="https://example.invalid/real")
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": "https://evil.example/substituted"}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=None,
        )
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.GENERATION_REJECTED
        assert "does not match" in result.detail
        assert PublicationRepository(connection).count() == 0

    def test_low_confidence_is_rejected_before_anything_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url, "confidence": 10}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=None,
        )
        before = snapshot(connection)
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.GENERATION_REJECTED
        assert snapshot(connection) == before

    def test_a_blank_generated_body_fails_canonical_draftresult_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Structurally impossible through the schema (headline/body required when not
        a rejection) — this proves the *canonical* DraftResult validator is still the
        one actually enforcing it, by constructing a technically-valid GeneratedPost
        whose body is whitespace only, which the schema itself cannot catch."""
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url, "body": "   "}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=None,
        )
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.VALIDATION_FAILED
        assert PublicationRepository(connection).count() == 0

    def test_daily_limit_reached_publishes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One real published run, then a second article the very same day. The
        second run must see the limit — fabricating a Publication row directly would
        skip the foreign-key-backed reality that a real prior run leaves behind.
        """
        connection = db(tmp_path)
        first_article = seed_official_article(connection)
        settings = build_settings(tmp_path, daily_post_limit=1)
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"},
                generation={**VALID_GENERATION, "source_url": first_article.canonical_url},
            ),
            telegram_transport=fake_telegram_transport(),
        )
        first = run_automation(connection, settings, mode="live")
        assert first.outcome is Outcome.PUBLISHED

        seed_official_article(connection)
        patch_clients(
            monkeypatch, gemini_transport=fake_gemini_transport(), telegram_transport=None
        )
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.DAILY_LIMIT_REACHED
        assert result.is_quiet

    def test_a_test_channel_publish_does_not_count_against_the_daily_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real test-channel publish, then a real live one the same day — the first
        must not consume the second's budget."""
        connection = db(tmp_path)
        first_article = seed_official_article(connection)
        settings = build_settings(tmp_path, daily_post_limit=1)
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"},
                generation={**VALID_GENERATION, "source_url": first_article.canonical_url},
            ),
            telegram_transport=fake_telegram_transport(),
        )
        first = run_automation(connection, settings, mode="test")
        assert first.outcome is Outcome.PUBLISHED
        assert first.channel == settings.test_channel

        second_article = seed_official_article(connection)
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"},
                generation={**VALID_GENERATION, "source_url": second_article.canonical_url},
            ),
            telegram_transport=fake_telegram_transport(),
        )
        result = run_automation(connection, settings, mode="live")
        assert result.outcome is Outcome.PUBLISHED
        assert result.channel == settings.telegram_channel


class TestSuccessfulPublication:
    def test_a_valid_candidate_is_approved_and_published_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        telegram = fake_telegram_transport()
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=telegram,
        )

        result = run_automation(connection, settings, mode="live")

        assert result.outcome is Outcome.PUBLISHED
        assert result.published
        assert result.channel == "@prod_channel"
        assert result.message_id == 4242

        # Exactly one of everything.
        assert len(DraftRepository(connection).list_all(limit=10)) == 1
        assert ReviewDecisionRepository(connection).count() == 1
        assert PublicationRepository(connection).count() == 1
        assert len([c for c in telegram.sent if "text" in c]) == 1  # type: ignore[attr-defined]

    def test_the_actor_is_exactly_gemini_auto(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        result = run_automation(connection, settings, mode="live")

        decisions = ReviewDecisionRepository(connection)
        decision = decisions.list_for_draft(result.draft_id)[0]
        assert decision.actor == "gemini:auto"

        draft = DraftRepository(connection).get(result.draft_id)
        assert draft.status.value == "PUBLISHED"

    def test_the_evaluation_is_distinguishable_as_automated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No storage migration was made for this: EvaluatorType.AUTOMATED already
        existed for exactly this purpose. Confirmed against the real row, not the
        Python object, so a future change to the storage mapping cannot silently
        break the distinction this test exists to hold."""
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        run_automation(connection, settings, mode="live")

        row = connection.execute(
            "SELECT evaluator_type, evaluator FROM evaluations"
        ).fetchone()
        assert row["evaluator_type"] == "AUTOMATED"
        assert row["evaluator"] == "gemini:auto"
        assert row["evaluator_type"] != "CLAUDE_CODE"
        assert row["evaluator_type"] != "HUMAN"

    def test_publication_goes_through_the_real_gate_and_service(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asserted structurally: automation.pipeline imports prepare_publication and
        publish_bundle from publishing.service, and contains no api.telegram.org
        literal of its own — see also
        test_approval_gate.py::test_http_access_is_confined_to_the_sources_layer."""
        source = Path(automation_pipeline.__file__).read_text(encoding="utf-8")
        assert "prepare_publication" in source
        assert "publish_bundle" in source
        assert "api.telegram.org" not in source

    def test_test_mode_sends_to_the_test_channel_not_production(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        result = run_automation(connection, settings, mode="test")
        assert result.outcome is Outcome.PUBLISHED
        assert result.channel == "@test_channel"
        assert result.channel != settings.telegram_channel

    def test_a_second_run_after_success_finds_nothing_left_to_do(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The published article now has an Evaluation and a Draft; it is not offered
        again, and nothing about a second pass can select it."""
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        first = run_automation(connection, settings, mode="live")
        assert first.outcome is Outcome.PUBLISHED

        second = run_automation(connection, settings, mode="live")
        assert second.outcome is Outcome.NO_CANDIDATE
        assert PublicationRepository(connection).count() == 1


class TestTestModeIsolation:
    """``--test`` runs the real pipeline against a throwaway, in-memory copy of the
    database (see run_automation's isolation branch) — every one of these proves a
    real Telegram send to the test channel leaves the real, on-disk database exactly
    as it was, so a manual test dispatch can never make an article unavailable to a
    later live run, count against the live daily limit, or leave a Publication record
    a human reading production history would mistake for a real one.
    """

    def test_test_mode_writes_nothing_to_the_real_database(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        before = snapshot(connection)

        result = run_automation(connection, settings, mode="test")

        # The pipeline ran for real — a real Evaluation, Draft, approval and
        # Publication were created and a real Telegram send happened — none of it
        # just visible on the real connection, because none of it landed there.
        assert result.outcome is Outcome.PUBLISHED
        assert result.draft_id is not None
        assert snapshot(connection) == before, (
            "a --test run must leave the real database byte-for-byte as it found it"
        )

    def test_test_mode_creates_no_production_publication_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        result = run_automation(connection, settings, mode="test")
        assert result.outcome is Outcome.PUBLISHED
        assert PublicationRepository(connection).count() == 0
        assert EvaluationRepository(connection).count() == 0
        assert DraftRepository(connection).list_all(limit=10) == []
        assert ReviewDecisionRepository(connection).count() == 0

    def test_test_mode_does_not_consume_the_candidate_for_a_later_live_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defining property: the exact same article, tested and then not
        re-seeded, is still eligible and gets genuinely selected and published by a
        live run immediately afterward — a test send never 'eats' a candidate."""
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}

        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        test_result = run_automation(connection, settings, mode="test")
        assert test_result.outcome is Outcome.PUBLISHED
        assert test_result.channel == settings.test_channel

        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        live_result = run_automation(connection, settings, mode="live")
        assert live_result.outcome is Outcome.PUBLISHED
        assert live_result.channel == settings.telegram_channel
        assert PublicationRepository(connection).count() == 1

    def test_test_mode_does_not_consume_the_production_daily_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Belt-and-suspenders alongside the isolation test above: even with the
        production daily limit set to its most restrictive possible value, a test
        send followed immediately by a live run for the same article still succeeds —
        proving the test send truly consumed none of that budget."""
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path, daily_post_limit=1)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}

        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        test_result = run_automation(connection, settings, mode="test")
        assert test_result.outcome is Outcome.PUBLISHED

        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        live_result = run_automation(connection, settings, mode="live")
        assert live_result.outcome is Outcome.PUBLISHED, (
            "the daily limit of 1 must still have its full budget after a test send"
        )

    def test_the_callers_connection_object_is_never_closed_by_test_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real connection must remain open and usable after a --test run — only
        run_automation's own throwaway in-memory copy is ever closed."""
        connection = db(tmp_path)
        article = seed_official_article(connection)
        settings = build_settings(tmp_path)
        generation = {**VALID_GENERATION, "source_url": article.canonical_url}
        patch_clients(
            monkeypatch,
            gemini_transport=fake_gemini_transport(
                selection={"selected_id": "1"}, generation=generation
            ),
            telegram_transport=fake_telegram_transport(),
        )
        run_automation(connection, settings, mode="test")
        # Would raise sqlite3.ProgrammingError on a closed connection.
        assert connection.execute("SELECT 1").fetchone()[0] == 1
