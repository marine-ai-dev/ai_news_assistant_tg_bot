"""Real, TEST-channel-only v2 soak runner — Step 6 (AI News Agent v2).

Wires real, already-collected candidates through the actual v2 pipeline —
``automation.pipeline_v2`` (capability + diversity + primary-source preference +
classification/generation), ``media.pipeline`` (discovery/download/policy),
``rendering`` (category rendering + publication planning) — and sends the result to
``AI_NEWS_TEST_CHANNEL`` for real. Never the production channel, never the canonical
on-disk database: every call in here is expected to run against the same isolated
in-memory connection ``automation.pipeline.isolated_connection`` already builds for
``--test`` mode, exactly like the existing v1 ``ai-news auto once --test`` path.

Broader than the live v1 candidate pool on purpose: ``automation.pipeline._eligible_candidates``
filters to ``TrustTier.OFFICIAL`` only, because the NEWS-only v1 generator only ever
grounds a post in a primary source. v2 categories (AI_LIFEHACK, PROMPT_WORKFLOW, ...)
legitimately draw on Tier B/C sources, gated instead by ``sources.capability`` per
category — so ``eligible_articles_v2`` reads every trust tier and leaves the gating to
the pipeline itself.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ai_news_editor.automation import test_history
from ai_news_editor.automation.gemini import GeminiClient
from ai_news_editor.automation.pipeline_v2 import (
    ArticleContext,
    OrchestrationOutcome,
    OrchestrationRejected,
    collapse_to_primary_sources,
    run_pipeline_v2,
)
from ai_news_editor.domain.enums import ArticleStatus, EditorialCategory, TrustTier
from ai_news_editor.domain.errors import ConfigurationError
from ai_news_editor.domain.models import Article
from ai_news_editor.editorial.diversity import RecentPost
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.publishing.rich import ComponentOutcome, run_step
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.rendering.plan import PlanVariant, build_publication_plan
from ai_news_editor.sources.capability import allows_category
from ai_news_editor.sources.config import SourcesConfig
from ai_news_editor.sources.fulltext import fetch_fulltext
from ai_news_editor.sources.geography import is_source_eligible
from ai_news_editor.sources.http import HttpClient, HttpError
from ai_news_editor.sources.http import UnsafeUrlError as UnsafeArticleUrlError
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    DraftRepository,
    EvaluationRepository,
    RawItemRepository,
)

logger = get_logger(__name__)

#: How many fulltext fetches one soak attempt is willing to spend building a candidate
#: pool for a single post — bounded so a large backlog cannot turn one attempt into an
#: unbounded number of outbound HTTP calls.
MAX_CANDIDATES_PER_ATTEMPT = 15

#: Wall-clock pause between posts — never a flood, never a persistent schedule.
DEFAULT_DELAY_SECONDS = 3.0


def eligible_articles_v2(
    connection: sqlite3.Connection,
    *,
    exclude_article_ids: frozenset[UUID] = frozenset(),
    exclude_source_urls: frozenset[str] = frozenset(),
    limit: int = 60,
) -> list[Article]:
    """Real, already-normalized articles not yet drafted or evaluated.

    Every trust tier — unlike the live v1 pool, which is OFFICIAL-only. Newest first.

    ``exclude_source_urls`` is how cross-run test dedup (``automation.test_history``)
    keeps two independent ``--test`` soak invocations from picking the same article —
    each ephemeral in-memory copy starts from an identical canonical snapshot with no
    memory of its own of what an earlier run already sent, so that memory has to come
    from outside the database entirely (see that module's own docstring).
    """
    articles = ArticleRepository(connection)
    drafts = DraftRepository(connection)
    evaluations = EvaluationRepository(connection)

    rows = articles.list_by_status(ArticleStatus.NORMALIZED, limit=limit)
    rows.sort(key=lambda a: a.published_at or a.created_at, reverse=True)

    eligible: list[Article] = []
    for article in rows:
        if article.id in exclude_article_ids:
            continue
        if article.canonical_url in exclude_source_urls:
            continue
        if drafts.find_by_article(article.id) is not None:
            continue
        if evaluations.latest_for_article(article.id) is not None:
            continue
        eligible.append(article)
    return eligible


def build_article_context(
    connection: sqlite3.Connection,
    article: Article,
    registry: SourcesConfig,
    *,
    http: HttpClient,
) -> ArticleContext | None:
    """Fetch what the v2 pipeline needs for one article, or ``None`` if unusable.

    A fulltext-fetch failure is a normal, expected outcome for some fraction of any
    real candidate pool (paywalls, transient errors) — this returns ``None`` rather
    than raising, exactly like ``sources.fulltext.fetch_fulltext`` itself never raises
    for that reason.
    """
    try:
        source_def = registry.get(article.source_id)
    except ConfigurationError:
        return None

    fulltext = fetch_fulltext(article.canonical_url, http=http)
    if not fulltext.ok or not fulltext.text:
        return None

    html: str | None = None
    try:
        response = http.get(article.canonical_url)
        html = response.body.decode("utf-8", errors="replace")
    except (HttpError, UnsafeArticleUrlError):
        html = None  # media discovery just gets less to work with; never fatal here

    raw_item = RawItemRepository(connection).get(article.raw_item_id)

    return ArticleContext(
        article_id=article.id,
        title=article.title,
        source_id=article.source_id,
        editorial_category=None,
        evidence_type=None,
        composite_score=50.0,
        article_text=fulltext.text,
        source_url=article.canonical_url,
        source_label=source_def.name,
        feed_payload_raw=raw_item.payload_raw,
        html=html,
    )


@dataclass(frozen=True, slots=True)
class SoakPostResult:
    """Everything the acceptance report needs about one real sent post."""

    index: int
    outcome: OrchestrationOutcome
    variant: PlanVariant
    component_outcomes: tuple[ComponentOutcome, ...]
    article_id: UUID
    source_id: str


def run_soak(
    connection: sqlite3.Connection,
    *,
    client: GeminiClient,
    registry: SourcesConfig,
    http: HttpClient,
    telegram: TelegramClient,
    target_channel: str,
    count: int,
    prefer_category: EditorialCategory | None = None,
    recent_seed: list[RecentPost] | None = None,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    test_history_path: Path | None = None,
) -> list[SoakPostResult]:
    """Send up to ``count`` real posts to ``target_channel``, one at a time.

    Sequential by construction — no concurrency, a pause between sends. Each
    successful post is excluded from the next attempt's candidate pool, and folded
    into ``recent`` so diversity actually varies across the batch. A candidate whose
    classification/generation is rejected is marked used (not retried) so one
    unusable article cannot loop the batch forever; the run simply moves on.

    ``prefer_category``, if given, narrows the candidate pool to sources whose
    registry entry declares that category's capability — never forces the pipeline's
    real classification result, and falls back to the unfiltered pool if nothing
    matches (never blocks the batch on an unmet preference).

    ``test_history_path``, if given, makes this run cross-run-aware: articles already
    recorded there are excluded from the pool up front, and every real send is
    recorded there too — see ``automation.test_history`` for why an ephemeral
    in-memory ``--test`` database cannot provide this memory on its own.
    """
    sources_by_id = {source.id: source for source in registry.sources}
    already_sent_urls = frozenset(
        entry.source_url for entry in test_history.load(test_history_path)
    ) if test_history_path is not None else frozenset()

    def trust_tier_of(article: Article) -> TrustTier:
        source = sources_by_id.get(article.source_id)
        return source.trust_tier if source else TrustTier.UNVERIFIED

    used: set[UUID] = set()
    recent: list[RecentPost] = list(recent_seed or [])
    results: list[SoakPostResult] = []

    max_attempts = count * 4
    attempts = 0
    while len(results) < count and attempts < max_attempts:
        attempts += 1
        pool = eligible_articles_v2(
            connection,
            exclude_article_ids=frozenset(used),
            exclude_source_urls=already_sent_urls,
        )
        if not pool:
            logger.info("soak_exhausted", extra={"posts_sent": len(results)})
            break

        pool = collapse_to_primary_sources(pool, trust_tier_of)

        # Step 6B: geography allowlist enforced here, locally and deterministically,
        # before any candidate reaches Gemini selection — never left to Gemini, and
        # never based on the article's subject, only the source's own reviewed origin.
        geography_ineligible = {
            article.id
            for article in pool
            if article.source_id not in sources_by_id
            or not is_source_eligible(sources_by_id[article.source_id])
        }
        if geography_ineligible:
            used.update(geography_ineligible)
            pool = [a for a in pool if a.id not in geography_ineligible]
        if not pool:
            continue

        if prefer_category is not None:
            narrowed = [
                a
                for a in pool
                if a.source_id in sources_by_id
                and allows_category(sources_by_id[a.source_id], prefer_category)
            ]
            pool = narrowed or pool

        contexts: list[ArticleContext] = []
        for article in pool[:MAX_CANDIDATES_PER_ATTEMPT]:
            context = build_article_context(connection, article, registry, http=http)
            if context is not None:
                contexts.append(context)
        if not contexts:
            # Every article in this slice failed fulltext fetch — mark them used so
            # the next attempt sees fresh candidates instead of retrying the same dead
            # batch.
            used.update(a.id for a in pool[:MAX_CANDIDATES_PER_ATTEMPT])
            continue

        with MediaWorkspace(label=f"soak-{len(results) + 1}") as workspace:
            try:
                outcome = run_pipeline_v2(
                    client=client,
                    candidates=contexts,
                    sources_by_id=sources_by_id,
                    recent=recent,
                    workspace=workspace,
                    http=http,
                )
            except OrchestrationRejected as exc:
                logger.info("soak_candidate_rejected", extra={"reason": str(exc)})
                used.update(c.article_id for c in contexts)
                continue

            variant, plan = build_publication_plan(outcome.content, outcome.media, workspace)

            component_outcomes: list[ComponentOutcome] = []
            main_message_id: int | None = None
            for step in plan.steps:
                sent = run_step(telegram, step, target_channel, workspace.root, main_message_id)
                component_outcomes.append(sent)
                if sent.message_id is not None:
                    main_message_id = sent.message_id

        used_article_id = next(
            (c.article_id for c in contexts if c.source_url == outcome.content.source_url),
            contexts[0].article_id,
        )
        used.add(used_article_id)
        if test_history_path is not None:
            test_history.record(
                test_history_path,
                source_url=outcome.content.source_url,
                message_id=main_message_id,
            )
        source_family = sources_by_id.get(
            next(c.source_id for c in contexts if c.article_id == used_article_id)
        )
        recent.append(
            RecentPost(
                editorial_category=outcome.content.category,
                source_family=source_family.source_family if source_family else None,
            )
        )
        results.append(
            SoakPostResult(
                index=len(results) + 1,
                outcome=outcome,
                variant=variant,
                component_outcomes=tuple(component_outcomes),
                article_id=used_article_id,
                source_id=next(c.source_id for c in contexts if c.article_id == used_article_id),
            )
        )
        logger.info(
            "soak_post_sent",
            extra={"index": results[-1].index, "category": outcome.content.category.value},
        )

        if len(results) < count:
            time.sleep(delay_seconds)

    return results


__all__ = [
    "DEFAULT_DELAY_SECONDS",
    "MAX_CANDIDATES_PER_ATTEMPT",
    "SoakPostResult",
    "build_article_context",
    "eligible_articles_v2",
    "run_soak",
]
