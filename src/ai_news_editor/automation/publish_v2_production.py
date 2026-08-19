"""Manual, one-off v2 production publish — explicitly not part of the schedule.

The scheduled/automated production path (``automation/pipeline.py``) is unchanged and
still NEWS-only v1. ``automation/soak.py`` proves the real v2 pipeline (source
eligibility -> geography allowlist -> forbidden-geography/dystopian content filter ->
diversity -> classification/generation -> branded card/media -> single-message plan ->
real Telegram send) end to end, but on purpose only against the TEST channel — it never
writes to the canonical database.

This module reuses that exact same tested pipeline for the *send itself*, pointed at
the real production channel, for exactly one manually-authorized post. What it adds is
bookkeeping: after a confirmed successful Telegram send (never before, never
speculatively), it writes a matching Draft/DraftVersion/Publication row set directly to
the canonical database, so production dedup (an article that already has any Draft is
never selected again — the same check ``eligible_articles_v2`` already applies) and the
review-decision audit trail see this post exists.

**Deliberately does not go through ``publishing.service.prepare_publication`` /
``publish_bundle``.** Those functions build the outgoing text by calling
``writing.format.render_version`` -> ``render_post``, which *recomputes* the message
from raw title/body/category at send time — a real security invariant for v1 content
(what a reviewer approved is exactly what gets sent, never anything pre-formatted), but
incompatible with v2's already-fully-rendered MarkdownV2 caption: forcing it through
would either double-escape the text or silently replace the approved v2 visual style
with v1's own, older formatting. So the Telegram call here goes directly through
``publishing.rich.run_step`` (the same execution primitive ``publish_bundle`` itself
uses internally), and the Draft/DraftVersion rows written afterward are historical
records of what was actually sent, not something anything re-renders.

The review-decision actor is deliberately distinct from both ``gemini:auto``
(automation.pipeline's scheduled actor, which the daily automated-post-limit query
specifically counts) and ``owner`` (a real human using the review UI) — a manual
smoke test is neither, and must not be counted as either.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from uuid import UUID

from ai_news_editor.automation.gemini import GeminiClient
from ai_news_editor.automation.pipeline_v2 import (
    ArticleContext,
    OrchestrationOutcome,
    OrchestrationRejected,
    collapse_to_primary_sources,
    run_pipeline_v2,
)
from ai_news_editor.automation.soak import build_article_context, eligible_articles_v2
from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    ContentType,
    DraftStatus,
    EditorialCategory,
    PublicationStatus,
    TrustTier,
)
from ai_news_editor.domain.models import Article, Publication
from ai_news_editor.editorial.diversity import RecentPost
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.publishing.gate import approve_draft
from ai_news_editor.publishing.rich import ComponentOutcome, run_step
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.rendering.plan import PlanVariant, build_publication_plan
from ai_news_editor.sources.capability import allows_category
from ai_news_editor.sources.config import SourcesConfig
from ai_news_editor.sources.geography import is_source_eligible
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.storage.repositories import (
    DraftRepository,
    PublicationRepository,
)

logger = get_logger(__name__)

#: Distinguishable from automation.pipeline.AUTOMATION_ACTOR ("gemini:auto", the
#: scheduled job) and from publishing.gate.DEFAULT_ACTOR ("owner", a real human review).
#: This exact string is what future dedup/audit queries can use to identify a manual
#: v2 smoke-test publication specifically.
MANUAL_V2_ACTOR = "claude:v2-manual-smoke-test"

#: Never select these — the smoke test is specifically for a category other than the
#: two the live v1 pipeline already covers.
_EXCLUDED_CATEGORIES = frozenset({EditorialCategory.NEWS, EditorialCategory.RESEARCH})

#: How many real candidates one call is willing to try before giving up. Bounded, same
#: discipline as automation.soak.MAX_CANDIDATES_PER_ATTEMPT / run_soak's max_attempts.
MAX_ATTEMPTS = 20
MAX_CANDIDATES_PER_ATTEMPT = 15


class NoEligibleCandidateError(Exception):
    """No real candidate produced a publishable non-NEWS/RESEARCH post."""


@dataclass(frozen=True, slots=True)
class PublishV2Result:
    """Everything the smoke-test report needs about the one real post sent."""

    outcome: OrchestrationOutcome
    variant: PlanVariant
    component_outcomes: tuple[ComponentOutcome, ...]
    article_id: UUID
    source_id: str
    draft_id: UUID
    publication_id: UUID


def publish_one_v2_post_to_production(
    connection: sqlite3.Connection,
    *,
    client: GeminiClient,
    registry: SourcesConfig,
    http: HttpClient,
    telegram: TelegramClient,
    target_channel: str,
    category_preference: tuple[EditorialCategory, ...],
) -> PublishV2Result:
    """Send exactly one real v2 post to ``target_channel`` and record it.

    ``category_preference`` narrows the candidate pool to sources capable of at least
    one of these categories (a soft preference — real classification still decides the
    final category), in the given order of preference. NEWS and RESEARCH are always
    hard-excluded regardless of what narrowing lets through, since real classification
    is not bound by the narrowing.

    Raises:
        NoEligibleCandidateError: no real candidate produced a publishable post outside
            NEWS/RESEARCH within the bounded attempt budget. Nothing was sent.
    """
    sources_by_id = {source.id: source for source in registry.sources}

    def trust_tier_of(article: Article) -> TrustTier:
        source = sources_by_id.get(article.source_id)
        return source.trust_tier if source else TrustTier.UNVERIFIED

    used: set[UUID] = set()
    recent: list[RecentPost] = []

    attempts = 0
    while attempts < MAX_ATTEMPTS:
        attempts += 1
        pool = eligible_articles_v2(connection, exclude_article_ids=frozenset(used))
        if not pool:
            break

        pool = collapse_to_primary_sources(pool, trust_tier_of)

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

        narrowed = [
            a
            for a in pool
            if a.source_id in sources_by_id
            and any(
                allows_category(sources_by_id[a.source_id], category)
                for category in category_preference
            )
        ]
        pool = narrowed or pool

        contexts: list[ArticleContext] = []
        for article in pool[:MAX_CANDIDATES_PER_ATTEMPT]:
            context = build_article_context(connection, article, registry, http=http)
            if context is not None:
                contexts.append(context)
        if not contexts:
            used.update(a.id for a in pool[:MAX_CANDIDATES_PER_ATTEMPT])
            continue

        with MediaWorkspace(label="v2-production-smoke") as workspace:
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
                logger.info("v2_production_candidate_rejected", extra={"reason": str(exc)})
                used.update(c.article_id for c in contexts)
                continue

            if outcome.content.category in _EXCLUDED_CATEGORIES:
                logger.info(
                    "v2_production_candidate_excluded_category",
                    extra={"category": outcome.content.category.value},
                )
                used_article_id = next(
                    (
                        c.article_id
                        for c in contexts
                        if c.source_url == outcome.content.source_url
                    ),
                    contexts[0].article_id,
                )
                used.add(used_article_id)
                continue

            variant, plan = build_publication_plan(outcome.content, outcome.media, workspace)
            if len(plan.steps) != 1:  # pragma: no cover - defensive, invariant elsewhere
                raise AssertionError(
                    f"expected exactly one publication step, got {len(plan.steps)}"
                )

            # The branded card (or any other media file) must survive past this
            # MediaWorkspace's cleanup, since the Telegram send happens via run_step
            # below, still inside this `with` block — matching automation.soak's own
            # sequencing (send happens before the workspace exits).
            component_outcomes: list[ComponentOutcome] = []
            main_message_id: int | None = None
            for step in plan.steps:
                sent = run_step(telegram, step, target_channel, workspace.root, main_message_id)
                component_outcomes.append(sent)
                if sent.message_id is not None:
                    main_message_id = sent.message_id

        if main_message_id is None:  # pragma: no cover - defensive
            raise AssertionError("Telegram send reported no message id; refusing to record it")

        used_article_id = next(
            (c.article_id for c in contexts if c.source_url == outcome.content.source_url),
            contexts[0].article_id,
        )
        used.add(used_article_id)
        source_id = next(c.source_id for c in contexts if c.article_id == used_article_id)

        draft_id, publication_id = _record_production_send(
            connection,
            article_id=used_article_id,
            channel=target_channel,
            message_id=main_message_id,
            headline=outcome.content.headline,
            source_url=outcome.content.source_url,
        )

        logger.info(
            "v2_production_post_sent",
            extra={
                "category": outcome.content.category.value,
                "article_id": str(used_article_id),
                "message_id": main_message_id,
            },
        )
        return PublishV2Result(
            outcome=outcome,
            variant=variant,
            component_outcomes=tuple(component_outcomes),
            article_id=used_article_id,
            source_id=source_id,
            draft_id=draft_id,
            publication_id=publication_id,
        )

    raise NoEligibleCandidateError(
        "no real candidate produced a publishable non-NEWS/RESEARCH post within "
        f"{MAX_ATTEMPTS} attempts; nothing was sent"
    )


def _record_production_send(
    connection: sqlite3.Connection,
    *,
    article_id: UUID,
    channel: str,
    message_id: int,
    headline: str,
    source_url: str,
) -> tuple[UUID, UUID]:
    """Write the audit trail for an already-successful send. Never re-renders,
    never re-sends — this is bookkeeping for dedup and the review-decision log only.
    """
    drafts = DraftRepository(connection)
    draft, version = drafts.create(
        article_id=article_id,
        content_type=ContentType.NEWS,  # provenance: sourced from a real Article
        title=headline,
        body=f"[v2] {headline}",
        category=Category.EVERYDAY_AI,
        audience=AudienceTier.GENERAL,
        source_attribution=f"🔗 Джерело: v2\n{source_url}",
        source_url=source_url,
        created_by=MANUAL_V2_ACTOR,
    )
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    authorization = approve_draft(
        connection, draft.id, actor=MANUAL_V2_ACTOR, expected_version_id=version.id
    )
    # APPROVED -> PUBLISHING -> PUBLISHED: the two-step transition the state machine
    # requires (see domain/transitions.py) — this send already succeeded, so both
    # steps happen back to back rather than bracketing a real Telegram call.
    drafts.set_status(draft.id, DraftStatus.PUBLISHING)
    drafts.set_status(draft.id, DraftStatus.PUBLISHED)

    publication = PublicationRepository(connection).add(
        Publication(
            draft_id=draft.id,
            draft_version_id=version.id,
            review_decision_id=authorization.decision_id,
            content_hash=version.content_hash,
            channel=channel,
            status=PublicationStatus.SUCCEEDED,
            message_id=message_id,
            published_at=now_utc(),
        )
    )
    return draft.id, publication.id


__all__ = [
    "MANUAL_V2_ACTOR",
    "MAX_ATTEMPTS",
    "NoEligibleCandidateError",
    "PublishV2Result",
    "publish_one_v2_post_to_production",
]
