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

from collections.abc import Mapping, Sequence
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
from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence
from ai_news_editor.editorial.diversity import RecentPost
from ai_news_editor.editorial.preview import PreviewCandidate, build_preview
from ai_news_editor.media.models import MediaOutcome
from ai_news_editor.media.pipeline import select_media
from ai_news_editor.media.workspace import MediaWorkspace
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.rendering.content import EditorialContent
from ai_news_editor.rendering.render import RenderedPost, render_editorial_post
from ai_news_editor.sources.config import SourceDefinition

logger = get_logger(__name__)


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
    capable = [row for row in rows if row.capability_ok]
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
) -> OrchestrationOutcome:
    """Run the full v2 pipeline for the single best candidate in ``candidates``.

    Primary-source preference (Step 3 ``editorial.primary_source``, section 30) is
    the caller's responsibility, applied *before* building ``candidates`` — that
    module operates on real ``Article`` rows and their ``possible_duplicate_of_id``
    links, which this function's deliberately thin ``ArticleContext`` input does not
    carry. A caller assembling ``candidates`` from the database should resolve each
    same-story group to its preferred (highest-trust-tier) article first, exactly as
    ``editorial.primary_source.prefer_primary_sources`` already does, so only one
    representative of any given story ever reaches this ranking step.

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

    media_outcome = select_media(
        workspace=workspace,
        source_url=top.source_url,
        media_policy=sources_by_id[top.source_id].media_policy,
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


__all__ = [
    "ArticleContext",
    "OrchestrationOutcome",
    "OrchestrationRejected",
    "run_pipeline_v2",
    "select_top_candidate",
]
