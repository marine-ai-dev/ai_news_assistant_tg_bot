"""The unattended v2 pipeline — collect, select, classify/generate, publish. Scheduled.

This is what ``ai-news auto once`` (and therefore the scheduled workflow) now runs for
mode ``"live"`` — see ``cli/auto.py``'s ``_run_once``. ``"dry-run"`` and ``"test"``
still exercise v1 (``automation/pipeline.py``) unchanged; there is no v2 dry-run yet,
and v2's own test-channel path is ``ai-news auto soak-v2 --test``.

Unlike v1's NEWS-only automation, this selects from the full v2 ``EditorialCategory``
range (NEWS, RESEARCH, AI_TOOL, AI_LIFEHACK, FREE_DEAL, PROMPT_WORKFLOW, EXPLAINER —
real classification decides which, nothing here narrows it away) and sends through the
v2 renderer and four-layer media strategy (branded cards / open-license media / video),
never v1's plain-text ``render_post``. WEEKLY_DIGEST is the one category deliberately
excluded: it needs a full week of published posts as its own input, not a single
per-slot candidate pick — a separate, not-yet-scheduled concern.

Shares ``automation.pipeline.AUTOMATION_ACTOR`` ("gemini:auto") with v1, so both count
against the exact same ``AI_NEWS_DAILY_POST_LIMIT`` ledger via the same
``production_publications_today`` — the daily cap is a property of the production
channel, not of which pipeline version handled a given post.

Reuses ``automation.publish_v2_production.publish_one_v2_post_to_production`` — the
same tested send-then-record path the manual smoke test uses — parameterized
differently here: no NEWS/RESEARCH exclusion, a real ``recent_history()`` diversity
seed instead of an empty one, and ``AUTOMATION_ACTOR`` instead of the smoke test's
distinct, deliberately-uncounted actor.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from ai_news_editor.automation.gemini import GeminiClient, GeminiError
from ai_news_editor.automation.pipeline import (
    AUTOMATION_ACTOR,
    AutomationResult,
    Outcome,
    production_publications_today,
)
from ai_news_editor.automation.publish_v2_production import (
    NoEligibleCandidateError,
    publish_one_v2_post_to_production,
)
from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import EditorialCategory
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.pipeline.collect import collect as collect_sources
from ai_news_editor.pipeline.process import process as run_processing
from ai_news_editor.planning.recent_history import recent_history
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.settings import Settings
from ai_news_editor.sources.config import load_sources_config
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    DraftRepository,
    EvaluationRepository,
    PublicationRepository,
)

logger = get_logger(__name__)

#: Not eligible for the per-slot scheduled pick — see module docstring.
_EXCLUDED_CATEGORIES: frozenset[EditorialCategory] = frozenset({EditorialCategory.WEEKLY_DIGEST})

#: Soft ordering only — sources capable of an earlier-listed category are preferred
#: when narrowing the candidate pool, but real classification is never bound by it
#: (see ``publish_one_v2_post_to_production``'s own docstring). NEWS listed first
#: keeps this pipeline at least as NEWS-capable as v1 was.
_CATEGORY_PREFERENCE: tuple[EditorialCategory, ...] = (
    EditorialCategory.NEWS,
    EditorialCategory.AI_TOOL,
    EditorialCategory.AI_LIFEHACK,
    EditorialCategory.PROMPT_WORKFLOW,
    EditorialCategory.FREE_DEAL,
    EditorialCategory.EXPLAINER,
    EditorialCategory.RESEARCH,
    EditorialCategory.AI_AUTOMATION,
)


def run_pass_v2(
    canonical_connection: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> AutomationResult:
    """Collect, normalize, then run the scheduled v2 pipeline. Always live — always
    writes to ``canonical_connection`` directly, same as v1's live mode.

    Returns the same ``AutomationResult``/``Outcome`` contract v1 uses, so
    ``cli/auto.py``'s reporting, ``GITHUB_OUTPUT`` emission, and the workflow's
    "only persist state after a live PUBLISHED run" gate all keep working unchanged.
    """
    run_id = uuid.uuid4().hex[:12]
    moment = now or now_utc()

    # 1. Kill switch — same switch, same semantics as v1's live mode.
    if not settings.automation_enabled:
        return AutomationResult(
            Outcome.DISABLED,
            "AI_NEWS_AUTOMATION_ENABLED is not set to a truthy value; nothing was done.",
        )
    if settings.gemini_api_key is None:
        return AutomationResult(Outcome.CONFIG_ERROR, "AI_NEWS_GEMINI_API_KEY is not set.")
    if settings.telegram_bot_token is None:
        return AutomationResult(Outcome.CONFIG_ERROR, "AI_NEWS_TELEGRAM_BOT_TOKEN is not set.")
    if not settings.telegram_channel:
        return AutomationResult(Outcome.CONFIG_ERROR, "AI_NEWS_TELEGRAM_CHANNEL is not set.")

    # 2. Collection and normalization — identical infrastructure to v1's run_pass,
    # called directly here rather than through it since everything past this point
    # diverges.
    try:
        config = load_sources_config(settings.sources_config_path)
        with HttpClient() as collect_http:
            collect_sources(canonical_connection, collect_http, config, run_id=run_id)
        run_processing(canonical_connection)
    except AiNewsError as exc:
        return AutomationResult(Outcome.CONFIG_ERROR, f"could not collect sources: {exc}")

    # 3. Daily limit — shared ledger with v1 (same actor, same channel, same day
    # boundary). See production_publications_today's own docstring.
    published_today = production_publications_today(canonical_connection, settings, moment)
    if published_today >= settings.daily_post_limit:
        return AutomationResult(
            Outcome.DAILY_LIMIT_REACHED,
            f"{published_today} of {settings.daily_post_limit} automated posts already "
            "published today.",
        )

    registry = load_sources_config(settings.sources_config_path)
    recent = recent_history(
        publications=PublicationRepository(canonical_connection),
        drafts=DraftRepository(canonical_connection),
        articles=ArticleRepository(canonical_connection),
        evaluations=EvaluationRepository(canonical_connection),
        sources=registry,
        channel=settings.telegram_channel,
    )

    # 4-12. Select, classify/generate, validate, render, attach media, send, record —
    # all inside the same tested path the manual smoke test uses.
    try:
        client = GeminiClient(
            settings.gemini_api_key.get_secret_value(),
            model=settings.llm_model,
            read_timeout=settings.gemini_read_timeout_seconds,
        )
        with (
            HttpClient() as http,
            TelegramClient(settings.telegram_bot_token.get_secret_value()) as telegram,
        ):
            result = publish_one_v2_post_to_production(
                canonical_connection,
                client=client,
                registry=registry,
                http=http,
                telegram=telegram,
                target_channel=settings.telegram_channel,
                category_preference=_CATEGORY_PREFERENCE,
                excluded_categories=_EXCLUDED_CATEGORIES,
                recent=recent,
                actor=AUTOMATION_ACTOR,
            )
    except NoEligibleCandidateError as exc:
        return AutomationResult(Outcome.NO_CANDIDATE, str(exc))
    except GeminiError as exc:
        return AutomationResult(Outcome.GEMINI_ERROR, str(exc))
    except Exception as exc:  # genuine infrastructure failure, e.g. a Telegram send error
        logger.error(
            "v2 scheduled publication failed",
            extra={"channel": settings.telegram_channel, "mode": "live"},
        )
        return AutomationResult(
            Outcome.PUBLISH_ERROR, str(exc), channel=settings.telegram_channel
        )

    main_message_id = next(
        (c.message_id for c in result.component_outcomes if c.message_id is not None), None
    )
    return AutomationResult(
        Outcome.PUBLISHED,
        f"published to {settings.telegram_channel} "
        f"(category={result.outcome.content.category.value}).",
        draft_id=result.draft_id,
        message_id=main_message_id,
        channel=settings.telegram_channel,
    )


__all__ = ["run_pass_v2"]
