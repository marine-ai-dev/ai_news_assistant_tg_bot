"""A week of the channel, assembled from what is already scheduled.

This module answers one question an editor asks constantly and a database cannot:
*what should go out next so the channel stays varied, useful and readable?*

Three things it deliberately is not.

**It is not a second scheduler.** The Phase-9 publication queue is the only source of
truth about when anything publishes. Everything here is a read.

**It is not an editor.** It produces diagnostics and one explainable suggestion. It never
schedules, never reorders, never approves, and never writes a row. A warning about four
news posts in a row is a sentence for a human, not an instruction to a machine.

**It is not an engagement model.** There is no analytics data in this project yet, so
there is no such thing as a best time to post, and nothing here claims otherwise. The
posting windows are the ones the owner configured, named after parts of the day.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    ContentType,
    DraftStatus,
    EvidenceKind,
    QueueStatus,
    SourceTier,
)
from ai_news_editor.domain.models import Draft, DraftVersion, QueueItem
from ai_news_editor.planning.buckets import (
    ACCESSIBLE_TARGET_MAX,
    ACCESSIBLE_TARGET_MIN,
    DEFAULT_MIX,
    Bucket,
    bucket_for,
    is_accessible,
    is_practical,
)
from ai_news_editor.scheduling.clock import CHANNEL_TIMEZONE, to_local
from ai_news_editor.scheduling.freshness import check_freshness, freshness_window
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    ContentItemRepository,
    DraftRepository,
    ReviewDecisionRepository,
)
from ai_news_editor.storage.repositories.publication_queue import PublicationQueueRepository

#: How long the channel may go without giving the reader something to try before that
#: is worth saying out loud. The channel's whole promise is "you can use this today".
PRACTICAL_GAP_DAYS = 7

#: How many of the same thing in a row starts to read as a rut.
MAX_SAME_BUCKET_STREAK = 3
#: Technical content in a row is a faster problem than repetition, so it is stricter.
MAX_TECHNICAL_STREAK = 2

#: When one publisher or one tool dominates upcoming content, say so. Not a rejection —
#: Google shipping three interesting things in a week is a fact, not a bias.
CONCENTRATION_THRESHOLD = 0.60
#: Below this many items, any share is noise rather than a pattern.
CONCENTRATION_MIN_ITEMS = 3

#: A resource is a standalone thing a reader saves. Crowding it is worth a soft note.
RESOURCE_BREATHING_ROOM = timedelta(hours=3)


class Freshness(StrEnum):
    """How close a scheduled post is to needing another human look."""

    FRESH = "FRESH"
    AGING = "AGING"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class Entry:
    """One scheduled post, with everything the calendar needs to talk about it."""

    item: QueueItem
    draft: Draft
    version: DraftVersion
    bucket: Bucket
    freshness: Freshness
    #: Who published the story this came from, when it came from somebody.
    source_id: str | None = None
    #: How close the evidence sits to whoever ran the workflow.
    source_tier: SourceTier | None = None
    evidence_kind: EvidenceKind | None = None
    #: The consumer AI tool the post is about, when the metadata records one. Never
    #: guessed from prose — an absent tool is recorded as absent.
    tool: str | None = None
    #: Name and position, for content that belongs to an ordered series. Stored on the
    #: ContentItem, which is where a series is actually defined.
    series: tuple[str, int] | None = None

    @property
    def local_time(self) -> datetime:
        return to_local(self.item.scheduled_for, self.item.display_timezone)

    @property
    def audience(self) -> AudienceTier:
        return self.version.audience

    @property
    def category(self) -> Category:
        return self.version.category

    @property
    def content_type(self) -> ContentType:
        return self.draft.content_type

    @property
    def accessible(self) -> bool:
        return is_accessible(self.version.audience)

    @property
    def is_lifehack(self) -> bool:
        """A use case somebody reported rather than a vendor demonstrating a feature."""
        return self.evidence_kind is EvidenceKind.USER_REPORTED_LIFEHACK


@dataclass(frozen=True, slots=True)
class Warning_:
    """Something an editor should know, phrased for a person rather than a log."""

    kind: str
    message: str
    #: INFO is an observation; NOTE is worth acting on this week.
    severity: str = "NOTE"


@dataclass(slots=True)
class Week:
    """Everything scheduled between two dates, and what is notable about it."""

    start: date
    end: date
    entries: list[Entry] = field(default_factory=list)
    warnings: list[Warning_] = field(default_factory=list)

    @property
    def by_day(self) -> dict[date, list[Entry]]:
        days: dict[date, list[Entry]] = {}
        for entry in sorted(self.entries, key=lambda e: e.item.scheduled_for):
            days.setdefault(entry.local_time.date(), []).append(entry)
        return days

    @property
    def accessible_share(self) -> float:
        if not self.entries:
            return 0.0
        return sum(1 for e in self.entries if e.accessible) / len(self.entries)

    @property
    def mix(self) -> dict[Bucket, int]:
        counts = Counter(e.bucket for e in self.entries)
        return {bucket: counts.get(bucket, 0) for bucket in Bucket}


# --------------------------------------------------------------------------- reading


def _freshness_of(
    connection: sqlite3.Connection, item: QueueItem, draft: Draft, now: datetime
) -> Freshness:
    """How much of this post's approval window is left when it is due to publish.

    Uses the Phase-9 policy rather than a second set of rules, and asks about the
    *scheduled* moment rather than now: a news post scheduled for Friday is the one that
    will be stale, however fresh it looks today.
    """
    decision = ReviewDecisionRepository(connection).latest_approval(
        item.draft_id, item.draft_version_id
    )
    if decision is None:
        return Freshness.EXPIRED

    at_publication = max(item.scheduled_for, now)
    verdict = check_freshness(
        content_type=draft.content_type, approved_at=decision.created_at, now=at_publication
    )
    if not verdict:
        return Freshness.EXPIRED

    window = freshness_window(draft.content_type)
    used = (at_publication - decision.created_at) / window
    return Freshness.AGING if used >= 0.6 else Freshness.FRESH


def _provenance(
    connection: sqlite3.Connection, draft: Draft
) -> tuple[str | None, SourceTier | None, EvidenceKind | None, str | None, tuple[str, int] | None]:
    """Where this came from and what tool it is about, from stored metadata only.

    Nothing here is inferred from prose. A post whose metadata does not name a tool has
    no tool, and the diagnostics say nothing rather than guessing.
    """
    source_id: str | None = None
    source_tier: SourceTier | None = None
    evidence_kind: EvidenceKind | None = None
    tool: str | None = None
    series: tuple[str, int] | None = None

    if draft.article_id is not None:
        try:
            source_id = ArticleRepository(connection).get(draft.article_id).source_id
        except Exception:  # pragma: no cover - an orphaned article cannot be queued
            source_id = None

    if draft.content_item_id is not None:
        try:
            item = ContentItemRepository(connection).get(draft.content_item_id)
        except Exception:  # pragma: no cover - a queued item always resolves
            return source_id, source_tier, evidence_kind, tool, series
        evidence_kind = item.evidence_kind
        if item.series_name is not None and item.series_order is not None:
            series = (item.series_name, item.series_order)
        if item.evidence is not None:
            source_tier = item.evidence.source_tier
            tool = item.evidence.tool_used
            source_id = source_id or item.evidence.source_platform

    return source_id, source_tier, evidence_kind, tool, series


def build_entry(
    connection: sqlite3.Connection, item: QueueItem, *, now: datetime
) -> Entry | None:
    """Everything the calendar knows about one scheduled post."""
    drafts = DraftRepository(connection)
    try:
        draft = drafts.get(item.draft_id)
        version = drafts.get_version(item.draft_version_id)
    except Exception:  # pragma: no cover - a queued version always exists
        return None

    source_id, source_tier, evidence_kind, tool, series = _provenance(connection, draft)
    return Entry(
        item=item,
        draft=draft,
        version=version,
        bucket=bucket_for(draft.content_type, version.category),
        freshness=_freshness_of(connection, item, draft, now),
        source_id=source_id,
        source_tier=source_tier,
        evidence_kind=evidence_kind,
        tool=tool,
        series=series,
    )


def week_bounds(reference: datetime, *, offset: int = 0) -> tuple[date, date]:
    """Monday to Sunday of the week containing ``reference``, in channel time."""
    local = to_local(reference).date() + timedelta(weeks=offset)
    monday = local - timedelta(days=local.weekday())
    return monday, monday + timedelta(days=6)


def build_week(
    connection: sqlite3.Connection, *, now: datetime, offset: int = 0
) -> Week:
    """Assemble one week from the publication queue, and diagnose it."""
    start, end = week_bounds(now, offset=offset)
    queue = PublicationQueueRepository(connection)

    entries: list[Entry] = []
    for item in queue.list_all(limit=500):
        if item.status in {QueueStatus.CANCELLED, QueueStatus.INVALIDATED}:
            continue
        local_date = to_local(item.scheduled_for, item.display_timezone).date()
        if not (start <= local_date <= end):
            continue
        entry = build_entry(connection, item, now=now)
        if entry is not None:
            entries.append(entry)

    entries.sort(key=lambda e: e.item.scheduled_for)
    week = Week(start=start, end=end, entries=entries)
    week.warnings = diagnose(connection, week, now=now)
    return week


# ----------------------------------------------------------------------- diagnostics


def diagnose(connection: sqlite3.Connection, week: Week, *, now: datetime) -> list[Warning_]:
    """Everything worth telling an editor about this week.

    Every one of these is a sentence, never an action. Nothing below reorders a post,
    changes a time, or refuses anything.
    """
    found: list[Warning_] = []
    entries = week.entries

    if not entries:
        found.append(
            Warning_(
                "empty",
                "Nothing is scheduled this week. 'ai-news calendar gaps' shows what is "
                "approved and waiting for a slot.",
            )
        )
        return found

    found.extend(_audience_warnings(week))
    found.extend(_mix_warnings(week))
    found.extend(_streak_warnings(entries))
    found.extend(_practical_gap_warnings(connection, entries, now=now))
    found.extend(_concentration_warnings(entries))
    found.extend(_series_warnings(entries))
    found.extend(_resource_warnings(entries))
    found.extend(_freshness_warnings(entries))
    return found


def _audience_warnings(week: Week) -> list[Warning_]:
    total = len(week.entries)
    accessible = sum(1 for e in week.entries if e.accessible)
    share = week.accessible_share

    if accessible == 0:
        return [
            Warning_(
                "audience",
                f"None of this week's {total} posts is written for a newcomer or "
                "beginner. The channel is for people who have never opened an AI chat.",
            )
        ]
    if share < ACCESSIBLE_TARGET_MIN:
        return [
            Warning_(
                "audience",
                f"Only {accessible} of {total} posts ({share:.0%}) are beginner-"
                f"accessible; the channel aims for {ACCESSIBLE_TARGET_MIN:.0%}–"
                f"{ACCESSIBLE_TARGET_MAX:.0%}.",
            )
        ]
    return [
        Warning_(
            "audience",
            f"{accessible} of {total} posts ({share:.0%}) are beginner-accessible.",
            severity="INFO",
        )
    ]


def _mix_warnings(week: Week) -> list[Warning_]:
    """Which buckets are missing or dominant, against the editorial targets.

    Only reported for a week with enough posts to have a shape. Two posts cannot be
    unbalanced; they are just two posts.
    """
    total = len(week.entries)
    if total < 4:
        return []

    found: list[Warning_] = []
    mix = week.mix

    missing = [b for b, target in DEFAULT_MIX.items() if target >= 0.15 and mix[b] == 0]
    if missing:
        names = ", ".join(b.value for b in missing)
        found.append(
            Warning_("mix", f"No {names} content scheduled this week.")
        )

    for bucket, count in mix.items():
        share = count / total
        target = DEFAULT_MIX[bucket]
        if count >= 3 and share > target * 2:
            found.append(
                Warning_(
                    "mix",
                    f"{count} of {total} posts are {bucket.value} ({share:.0%}); the "
                    f"target is around {target:.0%}. Targets are guidance, not quotas.",
                    severity="INFO",
                )
            )
    return found


def _streak_warnings(entries: list[Entry]) -> list[Warning_]:
    """Runs of the same kind of post, in publication order."""
    found: list[Warning_] = []

    run_bucket, run_length = None, 0
    for entry in entries:
        if entry.bucket == run_bucket:
            run_length += 1
        else:
            run_bucket, run_length = entry.bucket, 1
        if run_length == MAX_SAME_BUCKET_STREAK + 1:
            found.append(
                Warning_(
                    "streak",
                    f"{run_length} {run_bucket.value} posts in a row around "
                    f"{entry.local_time:%a %d %b}. Consider breaking the run.",
                )
            )

    technical_run = 0
    for entry in entries:
        if entry.accessible:
            technical_run = 0
            continue
        technical_run += 1
        if technical_run == MAX_TECHNICAL_STREAK + 1:
            found.append(
                Warning_(
                    "streak",
                    f"{technical_run} posts in a row assume prior AI experience, ending "
                    f"{entry.local_time:%a %d %b}.",
                )
            )
    return found


def _practical_gap_warnings(
    connection: sqlite3.Connection, entries: list[Entry], *, now: datetime
) -> list[Warning_]:
    """Has the reader been given anything to actually do lately?"""
    if any(is_practical(e.content_type) for e in entries):
        return []

    queue = PublicationQueueRepository(connection)
    drafts = DraftRepository(connection)
    cutoff = now - timedelta(days=PRACTICAL_GAP_DAYS)
    for item in queue.list_all(limit=500):
        if item.status is not QueueStatus.PUBLISHED or item.scheduled_for < cutoff:
            continue
        with suppress(Exception):  # pragma: no cover - a published draft always exists
            if is_practical(drafts.get(item.draft_id).content_type):
                return []

    return [
        Warning_(
            "practical",
            f"No prompt, use case or lifehack scheduled or published in the last "
            f"{PRACTICAL_GAP_DAYS} days. 'Try this today' is what the channel promises.",
        )
    ]


def _concentration_warnings(entries: list[Entry]) -> list[Warning_]:
    """One publisher or one tool dominating the week.

    A signal, never a rejection. If Google shipped three interesting things this week,
    three Google posts is reporting, not bias — the editor decides which it is.
    """
    found: list[Warning_] = []

    sources = Counter(e.source_id for e in entries if e.source_id)
    if sum(sources.values()) >= CONCENTRATION_MIN_ITEMS:
        name, count = sources.most_common(1)[0]
        share = count / sum(sources.values())
        if share >= CONCENTRATION_THRESHOLD:
            found.append(
                Warning_(
                    "source",
                    f"{count} of {sum(sources.values())} sourced posts come from "
                    f"{name}. Worth a second source for variety, not a reason to drop "
                    "anything.",
                )
            )

    practical = [e for e in entries if is_practical(e.content_type) and e.tool]
    tools = Counter(e.tool for e in practical)
    if len(practical) >= CONCENTRATION_MIN_ITEMS:
        name, count = tools.most_common(1)[0]
        if count / len(practical) >= CONCENTRATION_THRESHOLD:
            found.append(
                Warning_(
                    "tool",
                    f"{count} of {len(practical)} practical posts are about {name}. "
                    "Readers use different tools.",
                )
            )

            # The combined signal, when both point the same way, is the useful one.
            vendor_led = [
                e for e in practical
                if e.tool == name and e.source_tier is SourceTier.OFFICIAL_PRODUCT
            ]
            if len(vendor_led) >= CONCENTRATION_MIN_ITEMS:
                found.append(
                    Warning_(
                        "source+tool",
                        f"Most upcoming practical content is about {name} and comes "
                        "from the vendor's own material. An independent write-up or an "
                        "owner-tested example would balance it.",
                    )
                )
    return found


def _series_warnings(entries: list[Entry]) -> list[Warning_]:
    """Parts of an ordered series must go out in order."""
    found: list[Warning_] = []
    by_series: dict[str, list[Entry]] = {}
    for entry in entries:
        series = entry.series
        if series is not None:
            by_series.setdefault(series[0], []).append(entry)

    for name, items in by_series.items():
        in_schedule_order = sorted(items, key=lambda e: e.item.scheduled_for)
        positions = [e.series[1] for e in in_schedule_order]  # type: ignore[index]
        if positions != sorted(positions):
            found.append(
                Warning_(
                    "series",
                    f"'{name}' is scheduled out of order: parts go "
                    f"{' → '.join(str(p) for p in positions)}. Nothing was moved.",
                )
            )
        duplicates = [p for p, n in Counter(positions).items() if n > 1]
        if duplicates:
            found.append(
                Warning_(
                    "series",
                    f"'{name}' has part {duplicates[0]} scheduled more than once.",
                )
            )
    return found


def _resource_warnings(entries: list[Entry]) -> list[Warning_]:
    """A downloadable resource crowded between two other posts."""
    found: list[Warning_] = []
    for index, entry in enumerate(entries):
        if entry.content_type is not ContentType.RESOURCE:
            continue
        neighbours = [
            other
            for offset in (-1, 1)
            if 0 <= index + offset < len(entries)
            for other in [entries[index + offset]]
            if abs(other.item.scheduled_for - entry.item.scheduled_for)
            < RESOURCE_BREATHING_ROOM
        ]
        if neighbours:
            found.append(
                Warning_(
                    "resource",
                    f"The resource on {entry.local_time:%a %d %b · %H:%M} has another "
                    "post within a few hours. A thing readers save is easier to notice "
                    "with room around it.",
                    severity="INFO",
                )
            )
    return found


def _freshness_warnings(entries: list[Entry]) -> list[Warning_]:
    expired = [e for e in entries if e.freshness is Freshness.EXPIRED]
    if expired:
        return [
            Warning_(
                "freshness",
                f"{len(expired)} scheduled post(s) will be past their freshness window "
                "by the time they publish, and will be held for review instead.",
            )
        ]
    aging_news = [
        e for e in entries
        if e.freshness is Freshness.AGING and e.content_type is ContentType.NEWS
    ]
    if aging_news:
        first = min(aging_news, key=lambda e: e.item.scheduled_for)
        return [
            Warning_(
                "freshness",
                f"{len(aging_news)} news post(s) are ageing — the earliest publishes "
                f"{first.local_time:%a %d %b · %H:%M}. News keeps badly.",
                severity="INFO",
            )
        ]
    return []


# ------------------------------------------------------------------- what is waiting


@dataclass(frozen=True, slots=True)
class Waiting:
    """An approved draft with no slot yet — the pool to choose the next post from."""

    draft: Draft
    version: DraftVersion
    bucket: Bucket
    freshness: Freshness
    approved_at: datetime | None


def approved_unscheduled(
    connection: sqlite3.Connection, *, now: datetime, channel: str, limit: int = 50
) -> list[Waiting]:
    """Approved drafts nobody has scheduled yet.

    Kept separate from the calendar on purpose: these are candidates, not commitments,
    and nothing here queues any of them.
    """
    drafts = DraftRepository(connection)
    decisions = ReviewDecisionRepository(connection)
    queue = PublicationQueueRepository(connection)

    waiting: list[Waiting] = []
    for draft in drafts.list_by_status(DraftStatus.APPROVED, limit=limit):
        version = drafts.current_version(draft.id)
        if queue.active_for_version(version.id, channel) is not None:
            continue
        decision = decisions.latest_approval(draft.id, version.id)
        approved_at = decision.created_at if decision else None
        verdict = (
            check_freshness(
                content_type=draft.content_type, approved_at=approved_at, now=now
            )
            if approved_at
            else None
        )
        if verdict is None or not verdict:
            freshness = Freshness.EXPIRED
        else:
            used = (now - approved_at) / freshness_window(draft.content_type)  # type: ignore[operator]
            freshness = Freshness.AGING if used >= 0.6 else Freshness.FRESH

        waiting.append(
            Waiting(
                draft=draft,
                version=version,
                bucket=bucket_for(draft.content_type, version.category),
                freshness=freshness,
                approved_at=approved_at,
            )
        )
    # Most urgent first: what expires soonest should be decided about soonest.
    order = {Freshness.EXPIRED: 0, Freshness.AGING: 1, Freshness.FRESH: 2}
    waiting.sort(key=lambda w: (order[w.freshness], w.approved_at or now))
    return waiting


def pending_summary(connection: sqlite3.Connection) -> dict[str, int]:
    """How much is awaiting review, by content type.

    Reported separately from the calendar and never mixed into it: none of this is
    publishable, and a count that blurs the two would be worse than no count.
    """
    drafts = DraftRepository(connection)
    counts: Counter[str] = Counter()
    for draft in drafts.list_by_status(DraftStatus.PENDING_REVIEW, limit=500):
        counts[draft.content_type.value] += 1
    return dict(sorted(counts.items()))


__all__ = [
    "CHANNEL_TIMEZONE",
    "Entry",
    "Freshness",
    "Waiting",
    "Warning_",
    "Week",
    "approved_unscheduled",
    "build_entry",
    "build_week",
    "diagnose",
    "pending_summary",
    "week_bounds",
]
