"""Source geography allowlist enforcement — Step 6B (AI News Agent v2).

This is a *source-origin* rule, not an article-subject rule: a Ukrainian, European,
British or American outlet reporting *about* Russia, Belarus or Iran is fine — the
rule only ever looks at where the candidate's own source is published, via
``SourceDefinition.publisher_region``, never at what the article is about.

Enforced deterministically and locally, before any candidate reaches Gemini selection
or classification — Gemini never decides this. A source whose origin has not been
reviewed (``PublisherRegion.UNKNOWN``) is ineligible until it is explicitly classified,
which is what makes this an allowlist rather than a blacklist: nothing is eligible
by default, and an unreviewed or forbidden origin fails closed.
"""

from __future__ import annotations

from ai_news_editor.domain.enums import PublisherRegion
from ai_news_editor.sources.config import SourceDefinition

#: The only regions an eligible source's publisher may be from. United Kingdom is kept
#: as its own reviewed enum member (see ``PublisherRegion``) but counts as Europe here,
#: exactly as specified.
ALLOWED_REGIONS: frozenset[PublisherRegion] = frozenset(
    {
        PublisherRegion.UKRAINE,
        PublisherRegion.EUROPE,
        PublisherRegion.UNITED_KINGDOM,
        PublisherRegion.UNITED_STATES,
    }
)

#: Named explicitly so a rejection can say *which* forbidden country, not just "not
#: allowed" — see ``PublisherRegion``'s own docstring for why these are never folded
#: into a generic ``OTHER``.
EXPLICITLY_FORBIDDEN_REGIONS: frozenset[PublisherRegion] = frozenset(
    {PublisherRegion.RUSSIA, PublisherRegion.BELARUS, PublisherRegion.IRAN}
)


class GeographyError(ValueError):
    """A source's publisher origin is not on the channel's eligibility allowlist."""


def is_source_eligible(source: SourceDefinition) -> bool:
    """Whether ``source``'s reviewed publisher origin is on the allowlist.

    Fails closed: ``PublisherRegion.UNKNOWN`` (not yet reviewed) and
    ``PublisherRegion.OTHER`` (reviewed, but outside UA/EU/UK/US) are both ineligible,
    exactly like an explicitly forbidden region — the allowlist has no "assume yes"
    path.
    """
    return source.publisher_region in ALLOWED_REGIONS


def validate_source_geography(source: SourceDefinition) -> None:
    """Raises :class:`GeographyError` if ``source`` is not eligible.

    Called before a source's candidates are ever offered to Gemini selection — see
    ``automation.soak.eligible_articles_v2`` / ``automation.pipeline_v2`` callers.
    """
    if is_source_eligible(source):
        return
    if source.publisher_region in EXPLICITLY_FORBIDDEN_REGIONS:
        raise GeographyError(
            f"{source.id!r} is published from {source.publisher_region.value}, which is "
            "explicitly forbidden regardless of trust tier or content quality"
        )
    if source.publisher_region is PublisherRegion.UNKNOWN:
        raise GeographyError(
            f"{source.id!r} has no reviewed publisher_region — unknown origin fails "
            "closed until it is explicitly classified"
        )
    raise GeographyError(
        f"{source.id!r} is published from {source.publisher_region.value}, which is not "
        "on the channel's geography allowlist (UKRAINE, EUROPE, UNITED_KINGDOM, "
        "UNITED_STATES)"
    )


__all__ = [
    "ALLOWED_REGIONS",
    "EXPLICITLY_FORBIDDEN_REGIONS",
    "GeographyError",
    "is_source_eligible",
    "validate_source_geography",
]
