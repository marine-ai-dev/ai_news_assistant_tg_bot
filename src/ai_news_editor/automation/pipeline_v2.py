"""The v2 editorial pipeline — Step 5 (AI News Agent v2).

SOURCE -> ARTICLE -> EDITORIAL CATEGORY -> EVIDENCE -> DIVERSITY -> GENERATION ->
MEDIA -> CATEGORY-SPECIFIC TELEGRAM RENDERING -> TESTABLE PUBLICATION PLAN.

This module wires together every piece Steps 3-5 built, bounded to a small, audited
number of Gemini calls per successful post: **at most 2** — one classification call
(``automation.classification.classify_candidate``, skipped entirely when a candidate
already carries a known ``editorial_category``/``evidence_type`` from an earlier
evaluation) and one generation call (``automation.generation_v2.generate_editorial_content``).
Diversity, capability validation, primary-source preference, media selection, and
rendering are all deterministic — zero further Gemini calls for any of them, matching
the explicit "avoid 5 Gemini calls per post" quota-efficiency requirement.

This is **not** the live ``automation.pipeline._run_pipeline`` entrypoint. It is a new,
additive orchestration path — reads a candidate pool the caller has already assembled
(the same "already-loaded data in, decision out" shape ``editorial.preview.build_preview``
already established), and is never invoked by the unattended cron workflow.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from ai_news_editor.automation.classification import (
    Classification,
    ClassificationInvalid,
    ClassificationRejected,
    classify_candidate,
)
from ai_news_editor.automation.gemini import GeminiClient
from ai_news_editor.automation.generation_v2 import (
    GenerationV2Invalid,
    GenerationV2Rejected,
    build_editorial_content,
    generate_editorial_content,
)
from ai_news_editor.automation.schema import SelectionCandidate
from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence, TrustTier
from ai_news_editor.domain.models import Article
from ai_news_editor.editorial.diversity import RecentPost
from ai_news_editor.editorial.preview import PreviewCandidate, build_preview
from ai_news_editor.editorial.primary_source import prefer_primary_sources
from ai_news_editor.media.models import MediaOutcome
from ai_news_editor.media.strategy import select_media_with_fallbacks
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.rendering.content import EditorialContent
from ai_news_editor.rendering.render import RenderedPost, render_editorial_post
from ai_news_editor.sources.config import SourceDefinition
from ai_news_editor.sources.geography import is_source_eligible
from ai_news_editor.sources.http import HttpClient

logger = get_logger(__name__)


def collapse_to_primary_sources(
    articles: Sequence[Article], trust_tier_of: Callable[[Article], TrustTier]
) -> list[Article]:
    """Section 30, actually wired: before any candidate reaches selection, collapse
    each same-story cluster down to its single highest-trust-tier representative.

    A Tier B article and an equivalent Tier A official announcement of the same
    story — linked via the existing ``Article.possible_duplicate_of_id`` normalization
    already records — never both reach ``select_top_candidate``; only the Tier A one
    does. Zero Gemini calls: this is exactly ``editorial.primary_source.prefer_primary_sources``,
    called here instead of left as a note for the caller to remember.
    """
    preferred = prefer_primary_sources(articles, trust_tier_of)
    seen: set[UUID] = set()
    result: list[Article] = []
    for article in articles:
        primary = preferred[article.id]
        if primary.id not in seen:
            seen.add(primary.id)
            result.append(primary)
    return result


@dataclass(frozen=True, slots=True)
class ArticleContext:
    """Everything about one candidate this pipeline needs, gathered by the caller.

    Deliberately not a DB read inside this module — matching ``editorial.preview``'s
    own "already-loaded data in" shape, so this function stays a pure, easily-tested
    decision given a small, explicit input rather than a second DB-querying layer
    duplicating ``automation.pipeline``'s own candidate collection.
    """

    article_id: UUID
    title: str
    source_id: str
    #: Already known from a prior evaluation, if any — ``None`` means "not yet
    #: classified," the only case that spends a classification call.
    editorial_category: EditorialCategory | None
    evidence_type: EditorialEvidence | None
    composite_score: float
    article_text: str
    source_url: str
    source_label: str
    #: For media discovery — see ``media.pipeline.select_media``.
    feed_payload_raw: str | None = None
    html: str | None = None


@dataclass(frozen=True, slots=True)
class OrchestrationOutcome:
    """One successful post, ready for the publication-plan layer."""

    content: EditorialContent
    rendered: RenderedPost
    media: MediaOutcome
    classification: Classification | None
    #: Exact count for the quota-efficiency audit — never guessed.
    gemini_calls: int


class OrchestrationRejected(Exception):
    """No candidate produced a publishable post. A normal outcome, not a crash."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def select_top_candidate(
    candidates: Sequence[ArticleContext],
    sources_by_id: Mapping[str, SourceDefinition],
    recent: Sequence[RecentPost],
) -> ArticleContext | None:
    """Rank candidates by diversity-adjusted score, filtered by source capability.

    Reuses ``editorial.preview.build_preview`` directly — the same capability
    (Step 2/3 ``sources.capability``) and diversity (Step 3 ``editorial.diversity``,
    fed by real recent history) logic Step 3 already built and Step 5 section 28-29
    asks to be *wired into selection*, not just displayed. Zero Gemini calls.

    Step 6B: also filters by the source geography allowlist (``sources.geography``)
    here, locally and deterministically — a second, defense-in-depth layer on top of
    whatever pre-filtering a caller (e.g. ``automation.soak``) already did, so a future
    caller that forgets its own geography filter still cannot offer an ineligible
    source's candidate to Gemini selection.
    """
    by_article_id = {c.article_id: c for c in candidates}
    preview_candidates = [
        PreviewCandidate(
            article_id=c.article_id,
            title=c.title,
            source_id=c.source_id,
            editorial_category=c.editorial_category or EditorialCategory.NEWS,
            evidence_type=c.evidence_type,
            composite_score=c.composite_score,
        )
        for c in candidates
    ]
    rows = build_preview(preview_candidates, sources_by_id, recent)
    capable = [
        row
        for row in rows
        if row.capability_ok
        and row.candidate.source_id in sources_by_id
        and is_source_eligible(sources_by_id[row.candidate.source_id])
    ]
    if not capable:
        return None
    return by_article_id[capable[0].candidate.article_id]


def run_pipeline_v2(
    *,
    client: GeminiClient,
    candidates: Sequence[ArticleContext],
    sources_by_id: Mapping[str, SourceDefinition],
    recent: Sequence[RecentPost],
    workspace: MediaWorkspace,
    http: HttpClient,
) -> OrchestrationOutcome:
    """Run the full v2 pipeline for the single best candidate in ``candidates``.

    Primary-source preference (Step 3 ``editorial.primary_source``, section 30) is
    applied *before* building ``candidates`` — call ``collapse_to_primary_sources`` on
    the real ``Article`` rows first, since that module operates on
    ``possible_duplicate_of_id`` links this function's deliberately thin
    ``ArticleContext`` input does not carry itself. A caller assembling ``candidates``
    from the database should always do that collapse first, so only one
    representative of any given story ever reaches this ranking step.

    ``http`` is Step 6B's addition — the full four-layer media strategy
    (``media.strategy.select_media_with_fallbacks``) needs a real HTTP client for its
    licensed-first-party and open-license-provider layers, unlike the Step 5 media
    selection this replaces, which only ever read what the caller already fetched.

    Raises:
        OrchestrationRejected: no candidate was capability-eligible, or the winning
            candidate's classification/generation call was rejected or invalid. The
            caller falls back to whatever the live pipeline already does — this
            function makes no retry decision of its own.
    """
    top = select_top_candidate(candidates, sources_by_id, recent)
    if top is None:
        raise OrchestrationRejected("no candidate passed source-capability validation")

    gemini_calls = 0
    classification: Classification | None = None
    category = top.editorial_category
    evidence = top.evidence_type

    if category is None or evidence is None:
        selection_candidate = SelectionCandidate(
            id=str(top.article_id),
            source_name=top.source_label,
            title=top.title,
            url=top.source_url,
        )
        try:
            classification = classify_candidate(client, selection_candidate)
        except (ClassificationRejected, ClassificationInvalid) as exc:
            raise OrchestrationRejected(f"classification failed: {exc}") from exc
        gemini_calls += 1
        category = classification.content_type
        evidence = classification.evidence_type

    try:
        generated = generate_editorial_content(
            client, category=category, article_text=top.article_text, source_label=top.source_label
        )
    except (GenerationV2Rejected, GenerationV2Invalid) as exc:
        raise OrchestrationRejected(f"generation failed: {exc}") from exc
    gemini_calls += 1

    content = build_editorial_content(
        generated, category=category, evidence=evidence, source_url=top.source_url
    )
    rendered = render_editorial_post(content)

    media_outcome = select_media_with_fallbacks(
        workspace=workspace,
        http=http,
        source_id=top.source_id,
        source_url=top.source_url,
        media_policy=sources_by_id[top.source_id].media_policy,
        category=category,
        headline=content.headline,
        source_label=top.source_label,
        story_keywords=_story_keywords(content.headline, top.source_label),
        feed_payload_raw=top.feed_payload_raw,
        html=top.html,
    )

    logger.info(
        "pipeline_v2_outcome",
        extra={
            "category": category.value,
            "evidence": evidence.value if evidence else None,
            "gemini_calls": gemini_calls,
            "media_ok": media_outcome.ok,
        },
    )
    return OrchestrationOutcome(
        content=content,
        rendered=rendered,
        media=media_outcome,
        classification=classification,
        gemini_calls=gemini_calls,
    )


#: A capitalized Latin-script word or run of them — how a brand/product name usually
#: stays untranslated inside an otherwise-Ukrainian headline ("Google", "ChatGPT",
#: "OpenAI Sora"). This is a keyword *hint* for the media strategy's relevance
#: matching, not a translation or an NLP step — matching ``media.licensed_assets`` and
#: ``media.open_license``'s own plain substring matching.
_LATIN_PROPER_NOUN = re.compile(r"[A-Z][A-Za-z0-9]*(?:\s[A-Z][A-Za-z0-9]*)*")


def _story_keywords(headline: str, source_label: str) -> list[str]:
    """Short, specific terms for the media strategy's relevance matching — the
    source's own name plus any Latin-script proper nouns the headline itself names.
    Deliberately not the whole headline: a single generic word ("новий", "AI") would
    match almost anything, defeating the point of a relevance check.
    """
    found = _LATIN_PROPER_NOUN.findall(headline)
    keywords = [source_label, *found]
    seen: set[str] = set()
    unique: list[str] = []
    for keyword in keywords:
        normalized = keyword.strip()
        if normalized and normalized.lower() not in seen:
            seen.add(normalized.lower())
            unique.append(normalized)
    return unique


__all__ = [
    "ArticleContext",
    "OrchestrationOutcome",
    "OrchestrationRejected",
    "collapse_to_primary_sources",
    "run_pipeline_v2",
    "select_top_candidate",
]
