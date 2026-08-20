"""One unattended run, start to finish: the order in section 15 exists to be read here.

``run_pass`` is the whole story, collection included: it decides which database this
run actually writes to (:func:`isolated_connection`), then collects, normalizes and
hands off to ``run_automation`` for selection onward. Every step before "approve" only
*reads*; nothing is written to storage until a Gemini-written post has passed the same
:class:`~writing.schema.DraftResult` validation a human-written one passes, and nothing
is approved until that Draft exists through the same
:func:`~writing.import_results.import_drafts` a human-imported batch goes through.
Publication uses :func:`~publishing.service.prepare_publication` and
:func:`~publishing.service.publish_bundle` exactly as configured — this module contains
no Telegram call of its own, and could not add one without duplicating machinery that
already handles exactly-once delivery, partial-bundle recovery and uncertain outcomes
correctly.

Every early-exit path returns a normal :class:`AutomationResult`, not an exception. A
scheduled run that finds nothing to publish is success: nothing was wrong, there was
simply nothing to do. Only a genuine infrastructure failure — Gemini unreachable after
retries, a missing key, a database that will not open — is allowed to look like failure
to whatever is watching the exit code.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from ai_news_editor.automation import test_history
from ai_news_editor.automation.gemini import GeminiClient, GeminiError
from ai_news_editor.automation.provider import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    GenerationRejected,
    SelectionInvalid,
    SelectionRejected,
    generate_post,
    select_candidate,
)
from ai_news_editor.automation.schema import MAX_SELECTION_CANDIDATES, SelectionCandidate
from ai_news_editor.automation.test_history import DEFAULT_FILENAME as TEST_HISTORY_FILENAME
from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import (
    ArticleStatus,
    AudienceTier,
    Category,
    EditorialDecision,
    EvaluatorType,
    PostFormat,
    TrustTier,
    VerificationStatus,
)
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.domain.models import Article, Evaluation
from ai_news_editor.editorial.export import build_excerpt, fingerprint_for
from ai_news_editor.editorial.rubric import (
    RUBRIC_VERSION,
    composite_score,
    passes_credibility_gate,
)
from ai_news_editor.editorial.rubric import (
    SCHEMA_VERSION as EDITORIAL_SCHEMA_VERSION,
)
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.pipeline.collect import collect as collect_sources
from ai_news_editor.pipeline.process import process as run_processing
from ai_news_editor.publishing.gate import approve_draft
from ai_news_editor.publishing.service import prepare_publication, publish_bundle
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.scheduling.clock import CHANNEL_TIMEZONE, to_local
from ai_news_editor.settings import Settings
from ai_news_editor.sources.config import load_sources_config
from ai_news_editor.sources.fulltext import fetch_fulltext
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    DraftRepository,
    EvaluationRepository,
    PublicationRepository,
    ReviewDecisionRepository,
    SourceRepository,
)
from ai_news_editor.writing.export import eligibility_problem
from ai_news_editor.writing.format import has_any_markup
from ai_news_editor.writing.import_results import DraftImportError, import_drafts
from ai_news_editor.writing.schema import (
    STYLE_VERSION,
    WRITING_SCHEMA_VERSION,
    DraftBatch,
    DraftResult,
)

logger = get_logger(__name__)

#: Who a Gemini-produced approval is recorded as. Distinct from every human actor string
#: this project has ever used, on purpose — see docs/safety.md.
AUTOMATION_ACTOR = "gemini:auto"

Mode = Literal["dry-run", "test", "live"]

#: How many candidates are looked at before giving up and calling it a quiet run. Small
#: and deterministic, matching the selection prompt's own bound.
CANDIDATE_LIMIT = MAX_SELECTION_CANDIDATES

#: A fulltext failure at one of these statuses means the *fetcher* is unwelcome on that
#: domain right now, not that this one page is bad — a 403/401 on one OpenAI article is
#: not going to look different on the next one. 429 is included too, but only ever seen
#: here after HttpClient's own transient-retry budget (sources.http.DEFAULT_MAX_ATTEMPTS)
#: is already exhausted, which is what turns "a blip" into "sustained throttling" — not
#: to be confused with a 429 from the Gemini API, which is a wholly separate client and
#: stays a global GEMINI_ERROR exactly as before (see _run_pipeline's own except clause).
_DOMAIN_UNAVAILABLE_STATUS = frozenset({401, 403, 429})


def _domain_of(url: str) -> str:
    """The candidate's hostname, normalized just enough that a ``www.`` subdomain and
    its bare domain cool down together — nothing broader than that one prefix."""
    return (urlsplit(url).hostname or "").removeprefix("www.").lower()

#: Automation's own fixed classification for the Evaluation it creates. Not something
#: Gemini is asked for (see automation.schema's module docstring for why the schema
#: stays this narrow) and not something this pipeline tries to infer per-story: every
#: eligible candidate already comes from a configured OFFICIAL vendor source, which is
#: structurally a product-news category, and the generation prompt already instructs
#: Gemini to write for a reader who may never have opened an AI chat tool — NEWCOMER
#: matches that instruction rather than contradicting it. A known simplification,
#: documented rather than hidden; see the Phase report for the alternative considered.
AUTOMATION_CATEGORY = Category.PRODUCT_UPDATE
AUTOMATION_AUDIENCE = AudienceTier.NEWCOMER

#: Fixed rubric inputs for an automated evaluation, run through the *same* weights and
#: gate every human/Claude evaluation uses (editorial.rubric). Not a second scoring
#: system: the values are constants chosen to represent "passed every automated safety
#: and grounding check this pipeline enforces", not a nuanced per-story judgement — this
#: pipeline has no basis for judging novelty or wow-factor, and does not pretend to.
_AUTOMATION_SCORES: dict[str, int] = {
    "credibility": 85,  # official vendor source, by construction of this pipeline
    "general_ai_relevance": 85,  # every configured source is an AI vendor/platform
    "reader_interest": 55,
    "usefulness": 55,
    "novelty": 50,
    "wow_factor": 40,
    "virality_potential": 40,
    "accessibility": 60,
    "consumer_impact": 55,
}


class Outcome(StrEnum):
    """Why a run ended the way it did. Exactly one per run."""

    PUBLISHED = "PUBLISHED"
    DRY_RUN_COMPLETE = "DRY_RUN_COMPLETE"
    DISABLED = "DISABLED"
    DAILY_LIMIT_REACHED = "DAILY_LIMIT_REACHED"
    NO_CANDIDATE = "NO_CANDIDATE"
    SELECTION_REJECTED = "SELECTION_REJECTED"
    FULLTEXT_UNAVAILABLE = "FULLTEXT_UNAVAILABLE"
    GENERATION_REJECTED = "GENERATION_REJECTED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    #: Every candidate attempt (bounded by settings.max_candidate_attempts) ran into a
    #: candidate-specific rejection — distinct from NO_CANDIDATE (nothing was even
    #: eligible to try) and from each of the three per-candidate outcomes above (which
    #: now only end a run on their own when the very first attempt already exhausts
    #: every remaining candidate — see _run_pipeline).
    CANDIDATES_EXHAUSTED = "CANDIDATES_EXHAUSTED"
    CONFIG_ERROR = "CONFIG_ERROR"
    GEMINI_ERROR = "GEMINI_ERROR"
    PUBLISH_ERROR = "PUBLISH_ERROR"


#: Outcomes that mean "nothing was wrong, there was simply nothing to publish" — a
#: scheduled run ending here is success, and the CLI exits 0.
QUIET_OUTCOMES = frozenset(
    {
        Outcome.DRY_RUN_COMPLETE,
        Outcome.DISABLED,
        Outcome.DAILY_LIMIT_REACHED,
        Outcome.NO_CANDIDATE,
        Outcome.SELECTION_REJECTED,
        Outcome.FULLTEXT_UNAVAILABLE,
        Outcome.GENERATION_REJECTED,
        Outcome.VALIDATION_FAILED,
        Outcome.CANDIDATES_EXHAUSTED,
    }
)


@dataclass(frozen=True, slots=True)
class AutomationResult:
    """What one run decided, and why. Never raised as an exception for a normal no-op."""

    outcome: Outcome
    detail: str
    candidates_considered: int = 0
    draft_id: UUID | None = None
    message_id: int | None = None
    channel: str | None = None

    @property
    def published(self) -> bool:
        return self.outcome is Outcome.PUBLISHED

    @property
    def is_quiet(self) -> bool:
        """Whether this is a normal no-op rather than something worth a nonzero exit."""
        return self.outcome in QUIET_OUTCOMES


@contextmanager
def isolated_connection(
    canonical_connection: sqlite3.Connection, *, mode: Mode
) -> Iterator[sqlite3.Connection]:
    """The one place this project decides which database a run actually writes to.

    ``"live"`` yields the canonical connection it was given, unchanged — every write
    from collection through publication lands in the real, on-disk database, same as
    always. ``"dry-run"`` and ``"test"`` both yield a fresh, in-memory copy instead
    (SQLite's own backup API, page-for-page): every read sees genuine history — real
    collected articles, real dedup fingerprints, real prior publications — so a fresh
    GitHub Actions runner with an empty local checkout still evaluates candidates
    against the *canonical* database's history, not a blank slate; every write lands
    only in that copy and is gone the moment this context manager exits. The canonical
    connection passed in is never written to and never closed here.

    This has to wrap collection and normalization, not just the automation pipeline
    that follows them — a caller that calls this once, up front, and then threads the
    yielded connection through ``collect()``, ``process()`` and
    ``run_automation()`` in turn (see :func:`run_pass`) is what makes a dry run see
    real, freshly-collected candidates instead of an empty database, and what stops a
    ``--test`` run's collection step from writing new articles into the canonical
    database the way it used to.
    """
    if mode == "live":
        yield canonical_connection
        return

    ephemeral = sqlite3.connect(":memory:", isolation_level=None)
    ephemeral.row_factory = sqlite3.Row
    ephemeral.execute("PRAGMA foreign_keys = ON")
    canonical_connection.backup(ephemeral)
    try:
        yield ephemeral
    finally:
        ephemeral.close()


def run_pass(
    canonical_connection: sqlite3.Connection,
    settings: Settings,
    *,
    mode: Mode,
    now: datetime | None = None,
    run_id: str | None = None,
) -> AutomationResult:
    """Collect, normalize, then run the automation pipeline — as one unit, against
    whichever database :func:`isolated_connection` selects for ``mode``.

    This is the entire unattended pass, and the only place collection is wired
    together with selection/generation/publication for this pipeline — ``ai-news auto
    once`` (cli/auto.py) is a thin wrapper around this one function; nothing else
    duplicates this sequence. Collection and normalization always run for real (never
    ``collect()``'s own ``dry_run=True`` no-write mode) — what makes ``"dry-run"``
    dry is *which* database those real writes land in, not whether they happen.
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    with isolated_connection(canonical_connection, mode=mode) as connection:
        try:
            config = load_sources_config(settings.sources_config_path)
            with HttpClient() as http:
                collect_sources(connection, http, config, run_id=run_id)
            run_processing(connection)
        except AiNewsError as exc:
            return AutomationResult(Outcome.CONFIG_ERROR, f"could not collect sources: {exc}")

        return run_automation(connection, settings, mode=mode, now=now, run_id=run_id)


def run_automation(
    connection: sqlite3.Connection,
    settings: Settings,
    *,
    mode: Mode,
    now: datetime | None = None,
    run_id: str | None = None,
) -> AutomationResult:
    """Select, generate, validate and (unless ``"dry-run"``) publish at most one post.

    ``connection`` is used exactly as given — this function has no opinion about
    whether it is the canonical database or an isolated copy; that decision was
    already made by whoever called it (:func:`run_pass`, via
    :func:`isolated_connection`). Called directly like this, it is most useful for
    testing selection, generation and validation against a seeded database without
    also mocking collection.

    ``mode`` controls two things here:

    * whether Telegram is contacted at all (never, for ``"dry-run"``), and which
      channel receives the send when it is (``settings.test_channel`` for ``"test"``,
      ``settings.telegram_channel`` for ``"live"``).
    * whether ``AI_NEWS_AUTOMATION_ENABLED`` is even consulted — it gates ``"live"``
      only (see step 1 below). ``"dry-run"`` and ``"test"`` both have to be runnable
      by hand, from a ``workflow_dispatch``, while the switch stays off — that is the
      steady state this project expects once a scheduled job exists, and nobody should
      have to temporarily arm the setting that makes the *cron* start publishing just
      to prove a prompt still works.

    Selection, generation and validation run identically in all three modes — a dry
    run proves the same prompts and the same checks a live run would use, not an
    approximation of them.
    """
    moment = now or now_utc()
    run_id = run_id or uuid.uuid4().hex[:12]

    # 1. Kill switch. Live only — see the mode docstring above for why dry-run and
    # test do not check this at all. A scheduled run is always "live" (cli/auto.py and
    # the GitHub Actions workflow both hard-code that), so this is also what actually
    # gates the cron.
    if mode == "live" and not settings.automation_enabled:
        return AutomationResult(
            Outcome.DISABLED,
            "AI_NEWS_AUTOMATION_ENABLED is not set to a truthy value; nothing was done.",
        )

    # 2. Configuration, fail-closed and specific about what is missing.
    if settings.gemini_api_key is None:
        return AutomationResult(Outcome.CONFIG_ERROR, "AI_NEWS_GEMINI_API_KEY is not set.")
    if settings.telegram_bot_token is None:
        return AutomationResult(Outcome.CONFIG_ERROR, "AI_NEWS_TELEGRAM_BOT_TOKEN is not set.")
    target_channel = settings.test_channel if mode == "test" else settings.telegram_channel
    if mode != "dry-run" and not target_channel:
        missing = "AI_NEWS_TEST_CHANNEL" if mode == "test" else "AI_NEWS_TELEGRAM_CHANNEL"
        return AutomationResult(Outcome.CONFIG_ERROR, f"{missing} is not set.")

    return _run_pipeline(
        connection, settings, mode=mode, moment=moment, run_id=run_id,
        target_channel=target_channel,
    )


def _run_pipeline(
    connection: sqlite3.Connection,
    settings: Settings,
    *,
    mode: Mode,
    moment: datetime,
    run_id: str,
    target_channel: str | None,
) -> AutomationResult:
    """Steps 3 onward — everything that happens once the mode's database is settled.

    Split out of :func:`run_automation` only so that function's own job — deciding the
    kill switch, configuration, and which database this run actually writes to — stays
    readable as one block, before any candidate is even looked at. ``connection`` here
    may be the caller's real database or ``run_automation``'s in-memory test copy;
    nothing below this line can tell the difference, which is exactly the point.
    """
    # 3. Daily limit — scoped to the production channel and to live mode only. Test
    # mode has no real Publication rows to find here regardless (see the isolation
    # branch above — everything it writes lives in a copy that gets discarded), and a
    # deliberate manual test should never be blocked by automated activity earlier in
    # the day either.
    if mode == "live":
        published_today = production_publications_today(connection, settings, moment)
        if published_today >= settings.daily_post_limit:
            return AutomationResult(
                Outcome.DAILY_LIMIT_REACHED,
                f"{published_today} of {settings.daily_post_limit} automated posts already "
                f"published today ({to_local(moment, CHANNEL_TIMEZONE):%d %b, Europe/Kyiv}).",
            )

    # 4. Collection and normalization already happened — see run_once in cli.auto,
    # which calls collect() and process() before this function, exactly as a human
    # would run 'ai-news collect && ai-news process' before reviewing. Keeping that
    # network I/O out of this function is deliberate: everything from here on is a
    # pure function of what is already in the database, which is what makes it
    # possible to test selection, generation, validation and approval against a
    # seeded database with no HTTP mocking for feed collection at all.

    # 5. Build the eligible candidate list: NORMALIZED, from an OFFICIAL source, not
    # already drafted, not already evaluated by anyone (human or automated). Test mode
    # additionally excludes anything the dedicated test-history file already recorded
    # as sent — see automation.test_history's module docstring for why the canonical
    # database (which this exclusion never touches) cannot do that job on its own.
    test_history_path = settings.data_dir / TEST_HISTORY_FILENAME
    exclude_urls: frozenset[str] = frozenset()
    if mode == "test":
        exclude_urls = frozenset(
            entry.source_url for entry in test_history.load(test_history_path)
        )
    candidates, by_id = _eligible_candidates(connection, exclude_urls=exclude_urls)
    if not candidates:
        return AutomationResult(
            Outcome.NO_CANDIDATE, "no eligible NEWS candidate from an official source."
        )

    # 6-9. Select, fetch, generate, validate — bounded across up to
    # settings.max_candidate_attempts distinct candidates, one Gemini client reused for
    # all of them. A candidate-specific rejection (bad fulltext, an incomplete or
    # rejected generation, a validation failure) moves on to the next remaining
    # candidate rather than ending the run; a genuine infrastructure failure
    # (GeminiError — a bad key, an exhausted transient-retry budget, a permanent
    # rejection of the request itself) still aborts the whole run immediately, exactly
    # as before — that failure is not specific to whichever candidate was being tried
    # when it happened, and trying two more candidates would not change the outcome,
    # only spend two more Gemini calls finding that out. See run_candidates_loop below
    # for exactly which exceptions land in which bucket.
    try:
        with GeminiClient(
            settings.gemini_api_key.get_secret_value(), model=settings.llm_model,
            read_timeout=settings.gemini_read_timeout_seconds,
        ) as client:
            outcome = _attempt_candidates(
                client, connection, candidates, by_id,
                max_attempts=settings.max_candidate_attempts, run_id=run_id,
            )
    except GeminiError as exc:
        return AutomationResult(
            Outcome.GEMINI_ERROR, str(exc), candidates_considered=len(candidates)
        )

    if isinstance(outcome, AutomationResult):
        # SELECTION_REJECTED (the whole offered list, not one candidate) or
        # CANDIDATES_EXHAUSTED — nothing more to try.
        return outcome

    article, validated = outcome

    if mode == "dry-run":
        # Nothing was written: no Evaluation row, no Draft row, no approval, no send.
        # A validated-but-never-persisted DraftResult is the proof this mode exists to
        # produce — everything up to and including the canonical DraftResult validator
        # ran, and stops here.
        return AutomationResult(
            Outcome.DRY_RUN_COMPLETE,
            f"validated a post for {article.canonical_url}; nothing was written, "
            "approved or sent.",
            candidates_considered=len(candidates),
        )

    # 10. Storage. The only place either the Evaluation or the Draft is written.
    try:
        draft_id = _persist(connection, validated)
    except (DraftImportError, AiNewsError) as exc:
        return AutomationResult(
            Outcome.VALIDATION_FAILED, str(exc), candidates_considered=len(candidates)
        )

    # 11. Approve, through the one function that can mint an authorization.
    drafts = DraftRepository(connection)
    version = drafts.current_version(draft_id)
    try:
        approve_draft(
            connection, draft_id, actor=AUTOMATION_ACTOR, expected_version_id=version.id
        )
    except AiNewsError as exc:  # pragma: no cover - defensive; import_drafts already left
        # the draft in PENDING_REVIEW with this exact version current
        return AutomationResult(
            Outcome.VALIDATION_FAILED, f"approval failed: {exc}", draft_id=draft_id
        )

    # 12. Publish, through the same production path every other publisher in this
    # project uses. No Telegram call exists in this module.
    assert target_channel is not None  # checked in step 2
    assert settings.telegram_bot_token is not None  # checked in step 2
    try:
        with TelegramClient(settings.telegram_bot_token.get_secret_value()) as client:
            plan = prepare_publication(
                connection, draft_id, channel=target_channel,
                media_root=settings.resolved_media_dir,
            )
            publication = publish_bundle(
                connection, plan, client, media_root=settings.resolved_media_dir
            )
    except Exception as exc:
        logger.error(
            "automated publication failed",
            extra={"draft_id": str(draft_id), "channel": target_channel, "mode": mode},
        )
        return AutomationResult(
            Outcome.PUBLISH_ERROR, str(exc), draft_id=draft_id, channel=target_channel
        )

    logger.info(
        "automated publication succeeded",
        extra={
            "draft_id": str(draft_id),
            "channel": target_channel,
            "message_id": publication.message_id,
            "mode": mode,
        },
    )
    if mode == "test":
        # Recorded only now, after a real send succeeded — never speculatively, and
        # never into the canonical database (see the module docstring in
        # automation.test_history for why this file exists at all).
        test_history.record(
            test_history_path, source_url=article.canonical_url,
            message_id=publication.message_id,
        )
    return AutomationResult(
        Outcome.PUBLISHED,
        f"published to {target_channel}.",
        candidates_considered=len(candidates),
        draft_id=draft_id,
        message_id=publication.message_id,
        channel=target_channel,
    )


def _attempt_candidates(
    client: GeminiClient,
    connection: sqlite3.Connection,
    candidates: list[SelectionCandidate],
    by_id: dict[str, Article],
    *,
    max_attempts: int,
    run_id: str,
) -> AutomationResult | tuple[Article, _Validated]:
    """Try up to ``max_attempts`` distinct candidates, selected one at a time from
    what remains, until one produces a validated post or the budget (or the candidate
    pool itself) runs out.

    Returns the found ``(article, _Validated)`` pair on success, or the terminal
    :class:`AutomationResult` once nothing panned out:

    * ``SELECTION_REJECTED`` if Gemini declined the *offered list itself* — not
      specific to one candidate, so narrowing the list on a later attempt would not
      plausibly change that answer, and this ends the run immediately rather than
      spending the rest of the attempt budget re-asking a version of the same question.
    * ``CANDIDATES_EXHAUSTED`` once every attempt used up a genuine
      candidate-specific rejection instead (bad fulltext, an incomplete or rejected
      generation, a failed local validation) and there is nothing left to try.

    Deliberately catches only the narrow, already-established exceptions that mean
    "this one candidate did not work out" — :exc:`GenerationRejected` and the
    ``DraftImportError`` / ``AiNewsError`` pair from validation, plus a plain
    ``fulltext.ok`` check. Everything else — a :exc:`GeminiError` from an invalid key,
    an exhausted transient-retry budget, or a permanent request rejection — is left to
    propagate to the caller, whose own ``except GeminiError`` turns it into the loud
    ``GEMINI_ERROR`` outcome. That is what stops a global infrastructure failure from
    being silently retried against two more candidates instead of surfacing at all.

    A fulltext failure that looks like the *fetcher* being unwelcome on a domain
    (401/403, or a 429 that survived HttpClient's own transient retries) cools that
    hostname down for the rest of this call only — see ``blocked_domains`` below and
    ``_DOMAIN_UNAVAILABLE_STATUS``. Three OpenAI articles in a row used to be able to
    exhaust the whole attempt budget on the same blocked domain; now the second attempt
    already excludes every remaining OpenAI candidate instead of rediscovering the same
    403 twice. A 404, a too-short article, a duplicate, or a rejected generation stays
    exactly what it was: one candidate rejected, its domain untouched.
    """
    remaining = list(candidates)
    #: Hostnames the fetcher could not reach this run (401/403, or sustained 429 — see
    #: _DOMAIN_UNAVAILABLE_STATUS). Local to this one call: never written anywhere, and
    #: gone the moment this function returns, so the next run — scheduled or manual —
    #: gets a clean slate and may try the same domain again.
    blocked_domains: set[str] = set()
    attempted: list[str] = []

    def _reject(
        attempt: int, stage: str, candidate: SelectionCandidate, reason: str,
        *, domain: str | None = None, http_status: int | None = None,
    ) -> None:
        attempted.append(f"{candidate.source_name} ({candidate.url}): {stage} — {reason}")
        extra: dict[str, object] = {
            "attempt": attempt, "stage": stage,
            "source": candidate.source_name, "reason": reason,
        }
        if domain is not None:
            extra["domain"] = domain
        if http_status is not None:
            extra["http_status"] = http_status
        logger.info("candidate rejected", extra=extra)

    for attempt in range(1, max_attempts + 1):
        # Domains that failed with a fetcher-unavailable status on an earlier attempt
        # this run are never offered again — see the fulltext branch below for where
        # blocked_domains is populated.
        remaining = [c for c in remaining if _domain_of(c.url) not in blocked_domains]
        if not remaining:
            break

        try:
            selection = select_candidate(client, remaining)
        except SelectionRejected as exc:
            return AutomationResult(
                Outcome.SELECTION_REJECTED, exc.reason, candidates_considered=len(candidates)
            )
        except SelectionInvalid as exc:
            return AutomationResult(
                Outcome.SELECTION_REJECTED, str(exc), candidates_considered=len(candidates)
            )

        candidate = selection.candidate
        # Removed the instant it is selected, win or lose — a candidate a later
        # attempt might otherwise re-offer to Gemini after this one failed downstream.
        remaining = [c for c in remaining if c.id != candidate.id]
        article = by_id[candidate.id]

        fulltext = fetch_fulltext(candidate.url)
        if not fulltext.ok:
            domain = _domain_of(candidate.url)
            _reject(
                attempt, "fulltext", candidate, fulltext.reason or "unavailable",
                domain=domain, http_status=fulltext.status_code,
            )
            if fulltext.status_code in _DOMAIN_UNAVAILABLE_STATUS and domain not in blocked_domains:
                blocked_domains.add(domain)
                logger.info(
                    "domain unavailable for run",
                    extra={"domain": domain, "http_status": fulltext.status_code},
                )
            continue

        try:
            post = generate_post(
                client, candidate=candidate, article_text=fulltext.text or "",
                confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
            )
        except GenerationRejected as exc:
            _reject(attempt, "generation", candidate, exc.reason)
            continue

        try:
            validated = _validate_post(connection, article, post, run_id=run_id)
        except (DraftImportError, AiNewsError) as exc:
            _reject(attempt, "validation", candidate, str(exc))
            continue

        logger.info(
            "candidate accepted",
            extra={"attempt": attempt, "source": candidate.source_name, "url": candidate.url},
        )
        return article, validated

    return AutomationResult(
        Outcome.CANDIDATES_EXHAUSTED,
        f"tried {len(attempted)} candidate(s), none produced a publishable post. "
        + " | ".join(attempted),
        candidates_considered=len(candidates),
    )


def _eligible_candidates(
    connection, *, exclude_urls: frozenset[str] = frozenset()
) -> tuple[list[SelectionCandidate], dict[str, Article]]:
    """NORMALIZED articles from an OFFICIAL source, not already drafted or evaluated.

    Bounded to CANDIDATE_LIMIT and ordered newest-first, matching the selection
    prompt's own instruction to favour recent stories.

    ``exclude_urls`` is how test-only dedup (see ``automation.test_history``) keeps a
    story already sent to the test channel from being offered to Gemini again on a
    later ``--test`` run. Always empty for ``"dry-run"`` and ``"live"`` — production
    eligibility must never depend on what a test run happened to send.
    """
    articles_repo = ArticleRepository(connection)
    sources_repo = SourceRepository(connection)
    drafts_repo = DraftRepository(connection)
    evaluations_repo = EvaluationRepository(connection)

    sources = {source.id: source for source in sources_repo.list_all()}
    normalized = articles_repo.list_by_status(ArticleStatus.NORMALIZED, limit=200)
    normalized.sort(key=lambda a: a.published_at or a.created_at, reverse=True)

    candidates: list[SelectionCandidate] = []
    by_id: dict[str, Article] = {}
    for article in normalized:
        if len(candidates) >= CANDIDATE_LIMIT:
            break
        source = sources.get(article.source_id)
        if source is None or source.trust_tier is not TrustTier.OFFICIAL:
            continue
        if drafts_repo.find_by_article(article.id) is not None:
            continue
        if evaluations_repo.latest_for_article(article.id) is not None:
            continue
        if article.canonical_url in exclude_urls:
            continue

        candidate_id = str(len(candidates) + 1)
        excerpt, _truncated = build_excerpt(article.clean_text)
        candidates.append(
            SelectionCandidate(
                id=candidate_id,
                source_name=source.publisher or source.name,
                title=article.title,
                published_at=article.published_at.isoformat() if article.published_at else None,
                url=article.canonical_url,
                summary=excerpt,
            )
        )
        by_id[candidate_id] = article

    return candidates, by_id


@dataclass(frozen=True, slots=True)
class _Validated:
    """A post that has passed every canonical check, not yet written anywhere.

    Building this touches no storage at all — every check inside it is a pure function
    over objects already in memory (Pydantic's own validators, and
    ``eligibility_problem``, which takes an ``Evaluation`` instance rather than reading
    one back from the database). That is what makes a true dry run possible: the exact
    validation a live run would perform runs to completion, and nothing is persisted
    either way unless the caller goes on to call :func:`_persist`.
    """

    evaluation: Evaluation
    draft_result: DraftResult
    batch: DraftBatch


def _validate_post(connection, article: Article, post, *, run_id: str) -> _Validated:
    """Build and validate the Evaluation and DraftResult this post would become.

    Raises:
        DraftImportError: the constructed DraftResult failed canonical validation, or
            the article changed underneath the fingerprint since it was selected, or
            it already has an evaluation or a draft — checked here too, not only by
            the eligibility filter that built the candidate list, because time may
            have passed since that list was built.
    """
    excerpt, _truncated = build_excerpt(article.clean_text)
    fingerprint = fingerprint_for(article, excerpt)

    source_repo = SourceRepository(connection)
    source = source_repo.get(article.source_id)
    label = source.publisher or source.name

    evaluation = Evaluation(
        article_id=article.id,
        schema_version=EDITORIAL_SCHEMA_VERSION,
        rubric_version=RUBRIC_VERSION,
        evaluator_type=EvaluatorType.AUTOMATED,
        evaluator=AUTOMATION_ACTOR,
        batch_id=run_id,
        content_fingerprint=fingerprint,
        decision=EditorialDecision.SHORTLIST,
        category=AUTOMATION_CATEGORY,
        audience=AUTOMATION_AUDIENCE,
        scores=dict(_AUTOMATION_SCORES),
        composite_score=composite_score(_AUTOMATION_SCORES),
        verification_status=VerificationStatus.NOT_REQUIRED,
        why_selected=(f"automated selection, confidence {post.confidence}",),
        notes=None,
    )
    if not passes_credibility_gate(_AUTOMATION_SCORES):  # pragma: no cover - fixed constants
        raise DraftImportError(["automation's own fixed scores failed the credibility gate"])

    # The same eligibility rule a human import obeys, checked here against the
    # in-memory evaluation this call is about to propose — not one read back from
    # storage, because nothing has been written yet.
    drafts_repo = DraftRepository(connection)
    problem = eligibility_problem(
        article, evaluation, has_draft=drafts_repo.find_by_article(article.id) is not None
    )
    if problem:
        raise DraftImportError([f"article {article.id}: {problem}"])

    # Gemini must never control Telegram markup — bold, emoji and the source hyperlink
    # are entirely this renderer's decision (writing.format.render_post), never the
    # model's. DraftResult's own validator (disallowed_tags) only rejects tags outside
    # the human-writer-permitted subset; this is the stricter, automation-only check
    # that rejects *any* tag at all, checked before that subset even applies.
    for field_name, text in (("headline", post.headline or ""), ("body", post.body or "")):
        if has_any_markup(text):
            raise DraftImportError(
                [f"generated {field_name} contains markup, which automation must not produce"]
            )

    # DraftResult's own validator runs on construction: URL safety, blank text,
    # disallowed markup, the Telegram length limit. A ValidationError here is exactly
    # as final as any of the DraftImportError cases above.
    try:
        draft_result = DraftResult(
            article_id=article.id,
            evaluation_id=evaluation.id,
            article_fingerprint=fingerprint,
            post_format=PostFormat.STANDARD,
            headline=post.headline or "",
            body=post.body or "",
            source_label=label,
            source_url=article.canonical_url,
            writer_notes=(
                *tuple(post.factual_claims)[:7], f"gemini confidence: {post.confidence}"
            ),
        )
    except ValueError as exc:
        raise DraftImportError([f"article {article.id}: {exc}"]) from exc

    batch = DraftBatch(
        schema_version=WRITING_SCHEMA_VERSION,
        style_version=STYLE_VERSION,
        batch_id=run_id,
        writer=AUTOMATION_ACTOR,
        drafts=[draft_result],
    )
    return _Validated(evaluation=evaluation, draft_result=draft_result, batch=batch)


def _persist(connection, validated: _Validated) -> UUID:
    """Write the Evaluation and the Draft it authorises. The only place either is saved.

    Two calls, not one transaction — see the module-level note in run_automation's
    docstring on why: ``DraftRepository.create``, reached through ``import_drafts``
    below, opens its own transaction internally, and this project's ``transaction()``
    helper does not support nesting. The evaluation insert is a single autocommitted
    statement, matching how ``EvaluationRepository.add`` is called everywhere else in
    this codebase.

    The residual risk this accepts: a crash between the two calls leaves a SHORTLIST
    evaluation with no draft. That article is not lost — ``eligibility_problem`` already
    lets a human pick it up through the ordinary ``ai-news draft export`` path exactly as
    if it had been shortlisted by a Claude Code session — it is only excluded from a
    *later automated* attempt, by the same "already evaluated" check every candidate is
    filtered through.
    """
    EvaluationRepository(connection).add(validated.evaluation)
    report = import_drafts(connection, validated.batch)
    return report.draft_ids[0]


def production_publications_today(
    connection, settings: Settings, moment: datetime, *, actor: str = AUTOMATION_ACTOR
) -> int:
    """How many ``actor`` posts already reached the production channel today.

    Public (and ``actor``-parameterized) so any pipeline version's scheduled entrypoint
    can share this same count against ``AI_NEWS_DAILY_POST_LIMIT`` — the daily cap is a
    property of the production channel, not of which pipeline handled a given post. v1's
    own call site (above) always passes the default ``AUTOMATION_ACTOR`` ("gemini:auto"),
    which the v2 scheduled pipeline (``automation.pipeline_v2_live``) also uses as its
    approval actor for exactly this reason — both draw from one shared ledger.
    """
    if not settings.telegram_channel:
        return 0
    publications = PublicationRepository(connection)
    decisions = ReviewDecisionRepository(connection)
    day = to_local(moment, CHANNEL_TIMEZONE).date()

    count = 0
    for publication in publications.list_recent(limit=200):
        if publication.channel != settings.telegram_channel:
            continue
        if publication.status.value != "SUCCEEDED" or publication.published_at is None:
            continue
        if to_local(publication.published_at, CHANNEL_TIMEZONE).date() != day:
            continue
        decision = decisions.get(publication.review_decision_id)
        if decision.actor == actor:
            count += 1
    return count
