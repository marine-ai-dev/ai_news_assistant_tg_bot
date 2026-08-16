"""Suggesting a slot for an approved post — and showing its work.

An editor with nine approved drafts and an empty Thursday is doing arithmetic in their
head: this one is news so it should go sooner, that one is the third prompt this week,
the Tuesday slot already has something near it. This module does that arithmetic and
writes it down.

Two rules govern everything here.

**It suggests; it never schedules.** The output is a list of candidate times with
reasons. Creating the queue item is a separate, deliberate act by a person.

**Every number is explainable.** There is no opaque score. Each candidate carries the
reasons that moved it up or down, in words, and the same inputs always produce the same
answer. A recommendation an editor cannot argue with is a recommendation they should
ignore.

There is deliberately no concept of a *best* time to post. This project has no analytics
data, so such a claim would be invented — the windows are the ones the owner configured,
named after parts of the day.
"""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from ai_news_editor.domain.enums import ContentType, QueueStatus
from ai_news_editor.planning.buckets import (
    ACCESSIBLE_TARGET_MIN,
    DEFAULT_MIX,
    Bucket,
    bucket_for,
    is_accessible,
    is_practical,
)
from ai_news_editor.planning.calendar import (
    CONCENTRATION_THRESHOLD,
    Entry,
    build_entry,
    week_bounds,
)
from ai_news_editor.scheduling.clock import DAYPARTS, TimeError, describe, to_local, to_utc
from ai_news_editor.scheduling.freshness import freshness_window
from ai_news_editor.scheduling.queue import CROWDING_WINDOW
from ai_news_editor.storage.repositories import DraftRepository, ReviewDecisionRepository
from ai_news_editor.storage.repositories.publication_queue import PublicationQueueRepository

#: How many days ahead to consider. Beyond this a suggestion is guesswork about a week
#: whose content does not exist yet.
HORIZON_DAYS = 10

#: The soonest a suggestion may be, so there is time to look at it before it goes out.
MIN_LEAD = timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class Reason:
    """One thing that moved a candidate up or down, in words."""

    points: int
    text: str

    @property
    def sign(self) -> str:
        return "+" if self.points > 0 else ("−" if self.points < 0 else "·")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One possible time, with everything that argued for and against it."""

    when: datetime
    daypart: str
    reasons: tuple[Reason, ...] = field(default_factory=tuple)

    @property
    def score(self) -> int:
        return sum(r.points for r in self.reasons)

    @property
    def blocked(self) -> bool:
        """A collision is not a preference — it is a reason this slot is unusable."""
        return any(r.points <= -100 for r in self.reasons)

    def describe(self) -> str:
        return describe(self.when)


@dataclass(frozen=True, slots=True)
class Suggestion:
    """What the calendar would suggest, and why. Nothing has been scheduled."""

    draft_id: UUID
    bucket: Bucket
    candidates: tuple[Candidate, ...]
    #: Context the editor should read before accepting any of them.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def best(self) -> Candidate | None:
        usable = [c for c in self.candidates if not c.blocked]
        return max(usable, key=lambda c: (c.score, -c.when.timestamp())) if usable else None


def _slot_times(now: datetime, *, horizon: int = HORIZON_DAYS) -> list[tuple[datetime, str]]:
    """Every configured posting window between tomorrow-ish and the horizon.

    Reuses the Phase-9 dayparts rather than defining a second set of times, so changing
    the morning slot changes it everywhere.
    """
    slots: list[tuple[datetime, str]] = []
    local_now = to_local(now)
    for day in range(horizon + 1):
        target_date = (local_now + timedelta(days=day)).date()
        for name, (hour, minute) in DAYPARTS.items():
            naive = datetime.combine(target_date, datetime.min.time()).replace(
                hour=hour, minute=minute
            )
            try:
                when = to_utc(naive)
            except TimeError:
                # The two nights a year a wall clock is not a function. A suggestion
                # engine has no business guessing which of the two hours was meant.
                continue
            if when >= now + MIN_LEAD:
                slots.append((when, name))
    return sorted(slots)


def suggest_slot(
    connection: sqlite3.Connection,
    draft_id: UUID,
    *,
    now: datetime,
    channel: str,
    horizon: int = HORIZON_DAYS,
) -> Suggestion:
    """Where this approved draft could go, and what argues for each option.

    Deterministic: the same database and the same clock always produce the same list.
    """
    drafts = DraftRepository(connection)
    draft = drafts.get(draft_id)
    version = drafts.current_version(draft_id)
    bucket = bucket_for(draft.content_type, version.category)

    queue = PublicationQueueRepository(connection)
    scheduled: list[Entry] = []
    for item in queue.list_all(limit=500):
        if item.status in {QueueStatus.CANCELLED, QueueStatus.INVALIDATED}:
            continue
        entry = build_entry(connection, item, now=now)
        if entry is not None:
            scheduled.append(entry)

    deadline = _freshness_deadline(connection, draft, version.id, now)
    notes = _notes(_series_of(connection, draft), bucket, scheduled, deadline, now)

    candidates = [
        _evaluate(connection, when, name, draft, bucket, version, scheduled, deadline, now)
        for when, name in _slot_times(now, horizon=horizon)
    ]
    # Best first, so the top of the list is the answer and the rest is the argument.
    candidates.sort(key=lambda c: (c.blocked, -c.score, c.when))
    return Suggestion(
        draft_id=draft_id, bucket=bucket, candidates=tuple(candidates), notes=tuple(notes)
    )


def _freshness_deadline(
    connection: sqlite3.Connection, draft, version_id: UUID, now: datetime
) -> datetime | None:
    """The moment after which this post would be held for review rather than published."""
    decision = ReviewDecisionRepository(connection).latest_approval(draft.id, version_id)
    if decision is None:
        return None
    return decision.created_at + freshness_window(draft.content_type)


def _series_of(connection: sqlite3.Connection, draft) -> tuple[str, int] | None:
    """The series this post belongs to, read from the ContentItem that defines it."""
    if draft.content_item_id is None:
        return None
    from ai_news_editor.storage.repositories import ContentItemRepository

    with suppress(Exception):  # pragma: no cover - a draft's content item always exists
        item = ContentItemRepository(connection).get(draft.content_item_id)
        if item.series_name is not None and item.series_order is not None:
            return (item.series_name, item.series_order)
    return None


def _evaluate(
    connection: sqlite3.Connection,
    when: datetime,
    daypart: str,
    draft,
    bucket: Bucket,
    version,
    scheduled: list[Entry],
    deadline: datetime | None,
    now: datetime,
) -> Candidate:
    """Score one slot, recording a reason for every point moved."""
    reasons: list[Reason] = [Reason(1, f"{daypart} slot, currently free")]

    # --- hard blocks -------------------------------------------------------
    same_moment = [e for e in scheduled if e.item.scheduled_for == when]
    if same_moment:
        reasons.append(Reason(-1000, "another post is scheduled for this exact moment"))
    else:
        crowding = [
            e for e in scheduled
            if abs(e.item.scheduled_for - when) < CROWDING_WINDOW
        ]
        if crowding:
            minutes = int(
                min(abs(e.item.scheduled_for - when) for e in crowding).total_seconds() // 60
            )
            reasons.append(Reason(-100, f"another post is only {minutes} min away"))

    # --- freshness ---------------------------------------------------------
    if deadline is not None:
        if when > deadline:
            reasons.append(
                Reason(-1000, f"past its freshness window, which ends {describe(deadline)}")
            )
        else:
            remaining = deadline - when
            window = freshness_window(draft.content_type)
            if remaining < window * 0.25:
                reasons.append(Reason(-3, "cutting the freshness window fine"))
            elif draft.content_type is ContentType.NEWS and when < now + timedelta(days=1):
                reasons.append(Reason(4, "news keeps badly — sooner is better"))

    # --- editorial mix -----------------------------------------------------
    day_entries = [e for e in scheduled if e.local_time.date() == to_local(when).date()]
    if not day_entries:
        reasons.append(Reason(2, "nothing else scheduled that day"))

    week_start, week_end = week_bounds(when)
    week_entries = [e for e in scheduled if week_start <= e.local_time.date() <= week_end]
    if week_entries:
        share = sum(1 for e in week_entries if e.bucket is bucket) / len(week_entries)
        target = DEFAULT_MIX[bucket]
        if share < target:
            reasons.append(Reason(3, f"that week is short on {bucket.value}"))
        elif share > target * 2:
            reasons.append(Reason(-2, f"that week already leans {bucket.value}"))

        # Audience balance, the constraint the channel exists for.
        accessible = sum(1 for e in week_entries if e.accessible) / len(week_entries)
        if is_accessible(version.audience) and accessible < ACCESSIBLE_TARGET_MIN:
            reasons.append(Reason(3, "that week needs more beginner-accessible content"))
        elif not is_accessible(version.audience) and accessible < ACCESSIBLE_TARGET_MIN:
            reasons.append(Reason(-2, "that week is already short on accessible content"))

    # --- streaks -----------------------------------------------------------
    neighbours = sorted(
        (e for e in scheduled if abs(e.item.scheduled_for - when) < timedelta(days=1)),
        key=lambda e: e.item.scheduled_for,
    )
    if neighbours and all(e.bucket is bucket for e in neighbours) and len(neighbours) >= 2:
        reasons.append(Reason(-3, f"would make a run of {bucket.value} posts"))
    if not is_accessible(version.audience) and neighbours and not any(
        e.accessible for e in neighbours
    ):
        reasons.append(Reason(-2, "the posts around it also assume prior experience"))

    # --- concentration -----------------------------------------------------
    if is_practical(draft.content_type) and week_entries:
        practical = [e for e in week_entries if is_practical(e.content_type) and e.tool]
        tool = _tool_of(draft, version)
        if tool and practical:
            same = sum(1 for e in practical if e.tool == tool)
            if same / len(practical) >= CONCENTRATION_THRESHOLD:
                reasons.append(Reason(-3, f"that week is already heavy on {tool}"))

    # --- series ------------------------------------------------------------
    series = _series_of(connection, draft)
    if series is not None:
        series_name, series_order = series
        earlier = [
            e for e in scheduled
            if e.series and e.series[0] == series_name and e.series[1] < series_order
        ]
        later = [
            e for e in scheduled
            if e.series and e.series[0] == series_name and e.series[1] > series_order
        ]
        if any(e.item.scheduled_for > when for e in earlier):
            reasons.append(Reason(-1000, "an earlier part of the series publishes after this"))
        if any(e.item.scheduled_for < when for e in later):
            reasons.append(Reason(-1000, "a later part of the series publishes before this"))
        if earlier and all(e.item.scheduled_for < when for e in earlier):
            reasons.append(Reason(2, "follows the previous part of the series"))

    return Candidate(when=when, daypart=daypart, reasons=tuple(reasons))


def _tool_of(draft, version) -> str | None:
    """The tool this post is about, from stored metadata only."""
    for asset in version.media:
        if asset.tool_used:
            return asset.tool_used
    return None


def _notes(
    series: tuple[str, int] | None,
    bucket: Bucket,
    scheduled: list[Entry],
    deadline: datetime | None,
    now: datetime,
) -> list[str]:
    """Context worth reading before accepting any suggestion."""
    notes: list[str] = []
    if deadline is not None:
        notes.append(
            f"Publishable until {describe(deadline)}; after that it is held for review."
        )
    else:
        notes.append("No approval found — this draft cannot be scheduled at all yet.")

    if series is not None:
        notes.append(f"Part {series[1]} of '{series[0]}'.")

    upcoming = [e for e in scheduled if e.item.scheduled_for >= now]
    if upcoming:
        same_bucket = sum(1 for e in upcoming if e.bucket is bucket)
        notes.append(
            f"{same_bucket} of {len(upcoming)} upcoming posts are already {bucket.value}."
        )
    return notes
