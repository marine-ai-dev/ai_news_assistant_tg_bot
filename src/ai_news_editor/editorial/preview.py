"""Deterministic editorial preview — Step 3 (AI News Agent v2), section 29.

No Gemini call, no Telegram call: the offline explain surface for the classification,
capability and diversity modules Step 3 built. Pure function of already-loaded data —
``cli.editorial``'s ``preview`` command does the reading (shortlist, source registry,
recent publication history) and this module only assembles, checks and ranks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence, TrustTier
from ai_news_editor.editorial.diversity import DEFAULT_WEIGHTS, DiversityWeights, RecentPost, rank
from ai_news_editor.sources.capability import CapabilityError, validate_classification
from ai_news_editor.sources.config import SourceDefinition


@dataclass(frozen=True, slots=True)
class PreviewCandidate:
    """One shortlisted candidate, as already known before this module runs."""

    article_id: UUID
    title: str
    source_id: str
    editorial_category: EditorialCategory
    evidence_type: EditorialEvidence | None
    composite_score: float


@dataclass(frozen=True, slots=True)
class PreviewRow:
    """One line of the preview: what was decided, and why."""

    candidate: PreviewCandidate
    source_family: str | None
    trust_tier: TrustTier | None
    capability_ok: bool
    capability_reason: str | None
    diversity_adjustment: float
    final_score: float


def build_preview(
    candidates: Sequence[PreviewCandidate],
    sources_by_id: Mapping[str, SourceDefinition],
    recent: Sequence[RecentPost],
    *,
    weights: DiversityWeights = DEFAULT_WEIGHTS,
) -> list[PreviewRow]:
    """Rank ``candidates`` by diversity-adjusted score and explain each one.

    A candidate whose source is missing from the registry, or whose classification
    the registry's declared capability/trust tier does not support, is still shown
    (never dropped — same "explain, don't hide" discipline as the rest of this
    project's diagnostics) with ``capability_ok=False`` and a human-readable reason.
    """
    ranked = rank(
        [
            (
                candidate,
                candidate.editorial_category,
                _source_family(candidate, sources_by_id),
                candidate.composite_score,
            )
            for candidate in candidates
        ],
        recent,
        weights=weights,
    )

    rows: list[PreviewRow] = []
    for scored in ranked:
        candidate = scored.candidate
        source = sources_by_id.get(candidate.source_id)
        capability_ok, capability_reason = _check_capability(candidate, source)
        rows.append(
            PreviewRow(
                candidate=candidate,
                source_family=source.source_family if source else None,
                trust_tier=source.trust_tier if source else None,
                capability_ok=capability_ok,
                capability_reason=capability_reason,
                diversity_adjustment=scored.adjustment,
                final_score=scored.final_score,
            )
        )
    return rows


def _source_family(
    candidate: PreviewCandidate, sources_by_id: Mapping[str, SourceDefinition]
) -> str | None:
    source = sources_by_id.get(candidate.source_id)
    return source.source_family if source else None


def _check_capability(
    candidate: PreviewCandidate, source: SourceDefinition | None
) -> tuple[bool, str | None]:
    if source is None:
        return False, f"source {candidate.source_id!r} not found in the registry"
    if candidate.evidence_type is None:
        return True, None
    try:
        validate_classification(
            source, category=candidate.editorial_category, evidence=candidate.evidence_type
        )
    except CapabilityError as exc:
        return False, str(exc)
    return True, None
