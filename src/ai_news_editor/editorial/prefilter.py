"""Deterministic prefilter.

Runs before any LLM sees an article, purely to avoid paying to evaluate things that are
obviously not stories. It is **not** the editorial classifier.

The bar is deliberately high: a rule only fires on material that is obviously not
publishable regardless of taste — an empty entry, a job posting, a navigation stub, an
earnings notice. Deciding whether a real story is *interesting to our readers* is Phase
4's job.

In particular nothing here filters on technicality. A deeply technical release can turn
out to matter enormously to ordinary users, and a keyword blocklist would throw it away
before anything could judge it. Every rejection carries a machine-readable reason, so no
article is ever silently discarded.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import PrefilterReason
from ai_news_editor.domain.models import Article

#: Items older than this are treated as feed artifacts rather than news. Generous on
#: purpose: several configured feeds carry multi-year archives, and a real story
#: republished a year later is not something we want to publish anyway.
STALE_AFTER = timedelta(days=400)

#: A title this short with no body carries no story.
MIN_TITLE_CHARS = 8

_JOB_PATTERNS = (
    r"\bwe(?:'| a)re hiring\b",
    r"\bjob opening\b",
    r"\bjoin our team\b",
    r"\bcareers? at\b",
    r"\bapply now\b",
    r"\bopen roles?\b",
    r"\b(?:senior|staff|principal|junior)\s+\w+\s+engineer\s*[-–—|]\s*\w+",
)

_BOILERPLATE_PATTERNS = (
    r"^\s*(?:page\s+\d+|archives?|index|sitemap|home)\s*$",
    r"^\s*(?:privacy policy|terms of (?:service|use)|cookie policy|legal notice)\s*$",
    r"^\s*(?:newsletter|subscribe|rss feed|about us|contact us)\s*$",
    r"^\s*(?:previous|next|older|newer)(?:\s+(?:page|posts?))?\s*$",
    r"^\s*\[?no title\]?\s*$",
    r"^\s*untitled\s*$",
)

_LEGAL_INVESTOR_PATTERNS = (
    r"\bform\s+(?:10-[kq]|8-k|s-1)\b",
    r"\b(?:first|second|third|fourth)[- ]quarter\s+(?:and\s+\w+\s+)?(?:results|earnings)\b",
    r"\bq[1-4]\s+(?:20\d\d\s+)?(?:results|earnings)\b",
    r"\bearnings (?:call|release|report)\b",
    r"\bannual (?:report|general meeting)\b",
    r"\b(?:declares|announces)\s+(?:quarterly\s+)?(?:cash\s+)?dividend\b",
    r"\binvestor relations\b",
)


@dataclass(frozen=True, slots=True)
class PrefilterRule:
    """One named, deterministic screening rule."""

    id: str
    reason: PrefilterReason
    description: str
    applies: Callable[[Article], bool]


def _compiled(patterns: tuple[str, ...]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


_JOB_RE = _compiled(_JOB_PATTERNS)
_BOILERPLATE_RE = _compiled(_BOILERPLATE_PATTERNS)
_LEGAL_RE = _compiled(_LEGAL_INVESTOR_PATTERNS)


def _is_empty(article: Article) -> bool:
    title = article.title.strip()
    body = (article.clean_text or "").strip()
    if body:
        return False
    return len(title) < MIN_TITLE_CHARS


def _is_stale(article: Article) -> bool:
    if article.published_at is None:
        return False  # Missing dates are common and are not evidence of staleness.
    return now_utc() - article.published_at > STALE_AFTER


def _matches(patterns: list[re.Pattern[str]], article: Article) -> bool:
    return any(pattern.search(article.title) for pattern in patterns)


#: Evaluated in order; the first match wins, so the recorded reason is the most
#: specific one that applied.
RULES: tuple[PrefilterRule, ...] = (
    PrefilterRule(
        id="rule.empty_content",
        reason=PrefilterReason.EMPTY_CONTENT,
        description="No body text and a title too short to carry a story.",
        applies=_is_empty,
    ),
    PrefilterRule(
        id="rule.boilerplate",
        reason=PrefilterReason.BOILERPLATE,
        description="Navigation, archive, index or site-plumbing entry.",
        applies=lambda article: _matches(_BOILERPLATE_RE, article),
    ),
    PrefilterRule(
        id="rule.job_listing",
        reason=PrefilterReason.JOB_LISTING,
        description="A hiring post rather than news.",
        applies=lambda article: _matches(_JOB_RE, article),
    ),
    PrefilterRule(
        id="rule.legal_or_investor",
        reason=PrefilterReason.LEGAL_OR_INVESTOR_NOTICE,
        description="Earnings, filings or investor-relations notice with no product story.",
        applies=lambda article: _matches(_LEGAL_RE, article),
    ),
    PrefilterRule(
        id="rule.stale_item",
        reason=PrefilterReason.STALE_ITEM,
        description="Published implausibly long ago; usually a malformed or archive feed.",
        applies=_is_stale,
    ),
)


@dataclass(frozen=True, slots=True)
class PrefilterVerdict:
    """The outcome of screening one article."""

    keep: bool
    rule_id: str | None = None
    reason: PrefilterReason | None = None

    @property
    def screened_out(self) -> bool:
        return not self.keep


KEEP = PrefilterVerdict(keep=True)


def screen(article: Article) -> PrefilterVerdict:
    """Screen one article. Deterministic: the same article always gets the same verdict."""
    for rule in RULES:
        if rule.applies(article):
            return PrefilterVerdict(keep=False, rule_id=rule.id, reason=rule.reason)
    return KEEP


def rule_by_id(rule_id: str) -> PrefilterRule:
    """Look up a rule so a recorded rejection can be explained."""
    for rule in RULES:
        if rule.id == rule_id:
            return rule
    raise KeyError(f"unknown prefilter rule {rule_id!r}")
